"""CLI report formatting — rich tables and color-coded output."""

import json
from pathlib import Path
from typing import Dict, List, Optional

from rich.console import Console
from rich.table import Table
from rich.text import Text
from rich import box

from .analytics import (
    classify_domains,
    classify_domains_with_provider,
    get_account_campaigns,
    get_account_count,
    get_all_domains,
    get_data_date_range,
    get_domain_stats,
    get_domain_trend,
    get_mailbox_stats,
)
from .config import settings


console = Console()

# Path to settings.json
SETTINGS_JSON_PATH = Path(__file__).resolve().parent.parent / 'data' / 'settings.json'


# ------------------------------------------------------------------
# Settings.json loader (supplements config.py with persisted overrides)
# ------------------------------------------------------------------

_json_settings = None


def _load_json_settings() -> dict:
    """Load persisted thresholds from settings.json, cached after first read."""
    global _json_settings
    if _json_settings is not None:
        return _json_settings
    defaults: dict = {}
    try:
        with open(SETTINGS_JSON_PATH) as f:
            data = json.load(f)
            defaults.update(data)
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    _json_settings = defaults
    return defaults


def _get_provider_thresholds(provider: Optional[str]) -> tuple:
    """Return (bad_threshold, warn_threshold) for reply and bounce thresholds.

    Returns (burn_reply, at_risk_reply, burn_bounce, at_risk_bounce)
    based on provider and settings.json overrides.
    """
    js = _load_json_settings()

    if provider == "Gmail":
        prefix = "google"
    elif provider == "Outlook":
        prefix = "outlook"
    else:
        prefix = "other"

    burn_reply = float(js.get(f"{prefix}_burn_reply_rate", settings.BURN_REPLY_RATE_THRESHOLD))
    at_risk_reply = float(js.get(f"{prefix}_at_risk_reply_rate", settings.AT_RISK_REPLY_RATE_THRESHOLD))
    burn_bounce = float(js.get(f"{prefix}_burn_bounce_rate", settings.BURN_BOUNCE_RATE_THRESHOLD))
    at_risk_bounce = float(js.get(f"{prefix}_at_risk_bounce_rate", settings.AT_RISK_BOUNCE_RATE_THRESHOLD))
    return (burn_reply, at_risk_reply, burn_bounce, at_risk_bounce)


# ------------------------------------------------------------------
# Status styling helpers
# ------------------------------------------------------------------

STATUS_STYLES: Dict[str, Dict[str, str]] = {
    "BURNED": {
        "label": "🔥 BURNED",
        "style": "bold red",
    },
    "AT_RISK": {
        "label": "⚠ AT_RISK",
        "style": "bold yellow",
    },
    "RELATIVE_FLAG": {
        "label": "🚩 UNDERPERFORMING",
        "style": "italic yellow",
    },
    "HEALTHY": {
        "label": "✅ HEALTHY",
        "style": "bold green",
    },
}


def _status_cell(status: str) -> Text:
    info = STATUS_STYLES.get(status, STATUS_STYLES["HEALTHY"])
    return Text(info["label"], style=info["style"])


def _pct_cell(value: float, bad_threshold: float, warn_threshold: float) -> Text:
    """Color a percentage cell red/yellow/green based on thresholds."""
    if value >= warn_threshold:
        return Text(f"{value:.2f}%", style="green")
    elif value >= bad_threshold:
        return Text(f"{value:.2f}%", style="yellow")
    else:
        return Text(f"{value:.2f}%", style="red")


def _int_fmt(val: int) -> str:
    """Format integer with commas."""
    return f"{val:,}"


# ------------------------------------------------------------------
# Report: domain health table
# ------------------------------------------------------------------

def print_report(
    window_days: Optional[int] = None,
    domain_filter: Optional[str] = None,
    min_date: Optional[str] = None,
    max_date: Optional[str] = None,
) -> None:
    """Print the per-domain health report table."""
    win = window_days or settings.LOOKBACK_DAYS
    last_data = get_data_date_range()

    # Header
    console.print()
    console.print(
        f"[bold]Domain Health Report[/bold] — last {win} days",
        style="cyan",
    )
    if last_data[0] and last_data[1]:
        console.print(
            f"Data range: {last_data[0]} → {last_data[1]}",
            style="dim",
        )

    stats = get_domain_stats(
        window_days=win,
        min_date=min_date,
        max_date=max_date,
    )

    if domain_filter:
        stats = [d for d in stats if d["domain"] == domain_filter]

    if not stats:
        console.print(
            "\n[yellow]No domain data available yet. "
            "Run `python -m domain_tracker run` first.[/yellow]"
        )
        return

    classified = classify_domains_with_provider(stats)

    # Set up table
    table = Table(box=box.HEAVY)
    table.add_column("Domain", style="bold")
    table.add_column("Provider")
    table.add_column("Hosted")
    table.add_column("Accounts", justify="right")
    table.add_column("Sent", justify="right")
    table.add_column("All Replies", justify="right")
    table.add_column("Human", justify="right")
    table.add_column("Reply Rate", justify="right")
    table.add_column("Bounce Rate", justify="right")
    table.add_column("Status")

    for d in classified:
        provider_label = d.get("provider", "") or ""
        reply_bad, reply_warn, bounce_bad, bounce_warn = _get_provider_thresholds(d.get("provider"))
        hosted_country = d.get("hosted_country") or "—"
        table.add_row(
            d["domain"],
            provider_label,
            hosted_country,
            str(d["active_accounts"]),
            _int_fmt(d["total_sent"]),
            _int_fmt(d["total_replies"]),
            _int_fmt(d["human_replies"]),
            _pct_cell(d["reply_rate_pct"], bad_threshold=reply_bad, warn_threshold=reply_warn),
            _pct_cell(d["bounce_rate_pct"], bad_threshold=bounce_bad, warn_threshold=bounce_warn),
            _status_cell(d["status"]),
        )

    console.print(table)

    # Summarize
    burned = [d for d in classified if d["status"] == "BURNED"]
    at_risk = [d for d in classified if d["status"] == "AT_RISK"]
    flagged = [d for d in classified if d["status"] == "RELATIVE_FLAG"]

    if burned:
        console.print(
            f"\n[red]🔥 BURNED:[/red] "
            f"{', '.join(d['domain'] for d in burned)} — "
            f"consider retiring these domains"
        )
    if at_risk:
        console.print(
            f"\n[yellow]⚠ AT_RISK:[/yellow] "
            f"{', '.join(d['domain'] for d in at_risk)} — "
            f"monitor closely"
        )
    if flagged:
        console.print(
            f"\n[italic yellow]🚩 UNDERPERFORMING:[/italic yellow] "
            f"{', '.join(d['domain'] for d in flagged)} — "
            f"relative flags (below 30% of best domain)"
        )
    if not burned and not at_risk and not flagged:
        console.print("\n[green]All domains healthy ✅[/green]")

    min_sent = settings.MIN_VOLUME_THRESHOLD
    console.print(f"\n[dim]Minimum {min_sent} sent per domain to qualify.[/dim]")
    console.print()


# ------------------------------------------------------------------
# Detail: per-mailbox drill-down
# ------------------------------------------------------------------

def print_detail(
    domain: str,
    window_days: Optional[int] = None,
    min_date: Optional[str] = None,
    max_date: Optional[str] = None,
) -> None:
    """Print per-mailbox detail for a domain."""
    win = window_days or settings.LOOKBACK_DAYS

    # Domain summary
    stats = get_domain_stats(
        window_days=win,
        min_date=min_date,
        max_date=max_date,
    )
    domain_stats = [d for d in stats if d["domain"] == domain]

    if not domain_stats:
        console.print(
            f"[yellow]No data for domain '{domain}'[/yellow]"
        )
        return

    d = classify_domains_with_provider(domain_stats)[0]

    console.print()
    console.print(f"[bold]Domain:[/bold] {domain}")
    console.print(
        f"Status: {_status_cell(d['status'])}"
    )
    console.print(
        f"Reply rate: {d['reply_rate_pct']:.2f}%  |  "
        f"Bounce rate: {d['bounce_rate_pct']:.2f}%"
    )
    hosted_country = d.get("hosted_country")
    if hosted_country:
        console.print(f"Hosted country: [bold]{hosted_country}[/bold]")
    else:
        console.print("Hosted country: [dim]not detected[/dim]")

    # Campaigns
    # We need to get campaigns for accounts in this domain
    mailbox_data = get_mailbox_stats(
        domain=domain,
        window_days=win,
        min_date=min_date,
        max_date=max_date,
    )

    if not mailbox_data:
        console.print("  [dim]No mailbox data.[/dim]\n")
        return

    console.print(f"\nMailboxes ({len(mailbox_data)} active):")
    table = Table(box=box.SIMPLE)
    table.add_column("Email")
    table.add_column("Sent", justify="right")
    table.add_column("All Replies", justify="right")
    table.add_column("Human", justify="right")
    table.add_column("Reply Rate", justify="right")
    table.add_column("Bounce Rate", justify="right")

    default_reply_bad, default_reply_warn, default_bounce_bad, default_bounce_warn = _get_provider_thresholds(None)
    for m in mailbox_data:
        rr = _pct_cell(m["reply_rate_pct"], default_reply_bad, default_reply_warn)
        br = _pct_cell(m["bounce_rate_pct"], default_bounce_bad, default_bounce_warn)
        table.add_row(
            m["email"],
            _int_fmt(m["total_sent"]),
            _int_fmt(m["total_replies"]),
            _int_fmt(m["human_replies"]),
            rr,
            br,
        )

    console.print(table)

    # Show campaigns for first few mailboxes
    sample_emails = mailbox_data[:3]
    for m in sample_emails:
        campaigns = get_account_campaigns(m["email"])
        if campaigns:
            camp_names = ", ".join(
                f"{c['campaign_name']}"
                for c in campaigns
                if c.get("campaign_name")
            )
            console.print(
                f"  [dim]{m['email']}[/dim] → {camp_names}"
            )

    console.print()


# ------------------------------------------------------------------
# Trend: daily time series
# ------------------------------------------------------------------

def print_trend(
    domain: str,
    days: int = 30,
    min_date: Optional[str] = None,
    max_date: Optional[str] = None,
) -> None:
    """Print daily trend table for a domain."""
    trend = get_domain_trend(
        domain=domain,
        days=days,
        min_date=min_date,
        max_date=max_date,
    )

    if not trend:
        console.print(
            f"[yellow]No trend data for domain '{domain}'[/yellow]"
        )
        return

    console.print()
    console.print(
        f"[bold]Trend:[/bold] {domain} — "
        f"{trend[0]['date']} → {trend[-1]['date']} "
        f"({len(trend)} days)"
    )
    console.print()

    table = Table(box=box.SIMPLE)
    table.add_column("Date")
    table.add_column("Sent", justify="right")
    table.add_column("All Replies", justify="right")
    table.add_column("Reply Rate", justify="right")
    table.add_column("Bounce Rate", justify="right")
    table.add_column("Opened", justify="right")

    default_reply_bad, default_reply_warn, default_bounce_bad, default_bounce_warn = _get_provider_thresholds(None)
    for t in trend:
        rr = _pct_cell(t["reply_rate_pct"], default_reply_bad, default_reply_warn)
        br = _pct_cell(t["bounce_rate_pct"], default_bounce_bad, default_bounce_warn)
        table.add_row(
            t["date"],
            _int_fmt(t["total_sent"]),
            str(t["total_replies"]),
            rr,
            br,
            _int_fmt(t["total_unique_opened"]),
        )

    console.print(table)
    console.print()


# ------------------------------------------------------------------
# Accounts list
# ------------------------------------------------------------------

def print_accounts(
    domain_filter: Optional[str] = None,
) -> None:
    """Print a table of all tracked accounts."""
    from .db import get_connection

    conn = get_connection()
    query = """
        SELECT email, domain, status, warmup_status,
               tracking_domain, provider_code, provider, daily_limit,
               hosted_country
        FROM accounts
    """
    params = []
    if domain_filter:
        query += " WHERE domain = ?"
        params.append(domain_filter)
    query += " ORDER BY domain, email"

    rows = conn.execute(query, params).fetchall()

    if not rows:
        console.print(
            "[yellow]No accounts found. Run `python -m domain_tracker run` first.[/yellow]"
        )
        return

    console.print()
    console.print(f"[bold]Accounts[/bold] ({len(rows)} total)")
    console.print()

    table = Table(box=box.HEAVY)
    table.add_column("Email")
    table.add_column("Domain")
    table.add_column("Status")
    table.add_column("Warmup")
    table.add_column("Tracking Domain")
    table.add_column("Provider")
    table.add_column("DNS Provider")
    table.add_column("Hosted")

    STATUS_MAP = {
        1: "Active", 2: "Paused", 3: "Maintenance",
        -1: "ConnErr", -2: "SoftBounce", -3: "SendErr",
    }
    WARMUP_MAP = {
        0: "Paused", 1: "Active",
        -1: "Banned", -2: "SpamUnknown", -3: "PermSusp",
    }
    PROVIDER_MAP = {
        1: "IMAP", 2: "Google", 3: "Microsoft",
        4: "AWS", 8: "AirMail",
    }

    for r in rows:
        table.add_row(
            r["email"],
            r["domain"],
            STATUS_MAP.get(r["status"], str(r["status"])),
            WARMUP_MAP.get(r["warmup_status"], str(r["warmup_status"])),
            r["tracking_domain"] or "-",
            PROVIDER_MAP.get(r["provider_code"], str(r["provider_code"])),
            r["provider"] or "-",
            r["hosted_country"] or "-",
        )

    console.print(table)
    console.print(
        f"[dim]Total: {len(rows)} accounts "
        f"across {len(set(r['domain'] for r in rows))} domains[/dim]"
    )
    console.print()


# ------------------------------------------------------------------
# Campaigns table
# ------------------------------------------------------------------

def print_campaigns(
    campaign_name_filter: Optional[str] = None,
    window_days: Optional[int] = None,
) -> None:
    """Print campaign-level analytics table."""
    from .analytics import get_campaign_stats

    win = window_days or settings.LOOKBACK_DAYS
    last_data = get_data_date_range()

    stats = get_campaign_stats(
        lookback_days=win,
        campaign_name_filter=campaign_name_filter,
    )

    if not stats:
        console.print(
            "\n[yellow]No campaign data available. "
            "Run `python -m domain_tracker run` to fetch campaign mappings.[/yellow]"
        )
        # Suggest fallback if account_campaigns table is empty
        from .db import get_connection
        conn = get_connection()
        count = conn.execute(
            "SELECT COUNT(*) AS cnt FROM account_campaigns"
        ).fetchone()["cnt"]
        if count == 0:
            console.print(
                "[yellow]The account_campaigns table is empty. "
                "Run without --skip-campaigns to populate it.[/yellow]"
            )
        return

    console.print()
    console.print(
        f"[bold]Campaign Analytics[/bold] — last {win} days",
        style="cyan",
    )
    if last_data[0] and last_data[1]:
        console.print(
            f"Data range: {last_data[0]} → {last_data[1]}",
            style="dim",
        )

    table = Table(box=box.HEAVY)
    table.add_column("Campaign", style="bold")
    table.add_column("Domains", justify="right")
    table.add_column("Accounts", justify="right")
    table.add_column("Sent", justify="right")
    table.add_column("All Replies", justify="right")
    table.add_column("Human", justify="right")
    table.add_column("Reply Rate", justify="right")
    table.add_column("Bounce Rate", justify="right")
    table.add_column("Status")

    reply_bad, reply_warn, bounce_bad, bounce_warn = _get_provider_thresholds(None)

    for c in stats:
        table.add_row(
            c["campaign_name"] or c["campaign_id"],
            str(c["domain_count"]),
            str(c["account_count"]),
            _int_fmt(c["total_sent"]),
            _int_fmt(c["total_replies"]),
            _int_fmt(c["human_replies"]),
            _pct_cell(
                c["reply_rate_pct"],
                bad_threshold=reply_bad,
                warn_threshold=reply_warn,
            ),
            _pct_cell(
                c["bounce_rate_pct"],
                bad_threshold=bounce_bad,
                warn_threshold=bounce_warn,
            ),
            _status_cell(c["status"]),
        )

    console.print(table)

    # Summary counts
    burned = [c for c in stats if c["status"] == "BURNED"]
    at_risk = [c for c in stats if c["status"] == "AT_RISK"]
    healthy = [c for c in stats if c["status"] == "HEALTHY"]

    if burned:
        console.print(
            f"\n[red]🔥 BURNED campaigns:[/red] {len(burned)} "
            f"— {', '.join(c['campaign_name'] for c in burned)}"
        )
    if at_risk:
        console.print(
            f"\n[yellow]⚠ AT_RISK campaigns:[/yellow] {len(at_risk)} "
            f"— {', '.join(c['campaign_name'] for c in at_risk)}"
        )
    if not burned and not at_risk:
        console.print("\n[green]All campaigns healthy ✅[/green]")

    console.print(
        f"\n[dim]Minimum {settings.MIN_VOLUME_THRESHOLD} sent per campaign to qualify.[/dim]"
    )
    console.print()
