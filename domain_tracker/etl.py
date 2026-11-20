"""Daily ETL pipeline — 5-step process for fetching and loading Instantly data."""

import json
import sys
import time
from datetime import datetime, timedelta
from typing import List, Optional, Tuple
from urllib.request import urlopen

from .api import InstantlyClient, InstantlyAPIError
from .config import settings
from .db import (
    close_connection,
    ensure_account_exists,
    extract_domain,
    get_connection,
    init_db,
    upsert_account,
    upsert_account_campaign,
    upsert_daily_metrics,
    upsert_email_tags,
)
from .analytics import classify_domains_with_provider, get_domain_stats
from .users import UserRegistry


# ------------------------------------------------------------------
# Date helpers
# ------------------------------------------------------------------

def _date_range_chunks(
    start_date: str, end_date: str, chunk_days: int = 31
) -> List[Tuple[str, str]]:
    """Split (start, end) into non-overlapping date chunks.

    Each chunk is at most chunk_days long.
    """
    start = datetime.strptime(start_date, "%Y-%m-%d")
    end = datetime.strptime(end_date, "%Y-%m-%d")
    chunks: List[Tuple[str, str]] = []
    cursor = start
    while cursor <= end:
        chunk_end = min(cursor + timedelta(days=chunk_days - 1), end)
        chunks.append((cursor.strftime("%Y-%m-%d"), chunk_end.strftime("%Y-%m-%d")))
        cursor = chunk_end + timedelta(days=1)
    return chunks


def _today_str() -> str:
    return datetime.utcnow().strftime("%Y-%m-%d")


def _fetch_analytics_safe(
    client: "InstantlyClient",
    start_date: str,
    end_date: str,
    emails: Optional[List[str]],
    log: callable,
) -> list:
    """Fetch daily analytics, auto-shrinking on 413 "payload too large".

    Splits whichever dimension is larger — the email batch or the date
    range — in half and retries each half recursively, until the request
    fits or we hit a single email on a single day (at which point that
    slice is skipped and logged, not silently retried forever).
    """
    try:
        return client.get_daily_analytics(start_date, end_date, emails=emails)
    except InstantlyAPIError as e:
        if "413" not in str(e):
            raise

        span_days = (
            datetime.strptime(end_date, "%Y-%m-%d")
            - datetime.strptime(start_date, "%Y-%m-%d")
        ).days + 1
        n_emails = len(emails) if emails else 0

        if n_emails > 1:
            mid = n_emails // 2
            log(f"    413 too large — splitting {n_emails} email(s) "
                f"into batches of {mid} and {n_emails - mid}")
            return (
                _fetch_analytics_safe(client, start_date, end_date, emails[:mid], log)
                + _fetch_analytics_safe(client, start_date, end_date, emails[mid:], log)
            )

        if span_days > 1:
            mid_date = (
                datetime.strptime(start_date, "%Y-%m-%d")
                + timedelta(days=span_days // 2 - 1)
            ).strftime("%Y-%m-%d")
            next_date = (
                datetime.strptime(mid_date, "%Y-%m-%d") + timedelta(days=1)
            ).strftime("%Y-%m-%d")
            log(f"    413 too large — splitting {start_date}→{end_date} "
                f"into {start_date}→{mid_date} and {next_date}→{end_date}")
            return (
                _fetch_analytics_safe(client, start_date, mid_date, emails, log)
                + _fetch_analytics_safe(client, next_date, end_date, emails, log)
            )

        # Already at minimum granularity (1 email, 1 day) and still too large.
        who = emails[0] if emails else "(unfiltered)"
        log(f"    ERROR: 413 persists at minimum granularity "
            f"({who}, {start_date}) — skipping this slice")
        return []


def _last_data_date(user_slug: str = "default") -> Optional[str]:
    """Return the latest date in daily_metrics for a user, or None."""
    conn = get_connection(user_slug)
    row = conn.execute(
        "SELECT MAX(date) AS max_d FROM daily_metrics"
    ).fetchone()
    return row["max_d"] if row and row["max_d"] else None


# ------------------------------------------------------------------
# Progress callback type
# ------------------------------------------------------------------

ProgressFn = Optional[callable]


def _default_progress(msg: str):
    print(f"  • {msg}")


# ------------------------------------------------------------------
# Pipeline step 1: Refresh accounts
# ------------------------------------------------------------------

def refresh_accounts(
    client: InstantlyClient,
    progress: ProgressFn = None,
    user_slug: str = "default",
) -> int:
    """Fetch all accounts from API and UPSERT into accounts table.

    Returns count of accounts upserted.
    """
    log = progress or _default_progress
    log("Fetching accounts from Instantly API...")

    accounts = client.list_accounts()
    count = 0
    conn = get_connection(user_slug)

    for acct in accounts:
        email = acct.get("email", "")
        if not email:
            continue
        domain = extract_domain(email)

        # Handle nested warmup object for warmup_status
        warmup = acct.get("warmup", {}) or {}
        warmup_status = (
            acct.get("warmup_status") or warmup.get("warmup_status")
        )

        upsert_account(
            conn=conn,
            email=email,
            domain=domain,
            status=acct.get("status"),
            warmup_status=warmup_status,
            tracking_domain=acct.get("tracking_domain_name"),
            provider_code=acct.get("provider_code"),
            first_name=acct.get("first_name"),
            last_name=acct.get("last_name"),
            daily_limit=(
                acct.get("daily_limit") or warmup.get("limit")
            ),
        )
        count += 1

    conn.commit()
    log(f"Upserted {count} accounts")
    return count


# ------------------------------------------------------------------
# Pipeline step 2: Refresh campaign mappings
# ------------------------------------------------------------------

def refresh_campaign_mappings(
    client: InstantlyClient,
    progress: ProgressFn = None,
    user_slug: str = "default",
) -> int:
    """Fetch campaign mappings for all accounts.

    Returns count of mappings upserted.
    """
    log = progress or _default_progress
    conn = get_connection(user_slug)

    # Get all accounts from local DB
    account_emails = [
        r["email"]
        for r in conn.execute(
            "SELECT email FROM accounts"
        ).fetchall()
    ]

    log(f"Fetching campaign mappings for {len(account_emails)} accounts...")
    count = 0

    for email in account_emails:
        try:
            mappings = client.get_account_campaign_mappings(email)
        except Exception as e:
            log(f"  WARNING: Could not fetch mappings for {email}: {e}")
            # Brief sleep to avoid hammering API on transient errors
            time.sleep(0.5)
            continue
        # Brief rate-limit pause between calls
        time.sleep(0.05)

        for mapping in mappings:
            upsert_account_campaign(
                conn=conn,
                email_account=email,
                campaign_id=mapping.get("campaign_id", ""),
                campaign_name=mapping.get("campaign_name"),
                campaign_status=mapping.get("status"),
            )
            count += 1

    conn.commit()
    log(f"Upserted {count} campaign mappings")
    return count


# ------------------------------------------------------------------
# Pipeline step 3: Pull daily metrics
# ------------------------------------------------------------------

def pull_daily_metrics(
    client: InstantlyClient,
    start_date: str,
    end_date: str,
    progress: ProgressFn = None,
    chunk_days: int = 31,
    user_slug: str = "default",
    emails: Optional[List[str]] = None,
    email_batch_size: int = 100,
) -> int:
    """Fetch daily analytics for a date range and UPSERT into daily_metrics.

    Handles chunking for backfill (e.g. month-by-month).

    Args:
        emails: When provided, restrict the pull to these mailboxes and
            fetch them in bounded batches (email_batch_size per call). This
            keeps each API response small — required for large workspaces
            where the unfiltered call would time out or be truncated, and
            used by the per-domain pull. When None, a single unfiltered call
            per date chunk is made (legacy behavior).

    Returns total metric rows upserted.
    """
    log = progress or _default_progress
    total = 0
    chunks = _date_range_chunks(start_date, end_date, chunk_days)
    conn = get_connection(user_slug)

    def _load_rows(rows, fallback_date: str) -> int:
        loaded = 0
        for row in rows:
            date = row.get("date", fallback_date)
            email_acct = row.get("email_account", "")
            if not email_acct:
                continue

            # Ensure the parent account exists to avoid FK violations
            ensure_account_exists(conn, email_acct)

            upsert_daily_metrics(
                conn=conn,
                date=date,
                email_account=email_acct,
                sent=row.get("sent", 0) or 0,
                bounced=row.get("bounced", 0) or 0,
                contacted=row.get("contacted", 0) or 0,
                new_leads_contacted=row.get("new_leads_contacted", 0) or 0,
                opened=row.get("opened", 0) or 0,
                unique_opened=row.get("unique_opened", 0) or 0,
                replies=row.get("replies", 0) or 0,
                unique_replies=row.get("unique_replies", 0) or 0,
                replies_automatic=row.get("replies_automatic", 0) or 0,
                unique_replies_automatic=row.get(
                    "unique_replies_automatic", 0
                ) or 0,
                clicks=row.get("clicks", 0) or 0,
                unique_clicks=row.get("unique_clicks", 0) or 0,
            )
            loaded += 1
        return loaded

    # Pre-split the email filter into bounded batches (if provided).
    email_batches: Optional[List[List[str]]] = None
    if emails:
        email_batches = [
            emails[i:i + email_batch_size]
            for i in range(0, len(emails), email_batch_size)
        ]

    scope = f" for {len(emails)} mailbox(es)" if emails else ""
    log(f"Pulling daily metrics{scope}: {start_date} → {end_date} "
        f"({len(chunks)} chunk(s))")

    for i, (chunk_start, chunk_end) in enumerate(chunks):
        log(f"  Chunk {i + 1}/{len(chunks)}: {chunk_start} → {chunk_end}")

        if email_batches is not None:
            for bi, batch in enumerate(email_batches):
                try:
                    rows = _fetch_analytics_safe(
                        client, chunk_start, chunk_end, batch, log
                    )
                except InstantlyAPIError as e:
                    log(f"    ERROR on email batch {bi + 1}/{len(email_batches)} "
                        f"({chunk_start}-{chunk_end}): {e}")
                    continue
                total += _load_rows(rows, chunk_start)
        else:
            try:
                rows = _fetch_analytics_safe(
                    client, chunk_start, chunk_end, None, log
                )
            except InstantlyAPIError as e:
                log(f"  ERROR fetching analytics for {chunk_start}-{chunk_end}: {e}")
                continue
            total += _load_rows(rows, chunk_start)

        conn.commit()

    log(f"Upserted {total} daily metric rows")
    return total


# ------------------------------------------------------------------
# Per-domain pull — scoped ETL for a single domain
# ------------------------------------------------------------------

def _domain_mailboxes(conn, domain: str) -> List[str]:
    """Return all mailbox emails on a given domain from the local accounts table."""
    return [
        r["email"]
        for r in conn.execute(
            "SELECT email FROM accounts WHERE LOWER(domain) = ?", (domain,)
        ).fetchall()
    ]


def run_domain_etl(
    user_slug: str = "default",
    domain: str = "",
    days: int = 180,
    progress: ProgressFn = None,
) -> dict:
    """Pull data for a SINGLE domain only, over the last `days` days.

    Steps (all scoped to the one domain — no full-workspace work):
      1. Resolve the domain's mailboxes from the local accounts table.
         If none are known locally, fetch the account list from Instantly,
         keep only this domain's mailboxes, and upsert them (+ their tags).
      2. Pull daily metrics for those mailboxes over the last `days` days.
      3. Re-run burn analysis so the domain's health is reclassified.

    Campaign metrics are intentionally NOT synced here (kept fast).

    Returns: {"domain": str, "mailboxes": int, "rows": int}.
    """
    log = progress or _default_progress
    domain = (domain or "").strip().lower()
    if not domain:
        log("ERROR: No domain provided.")
        return {"domain": domain, "mailboxes": 0, "rows": 0}

    # Ensure schema + resolve the user's API key
    init_db(user_slug)
    registry = UserRegistry()
    user = registry.get_user(user_slug)
    if not user or not user.instantly_api_key:
        log(f"ERROR: User '{user_slug}' not found or has no API key.")
        return {"domain": domain, "mailboxes": 0, "rows": 0}

    client = InstantlyClient(api_key=user.instantly_api_key)
    conn = get_connection(user_slug)

    # ── Step 1: Resolve the domain's mailboxes ──
    emails = _domain_mailboxes(conn, domain)

    if not emails:
        log(f"No local mailboxes for '{domain}' — fetching account list from Instantly...")
        accounts = client.list_accounts(include_tags=True)
        matched = 0
        for acct in accounts:
            email = acct.get("email", "")
            if not email or extract_domain(email).lower() != domain:
                continue

            warmup = acct.get("warmup", {}) or {}
            warmup_status = acct.get("warmup_status") or warmup.get("warmup_status")
            upsert_account(
                conn=conn,
                email=email,
                domain=extract_domain(email),
                status=acct.get("status"),
                warmup_status=warmup_status,
                tracking_domain=acct.get("tracking_domain_name"),
                provider_code=acct.get("provider_code"),
                first_name=acct.get("first_name"),
                last_name=acct.get("last_name"),
                daily_limit=acct.get("daily_limit") or warmup.get("limit"),
            )

            tags = [t.get("label", "") for t in acct.get("tags", []) if t.get("label")]
            if tags:
                upsert_email_tags(conn, email, tags)
            matched += 1

        conn.commit()
        log(f"  Found {matched} mailbox(es) for '{domain}'")
        emails = _domain_mailboxes(conn, domain)

    if not emails:
        log(f"No mailboxes found for domain '{domain}'. Nothing to pull.")
        return {"domain": domain, "mailboxes": 0, "rows": 0}

    # ── Step 2: Pull daily metrics for those mailboxes ──
    today = datetime.utcnow()
    start = (today - timedelta(days=days)).strftime("%Y-%m-%d")
    end = today.strftime("%Y-%m-%d")
    log(f"Pulling last {days} days ({start} → {end}) for {len(emails)} "
        f"mailbox(es) on '{domain}'...")
    rows = pull_daily_metrics(
        client,
        start,
        end,
        progress=log,
        user_slug=user_slug,
        emails=emails,
    )

    # ── Step 3: Re-run burn analysis ──
    log("Running burn analysis...")
    run_burn_analysis(progress=log, user_slug=user_slug)

    log(f"✅ Domain pull complete: '{domain}' — {len(emails)} mailbox(es), "
        f"{rows} metric rows")
    return {"domain": domain, "mailboxes": len(emails), "rows": rows}


# ------------------------------------------------------------------
# Provider detection via DNS MX lookup
# ------------------------------------------------------------------

def _doh_query(domain: str) -> Optional[dict]:
    """Query Google DNS-over-HTTPS for MX records of a domain."""
    import ssl
    url = f"https://dns.google.com/resolve?name={domain}&type=MX"
    try:
        ctx = ssl.create_default_context()
        resp = urlopen(url, timeout=10, context=ctx)
        return json.loads(resp.read().decode())
    except Exception:
        return None


def detect_provider(domain: str) -> str:
    """Detect email provider via DNS MX lookup.

    Returns 'Gmail', 'Outlook', or 'Other'.
    """
    result = _doh_query(domain)
    if result is None:
        return "Other"

    answers = result.get("Answer", [])
    for answer in answers:
        data = answer.get("data", "").lower()
        if "google.com" in data or "googlemail.com" in data:
            return "Gmail"
        if "outlook.com" in data or "protection.outlook.com" in data or "microsoft.com" in data:
            return "Outlook"

    return "Other"


def _resolve_mx_ip(domain: str) -> Optional[str]:
    """Resolve the first MX hostname for a domain to an IP address.

    Feeds MX hostname through socket.gethostbyname() to get its A record.

    Returns IP as string (e.g. '52.101.10.12') or None on failure.
    """
    import socket
    result = _doh_query(domain)
    if result is None:
        return None
    answers = result.get("Answer", [])
    for answer in answers:
        data = answer.get("data", "")
        # MX record format: "<priority> <hostname>"
        parts = data.split()
        if len(parts) >= 2:
            hostname = parts[1].rstrip(".")
            try:
                return socket.gethostbyname(hostname)
            except Exception:
                continue
    return None


def _ip_to_country(ip: str) -> Optional[str]:
    """Look up the country code for an IP address using ipinfo.io.

    Returns a 2-letter ISO country code (e.g. 'US', 'IN') or None.
    """
    import ssl
    url = f"https://ipinfo.io/{ip}/json"
    try:
        ctx = ssl.create_default_context()
        resp = urlopen(url, timeout=10, context=ctx)
        data = json.loads(resp.read().decode())
        return data.get("country")
    except Exception:
        return None


def detect_hosted_country(domain: str) -> tuple:
    """Detect the hosting country for a domain's email MX server.

    Chains: MX DNS lookup → resolve hostname to IP → look up IP location.

    Returns (ip, country_code) tuple, e.g. ('52.101.10.12', 'US').
    Either value may be None on failure.
    """
    ip = _resolve_mx_ip(domain)
    if not ip:
        return (None, None)
    country = _ip_to_country(ip)
    return (ip, country)


def batch_detect_providers(
    progress: ProgressFn = None,
    force: bool = False,
    user_slug: str = "default",
) -> int:
    """Detect email providers for domains via DNS MX lookup.

    Also resolves the MX server IP and hosting country via ipinfo.io.

    By default only detects domains where provider is not set.
    Use force=True to re-detect all domains.

    Rate-limited: 100ms between queries.
    Returns count of domains updated.
    """
    log = progress or _default_progress
    conn = get_connection(user_slug)

    if force:
        rows = conn.execute(
            "SELECT DISTINCT domain FROM accounts"
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT DISTINCT domain FROM accounts
               WHERE provider IS NULL OR provider = ''"""
        ).fetchall()

    if not rows:
        log("All domains already have a provider set.")
        return 0

    domains = [r["domain"] for r in rows]
    log(f"Detecting provider for {len(domains)} domains via DNS MX lookup...")

    updated = 0
    for i, domain in enumerate(domains):
        provider = detect_provider(domain)
        # Also resolve hosted IP and country
        ip, country = detect_hosted_country(domain)
        conn.execute(
            "UPDATE accounts SET provider = ?, hosted_ip = ?, hosted_country = ? WHERE domain = ?",
            (provider, ip, country, domain),
        )
        updated += 1
        ip_str = ip or "?"
        country_str = country or "?"
        log(f"  [{i+1}/{len(domains)}] {domain} -> {provider} ({ip_str}, {country_str})")

        # Rate limit: 100ms between queries
        if i < len(domains) - 1:
            time.sleep(0.1)

    conn.commit()
    log(f"Updated {updated} domains with provider + hosting info")
    return updated


# ------------------------------------------------------------------
# Pipeline step 4: Run burn analysis
# ------------------------------------------------------------------

def run_burn_analysis(
    progress: ProgressFn = None,
    user_slug: str = "default",
) -> List[dict]:
    """Query DB for domain stats and classify domains using provider-aware thresholds.

    Returns classified domain stats list.
    """
    log = progress or _default_progress
    log("Running burn analysis (provider-aware)...")

    stats = get_domain_stats(user_slug=user_slug)
    if not stats:
        log("  No domain stats to analyze (insufficient data?)")
        return []

    classified = classify_domains_with_provider(stats, user_slug=user_slug)

    burned = [d for d in classified if d["status"] == "BURNED"]
    at_risk = [d for d in classified if d["status"] == "AT_RISK"]
    flagged = [d for d in classified if d["status"] == "RELATIVE_FLAG"]
    healthy = [d for d in classified if d["status"] == "HEALTHY"]

    log(f"  {len(classified)} domains analysed: "
        f"{len(burned)} BURNED, "
        f"{len(at_risk)} AT_RISK, "
        f"{len(flagged)} relative flags, "
        f"{len(healthy)} HEALTHY")

    return classified


def refresh_tags(
    client: InstantlyClient,
    progress: ProgressFn = None,
    user_slug: str = "default",
) -> int:
    """Fetch accounts from API with embedded Custom Tags and upsert into email_tags table.

    Tags are extracted from the tags[] array on each account response
    (requires ?include_tags=true on the accounts endpoint).

    Compared to the old approach that read from warmup_custom_ftag
    (which was near-empty — only 1/500+ accounts had it), this pulls
    the real 29 Custom Tags like "healthy - MS", "AirMail", etc.

    Accounts with no tags are skipped (treated as unlabeled).

    Returns count of tag rows upserted.
    """
    log = progress or _default_progress
    log("Syncing email tags from Instantly Custom Tags API (include_tags=true)...")

    accounts = client.list_accounts(include_tags=True)
    conn = get_connection(user_slug)
    count = 0

    for acct in accounts:
        email = acct.get("email", "")
        if not email:
            continue

        # Extract tag labels from the embedded tags[] array
        tags = [t.get("label", "") for t in acct.get("tags", []) if t.get("label")]

        if tags:
            upsert_email_tags(conn, email, tags)
            count += len(tags)

    conn.commit()
    log(f"Synced tags for {len(accounts)} accounts: {count} tag rows")
    return count


# ------------------------------------------------------------------
# Full pipeline run
# ------------------------------------------------------------------

def run_etl(
    user_slug: str = "default",
    skip_campaigns: bool = False,
    full_backfill: bool = False,
    skip_campaign_sync: bool = False,
    progress: ProgressFn = None,
) -> List[dict]:
    """Execute the full ETL pipeline for a specific user.

    Args:
        user_slug: User identifier (e.g. "default", "client_acme").
        skip_campaigns: Skip campaign mapping refresh (for faster daily runs).
        full_backfill: Pull from BACKFILL_START_DATE regardless of existing data.
        skip_campaign_sync: Skip campaign account metrics sync (default: enabled).
        progress: Callable for progress messages.

    Returns:
        Classified domain stats from the burn analysis step.
    """
    log = progress or _default_progress

    # Ensure schema exists for this user
    init_db(user_slug)

    # Get user's API key from registry
    registry = UserRegistry()
    user = registry.get_user(user_slug)
    if not user:
        log(f"ERROR: User '{user_slug}' not found in registry.")
        sys.exit(1)

    api_key = user.instantly_api_key
    if not api_key:
        log(f"ERROR: No API key configured for user '{user_slug}'.")
        sys.exit(1)

    # Instantiate API client with user's key
    client = InstantlyClient(api_key=api_key)

    # ── Step 1: Refresh accounts ──
    log("Step 1/6: Refresh accounts")
    refresh_accounts(client, progress=log, user_slug=user_slug)

    # ── Step 2: Refresh email tags ──
    log("Step 2/7: Refresh email tags")
    refresh_tags(client, progress=log, user_slug=user_slug)

    # ── Step 3: Refresh campaign mappings ──
    step3_label = "Step 3/7: Refresh campaign mappings"
    if skip_campaigns:
        log(f"{step3_label} (SKIPPED via --skip-campaigns)")
    else:
        log(step3_label)
        refresh_campaign_mappings(client, progress=log, user_slug=user_slug)

    # ── Step 4: Pull daily metrics ──
    log("Step 4/7: Pull daily metrics")
    today = _today_str()

    if full_backfill:
        # Full backfill from configured start date
        start = settings.BACKFILL_START_DATE
    else:
        # Incremental: start from day after last data date
        last_date = _last_data_date(user_slug=user_slug)
        if last_date:
            start = (
                datetime.strptime(last_date, "%Y-%m-%d")
                + timedelta(days=1)
            ).strftime("%Y-%m-%d")
        else:
            # No data yet — backfill from start
            start = settings.BACKFILL_START_DATE

    if start > today:
        log(f"  Data is already current (last: {_last_data_date(user_slug=user_slug)}). Skipping.")
    else:
        conn = get_connection(user_slug)
        all_emails = [
            r["email"] for r in conn.execute("SELECT email FROM accounts").fetchall()
        ]
        pull_daily_metrics(
            client, start, today, progress=log, user_slug=user_slug,
            emails=all_emails,
        )

    # ── Step 5: Run burn analysis ──
    log("Step 5/7: Burn analysis")
    classified = run_burn_analysis(progress=log, user_slug=user_slug)

    # ── Step 6: Sync campaign account metrics ──
    step6_label = "Step 6/7: Sync campaign account metrics"
    if skip_campaign_sync:
        log(f"{step6_label} (SKIPPED via --skip-campaign-sync)")
    else:
        log(step6_label)
        try:
            from .campaign_etl import incremental_campaign_sync
            # List all campaigns from API and incremental sync each
            campaigns = client.list_campaigns()
            if campaigns:
                log(f"  Syncing {len(campaigns)} campaigns (incremental)...")
                for i, camp in enumerate(campaigns):
                    cid = camp.get("campaign_id", camp.get("id", ""))
                    if cid:
                        log(f"    [{i+1}/{len(campaigns)}] Campaign {cid[:12]}...")
                        try:
                            incremental_campaign_sync(
                                cid, session=user_slug, callback=log
                            )
                        except Exception as e:
                            log(f"      WARNING: {e}")
            else:
                log("  No campaigns to sync.")
        except Exception as e:
            log(f"  ERROR during campaign sync: {e}")

    # ── Step 7: Generate report ── (done by CLI after this returns)
    log("Step 7/7: Report — run `python -m domain_tracker report` to view")

    return classified
