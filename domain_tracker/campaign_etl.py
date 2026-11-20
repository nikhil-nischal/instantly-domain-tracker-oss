"""ETL pipeline for campaign account metrics.

Fetches per-account metrics within campaigns from the Instantly API and
stores them in campaign_account_metrics and campaign_analytics tables.

Account-campaign mapping is derived from email records (eaccount field)
since campaign email_list is always empty.

Key API rules applied here:
  - ue_type=1 = sent, ue_type=2 = received
  - is_auto_reply is always null — all received count as replies
  - min_timestamp_created returns HTTP 500 — cursor-based approach instead
  - Rate limit: 1.1s delay between calls
  - Analytics endpoint returns plain array, not {items: [...]}
"""

import time
from datetime import datetime, timezone
from typing import Callable, Optional

from .api import InstantlyClient
from .classifier import classify_email
from .db import (
    ensure_account_exists,
    get_connection,
    upsert_account_campaign,
    upsert_campaign_account_metrics,
    upsert_campaign_analytics,
)
from .users import UserRegistry

# ---------------------------------------------------------------------------
# Progress callback type
# ---------------------------------------------------------------------------

ProgressFn = Optional[Callable[[str], None]]


def _default_progress(msg: str):
    print(f"  • {msg}")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_ts() -> str:
    return datetime.now(timezone.utc).isoformat()


def _match_analytics(analytics_list: list, campaign_id: str) -> Optional[dict]:
    """Find this campaign's row in an analytics response.

    /api/v2/campaigns/analytics ignores the campaign_ids filter and always
    returns every campaign — never trust analytics_list[0].
    """
    for row in analytics_list:
        if row.get("campaign_id") == campaign_id:
            return row
    return None


# ---------------------------------------------------------------------------
# sync_campaign_account_metrics
# ---------------------------------------------------------------------------

def sync_campaign_account_metrics(
    campaign_id: str,
    session: Optional[str] = None,
    callback: ProgressFn = None,
) -> int:
    """Fetch all sent and received emails for a campaign and aggregate per-account.

    Args:
        campaign_id: Campaign UUID.
        session: User slug (default "default").
        callback: Optional progress callback.

    Returns:
        Number of accounts processed.
    """
    from .api import InstantlyClient
    from .users import UserRegistry

    log = callback or _default_progress
    user_slug = session or "default"
    registry = UserRegistry()
    user = registry.get_user(user_slug)
    if not user:
        log(f"ERROR: User '{user_slug}' not found.")
        return 0

    client = InstantlyClient(api_key=user.instantly_api_key)
    conn = get_connection(user_slug)

    # ── Aggregate per-account counters ──
    # Key: account_email, Value: {"sent": int, "replies": int, "last_activity": str, "most_recent_received": dict|None}
    accounts: dict[str, dict] = {}

    def _process_emails(ue_type: int, label: str):
        """Fetch all pages of emails for a given ue_type and aggregate."""
        log(f"  Fetching {label} emails (ue_type={ue_type})...")
        starting_after: Optional[str] = None
        page = 0

        while True:
            page += 1
            try:
                data = client.get_emails(
                    campaign_id=campaign_id,
                    email_type="sent" if ue_type == 1 else "received",
                    limit=100,
                    starting_after=starting_after,
                    sort_by="timestamp_created",
                    sort_order="desc",
                )
            except Exception as e:
                log(f"    ERROR fetching {label} page {page}: {e}")
                break

            items = data.get("items", [])
            if not items:
                break

            for item in items:
                eaccount = item.get("eaccount", "")
                if not eaccount:
                    continue

                if eaccount not in accounts:
                    accounts[eaccount] = {"sent": 0, "replies": 0, "last_activity": None, "most_recent_received": None, "most_recent_received_ts": None}

                if ue_type == 1:
                    accounts[eaccount]["sent"] += 1
                else:
                    # is_auto_reply is always null — all received count as replies
                    accounts[eaccount]["replies"] += 1
                    # Track the most recent received email by timestamp_created
                    # Use a dedicated field, not last_activity (which is also set by sent emails)
                    ts = item.get("timestamp_created") or item.get("created_at")
                    current_recv_ts = accounts[eaccount]["most_recent_received_ts"]
                    if current_recv_ts is None or (ts and ts > current_recv_ts):
                        accounts[eaccount]["most_recent_received"] = item
                        accounts[eaccount]["most_recent_received_ts"] = ts

                # Track latest timestamp as last_activity
                ts = item.get("timestamp_created") or item.get("created_at")
                if ts:
                    current = accounts[eaccount]["last_activity"]
                    if current is None or ts > current:
                        accounts[eaccount]["last_activity"] = ts

            starting_after = data.get("next_starting_after")
            log(f"    Page {page}: {len(items)} items, next_cursor={starting_after is not None}")

            if not starting_after:
                break

    # Fetch sent emails (ue_type=1)
    _process_emails(1, "sent")

    # Fetch received emails (ue_type=2)
    _process_emails(2, "received")

    # ── UPSERT into campaign_account_metrics ──
    log(f"  Upserting {len(accounts)} account metrics...")
    acct_count = 0
    for email, data in accounts.items():
        # Extract subject/i_status from most recent received email if available
        subject_line = None
        i_status = None
        classification = None
        recv_item = data.get("most_recent_received")
        if recv_item:
            subject_line = (recv_item.get("subject") or "").strip() or None
            i_status = recv_item.get("i_status")
            classification = classify_email(recv_item)

        ensure_account_exists(conn, email)
        upsert_campaign_account_metrics(
            conn=conn,
            campaign_id=campaign_id,
            account_email=email,
            total_sent=data["sent"],
            replies=data["replies"],
            last_activity_at=data["last_activity"],
            subject_line=subject_line,
            i_status=i_status,
            classification=classification,
        )
        acct_count += 1

    conn.commit()
    log(f"  Upserted metrics for {acct_count} accounts")
    return acct_count


# ---------------------------------------------------------------------------
# sync_campaign_analytics
# ---------------------------------------------------------------------------

def sync_campaign_analytics(
    campaign_id: str,
    session: Optional[str] = None,
    callback: ProgressFn = None,
) -> dict:
    """Fetch campaign analytics and UPSERT into campaign_analytics table.

    Also processes account metrics for this campaign.

    Args:
        campaign_id: Campaign UUID.
        session: User slug.
        callback: Optional progress callback.

    Returns:
        The analytics data dict, or empty dict on failure.
    """
    from .api import InstantlyClient
    from .users import UserRegistry

    log = callback or _default_progress
    user_slug = session or "default"

    registry = UserRegistry()
    user = registry.get_user(user_slug)
    if not user:
        log(f"ERROR: User '{user_slug}' not found.")
        return {}

    client = InstantlyClient(api_key=user.instantly_api_key)
    conn = get_connection(user_slug)

    # Fetch analytics
    try:
        analytics_list = client.get_campaign_analytics([campaign_id])
    except Exception as e:
        log(f"  ERROR fetching analytics: {e}")
        return {}

    if not analytics_list:
        log("  No analytics data returned")
        return {}

    data = _match_analytics(analytics_list, campaign_id)
    if not data:
        log(f"  WARNING: campaign {campaign_id[:12]} not found in analytics response — skipping")
        return {}

    upsert_campaign_analytics(conn, campaign_id, data)
    conn.commit()
    log(f"  Upserted campaign analytics for {campaign_id}")

    # Now sync account metrics for this campaign
    sync_campaign_account_metrics(campaign_id, session=session, callback=callback)

    return data


# ---------------------------------------------------------------------------
# sync_all_campaign_overviews
# ---------------------------------------------------------------------------

def sync_all_campaign_overviews(
    session: Optional[str] = None,
    callback: ProgressFn = None,
) -> int:
    """Refresh campaign_analytics (name/leads/sent/replies) for every campaign.

    This is the homepage-listing data only — it does NOT touch
    campaign_account_metrics (the per-mailbox detail pull), which is a much
    heavier, separate step (see sync_campaign_account_metrics).

    Since /api/v2/campaigns/analytics ignores the campaign_ids filter and
    always returns every campaign, this fetches it once and matches rows
    by their own campaign_id instead of looping a call per campaign.

    Returns:
        Number of campaigns updated.
    """
    from .api import InstantlyClient
    from .users import UserRegistry

    log = callback or _default_progress
    user_slug = session or "default"

    registry = UserRegistry()
    user = registry.get_user(user_slug)
    if not user:
        log(f"ERROR: User '{user_slug}' not found.")
        return 0

    client = InstantlyClient(api_key=user.instantly_api_key)
    conn = get_connection(user_slug)

    log("Fetching campaign list...")
    try:
        campaigns = client.list_campaigns()
    except Exception as e:
        log(f"ERROR fetching campaigns: {e}")
        return 0

    if not campaigns:
        log("No campaigns found.")
        return 0

    by_id = {}
    for c in campaigns:
        cid = c.get("campaign_id") or c.get("id")
        if cid:
            by_id[cid] = c
    ids = list(by_id.keys())
    log(f"Found {len(ids)} campaigns")

    try:
        analytics_list = client.get_campaign_analytics(ids)
    except Exception as e:
        log(f"ERROR fetching analytics: {e}")
        return 0

    log(f"Fetched analytics for {len(analytics_list)} campaigns from API")

    updated = 0
    for cid, camp in by_id.items():
        data = _match_analytics(analytics_list, cid)
        if not data:
            # No analytics row (e.g. draft campaigns that never sent) — still
            # refresh name/status with zeroed metrics so stale numbers from
            # past mismatches don't linger.
            data = {
                "campaign_id": cid,
                "campaign_name": camp.get("name") or camp.get("campaign_name"),
                "status": camp.get("status"),
            }
        upsert_campaign_analytics(conn, cid, data)
        updated += 1

    conn.commit()
    log(f"Updated campaign_analytics for {updated}/{len(ids)} campaigns")
    return updated


# ---------------------------------------------------------------------------
# backfill_all_campaigns
# ---------------------------------------------------------------------------

def backfill_all_campaigns(
    session: Optional[str] = None,
    callback: ProgressFn = None,
) -> int:
    """Backfill analytics and account metrics for ALL campaigns.

    Lists all campaigns from the API, then for each campaign syncs
    analytics and account metrics.

    Estimated time: ~2-4 hours for all campaigns (1 req/sec rate limit).

    Args:
        session: User slug.
        callback: Optional progress callback.

    Returns:
        Number of campaigns processed.
    """
    from .api import InstantlyClient
    from .users import UserRegistry

    log = callback or _default_progress
    user_slug = session or "default"

    registry = UserRegistry()
    user = registry.get_user(user_slug)
    if not user:
        log(f"ERROR: User '{user_slug}' not found.")
        return 0

    client = InstantlyClient(api_key=user.instantly_api_key)

    # List all campaigns
    log("Fetching campaign list...")
    try:
        all_campaigns = client.list_campaigns()
    except Exception as e:
        log(f"ERROR fetching campaigns: {e}")
        return 0

    if not all_campaigns:
        log("No campaigns found.")
        return 0

    log(f"Found {len(all_campaigns)} campaigns")

    processed = 0
    total = len(all_campaigns)
    start_time = time.time()

    for i, camp in enumerate(all_campaigns):
        cid = camp.get("campaign_id", camp.get("id", ""))
        cname = camp.get("campaign_name", camp.get("name", f"campaign_{cid[:8]}"))
        elapsed = time.time() - start_time
        log(f"\n  [{i+1}/{total}] {cname} ({cid[:12]}...) — elapsed: {elapsed:.0f}s")

        try:
            sync_campaign_analytics(cid, session=user_slug, callback=callback)
            processed += 1
        except Exception as e:
            log(f"  ERROR processing campaign {cid[:12]}: {e}")

        # Rate-limit pause between campaigns
        time.sleep(1.1)

    total_elapsed = time.time() - start_time
    log(f"\nDone: {processed}/{total} campaigns processed in {total_elapsed:.0f}s")
    return processed


# ---------------------------------------------------------------------------
# incremental_campaign_sync
# ---------------------------------------------------------------------------

def incremental_campaign_sync(
    campaign_id: str,
    session: Optional[str] = None,
    callback: ProgressFn = None,
) -> int:
    """Incrementally sync campaign account metrics since last sync.

    Uses cursor-based pagination (DESC from most recent) and stops when
    timestamp_created <= last_synced_at. This avoids the broken
    min_timestamp_created filter.

    Assumes idempotency of UPSERT for safe interrupt/resume.

    Args:
        campaign_id: Campaign UUID.
        session: User slug.
        callback: Optional progress callback.

    Returns:
        Number of new/updated account metrics.
    """
    from .api import InstantlyClient
    from .users import UserRegistry

    log = callback or _default_progress
    user_slug = session or "default"

    registry = UserRegistry()
    user = registry.get_user(user_slug)
    if not user:
        log(f"ERROR: User '{user_slug}' not found.")
        return 0

    client = InstantlyClient(api_key=user.instantly_api_key)
    conn = get_connection(user_slug)

    # Get last_synced_at from campaign_analytics
    row = conn.execute(
        "SELECT last_synced_at FROM campaign_analytics WHERE campaign_id = ?",
        (campaign_id,),
    ).fetchone()

    last_synced = row["last_synced_at"] if row and row["last_synced_at"] else None
    log(f"  Last synced: {last_synced or 'never'}")

    # If never synced, do full sync
    if not last_synced:
        log("  No prior sync — doing full sync")
        # First sync campaign analytics
        try:
            analytics_list = client.get_campaign_analytics([campaign_id])
            data = _match_analytics(analytics_list, campaign_id)
            if data:
                upsert_campaign_analytics(conn, campaign_id, data)
                conn.commit()
        except Exception as e:
            log(f"  WARNING: Could not fetch analytics: {e}")

        return sync_campaign_account_metrics(campaign_id, session=user_slug, callback=callback)

    # Incremental: paginate DESC and stop when timestamp <= last_synced
    log(f"  Incremental sync from {last_synced}")

    # First sync analytics (cheap single call)
    try:
        analytics_list = client.get_campaign_analytics([campaign_id])
        data = _match_analytics(analytics_list, campaign_id)
        if data:
            upsert_campaign_analytics(conn, campaign_id, data)
            conn.commit()
    except Exception as e:
        log(f"  WARNING: Could not fetch analytics: {e}")

    # Per-account counters for new records
    # Key: account_email, Value: {"sent": int, "replies": int, "last_activity": str, "most_recent_received": dict|None}
    accounts: dict[str, dict] = {}

    def _process_new_emails(ue_type: int, label: str):
        """Fetch new emails since last sync using cursor pagination."""
        starting_after: Optional[str] = None
        page = 0
        reached_boundary = False

        while not reached_boundary:
            page += 1
            try:
                data = client.get_emails(
                    campaign_id=campaign_id,
                    email_type="sent" if ue_type == 1 else "received",
                    limit=100,
                    starting_after=starting_after,
                    sort_by="timestamp_created",
                    sort_order="desc",
                )
            except Exception as e:
                log(f"    ERROR fetching {label} page {page}: {e}")
                break

            items = data.get("items", [])
            if not items:
                break

            for item in items:
                ts = item.get("timestamp_created") or item.get("created_at", "")

                # Stop when we hit data older than last sync
                if ts and last_synced and ts <= last_synced:
                    reached_boundary = True
                    break

                eaccount = item.get("eaccount", "")
                if not eaccount:
                    continue

                if eaccount not in accounts:
                    accounts[eaccount] = {"sent": 0, "replies": 0, "last_activity": None, "most_recent_received": None, "most_recent_received_ts": None}

                if ue_type == 1:
                    accounts[eaccount]["sent"] += 1
                else:
                    accounts[eaccount]["replies"] += 1
                    # Track the most recent received email by timestamp_created
                    # Use a dedicated field, not last_activity (which is also set by sent emails)
                    ts = item.get("timestamp_created") or item.get("created_at")
                    current_recv_ts = accounts[eaccount]["most_recent_received_ts"]
                    if current_recv_ts is None or (ts and ts > current_recv_ts):
                        accounts[eaccount]["most_recent_received"] = item
                        accounts[eaccount]["most_recent_received_ts"] = ts

                if ts:
                    current = accounts[eaccount]["last_activity"]
                    if current is None or ts > current:
                        accounts[eaccount]["last_activity"] = ts

            if reached_boundary:
                break

            starting_after = data.get("next_starting_after")
            if not starting_after:
                break

    _process_new_emails(1, "sent")
    _process_new_emails(2, "received")

    if not accounts:
        log("  No new email records since last sync")
        return 0

    # UPSERT
    log(f"  Upserting {len(accounts)} account metrics (incremental)...")
    for email, data in accounts.items():
        # Extract subject/i_status from most recent received email if available
        subject_line = None
        i_status = None
        classification = None
        recv_item = data.get("most_recent_received")
        if recv_item:
            subject_line = (recv_item.get("subject") or "").strip() or None
            i_status = recv_item.get("i_status")
            classification = classify_email(recv_item)

        ensure_account_exists(conn, email)
        upsert_campaign_account_metrics(
            conn=conn,
            campaign_id=campaign_id,
            account_email=email,
            total_sent=data["sent"],
            replies=data["replies"],
            last_activity_at=data["last_activity"],
            subject_line=subject_line,
            i_status=i_status,
            classification=classification,
        )

    conn.commit()
    log(f"  Incremental sync complete: {len(accounts)} accounts updated")
    return len(accounts)
