"""CLI entry point — click commands for run, report, detail, trend, accounts."""

import sys
from typing import Optional

import click

from . import __version__
from .config import settings
from .db import init_db, close_connection


# ------------------------------------------------------------------
# Shared options
# ------------------------------------------------------------------

_date_range_opts = [
    click.option("--min-date", "-f", help="Start date (YYYY-MM-DD)"),
    click.option("--max-date", "-t", help="End date (YYYY-MM-DD)"),
]


def _with_date_range(func):
    for opt in reversed(_date_range_opts):
        func = opt(func)
    return func


# ------------------------------------------------------------------
# detect-providers
# ------------------------------------------------------------------

@click.command()
@click.option(
    "--force",
    is_flag=True,
    help="Re-detect all domains even if provider is already set",
)
def detect_providers(force: bool) -> None:
    """Detect email providers (Gmail/Outlook/Other) for domains via DNS MX lookup."""
    from .etl import batch_detect_providers

    try:
        init_db()
        updated = batch_detect_providers(force=force)
        if updated > 0:
            click.echo(f"Done: updated {updated} domains.")
        else:
            click.echo("No domains needed updating.")
    finally:
        close_connection()


# ------------------------------------------------------------------
# run
# ------------------------------------------------------------------

@click.command()
@click.option(
    "--user", "-u",
    default="default",
    show_default=True,
    help="User slug (e.g. 'default', 'client_acme')",
)
@click.option(
    "--skip-campaigns",
    is_flag=True,
    help="Skip campaign mapping refresh for faster daily runs",
)
@click.option(
    "--skip-campaign-sync",
    is_flag=True,
    help="Skip campaign account metrics sync",
)
@click.option(
    "--full-backfill",
    is_flag=True,
    help="Pull from BACKFILL_START_DATE (default: 2026-01-01) regardless "
         "of existing data",
)
@click.option(
    "--no-report",
    is_flag=True,
    help="Skip showing report after ETL completes",
)
def run(
    user: str,
    skip_campaigns: bool,
    skip_campaign_sync: bool,
    full_backfill: bool,
    no_report: bool,
) -> None:
    """Run the full daily ETL pipeline, then show the domain health report."""
    from .etl import run_etl

    try:
        classified = run_etl(
            user_slug=user,
            skip_campaigns=skip_campaigns,
            skip_campaign_sync=skip_campaign_sync,
            full_backfill=full_backfill,
        )
    finally:
        close_connection()

    if not no_report and classified:
        # Show report after ETL
        from .report import print_report

        print_report()


# ------------------------------------------------------------------
# report
# ------------------------------------------------------------------

@click.command()
@click.option("--window", "-w", type=int, default=None, help="Lookback days")
@click.option("--domain", "-d", help="Filter to a specific domain")
@_with_date_range
def report(
    window: Optional[int],
    domain: Optional[str],
    min_date: Optional[str],
    max_date: Optional[str],
) -> None:
    """Show per-domain health report (reads from DB, no API calls)."""
    try:
        init_db()
    finally:
        pass

    from .report import print_report

    try:
        print_report(
            window_days=window,
            domain_filter=domain,
            min_date=min_date,
            max_date=max_date,
        )
    finally:
        close_connection()


# ------------------------------------------------------------------
# detail
# ------------------------------------------------------------------

@click.command()
@click.option("--domain", "-d", required=True, help="Domain to drill into")
@click.option("--window", "-w", type=int, default=None, help="Lookback days")
@_with_date_range
def detail(
    domain: str,
    window: Optional[int],
    min_date: Optional[str],
    max_date: Optional[str],
) -> None:
    """Show per-mailbox drill-down for a specific domain."""
    try:
        init_db()
    finally:
        pass

    from .report import print_detail

    try:
        print_detail(
            domain=domain,
            window_days=window,
            min_date=min_date,
            max_date=max_date,
        )
    finally:
        close_connection()


# ------------------------------------------------------------------
# trend
# ------------------------------------------------------------------

@click.command()
@click.option("--domain", "-d", required=True, help="Domain to show trend for")
@click.option("--days", type=int, default=30, help="Number of days (default: 30)")
@_with_date_range
def trend(
    domain: str,
    days: int,
    min_date: Optional[str],
    max_date: Optional[str],
) -> None:
    """Show daily trend for a domain."""
    try:
        init_db()
    finally:
        pass

    from .report import print_trend

    try:
        print_trend(
            domain=domain,
            days=days,
            min_date=min_date,
            max_date=max_date,
        )
    finally:
        close_connection()


# ------------------------------------------------------------------
# accounts
# ------------------------------------------------------------------

@click.command()
@click.option("--domain", "-d", help="Filter to a specific domain")
def accounts(domain: Optional[str]) -> None:
    """List all tracked accounts."""
    try:
        init_db()
    finally:
        pass

    from .report import print_accounts

    try:
        print_accounts(domain_filter=domain)
    finally:
        close_connection()


# ------------------------------------------------------------------
# campaigns
# ------------------------------------------------------------------

@click.command()
@click.option("--name", "-n", help="Filter by campaign name (LIKE match)")
@click.option("--window", "-w", type=int, default=None, help="Lookback days")
def campaigns(name: Optional[str], window: Optional[int]) -> None:
    """Show campaign-level analytics with per-campaign metrics.

    Groups accounts by campaign and aggregates sent/replies.
    Helps identify copy issues vs domain issues — if every domain
    in a campaign has 0% reply rate, it's likely a copy problem.
    """
    try:
        init_db()
    finally:
        pass

    from .report import print_campaigns

    try:
        print_campaigns(
            campaign_name_filter=name,
            window_days=window,
        )
    finally:
        close_connection()


# ------------------------------------------------------------------
# version
# ------------------------------------------------------------------

@click.command()
def version() -> None:
    """Show the installed version."""
    click.echo(f"instantly-domain-tracker v{__version__}")


# ------------------------------------------------------------------
# campaign-mailbox-refresh
# ------------------------------------------------------------------

@click.command()
@click.option(
    "--campaign-id", "-c",
    help="Single campaign ID to refresh (omit for all campaigns)",
)
@click.option(
    "--force",
    is_flag=True,
    help="Re-sync existing data (full backfill)",
)
@click.option(
    "--user", "-u",
    default="default",
    show_default=True,
    help="User slug",
)
def campaign_mailbox_refresh(
    campaign_id: Optional[str],
    force: bool,
    user: str,
) -> None:
    """Backfill per-account email metrics for campaigns.

    Fetches all sent/received emails per campaign and aggregates
    per-account metrics (sent count, reply count).
    """
    from .campaign_etl import (
        backfill_all_campaigns,
        sync_campaign_analytics,
    )

    def _callback(msg):
        click.echo(msg)

    start = __import__("time").time()

    try:
        init_db(user)

        if campaign_id:
            click.echo(f"\n📬 Refreshing campaign {campaign_id[:12]}...")
            sync_campaign_analytics(campaign_id, session=user, callback=_callback)
        else:
            click.echo("\n📬 Backfilling all campaigns...")
            click.echo("   ⚠️  This may take 2-4 hours (1 req/sec rate limit).")
            processed = backfill_all_campaigns(session=user, callback=_callback)
            click.echo(f"\n✅ Processed {processed} campaigns")

        elapsed = __import__("time").time() - start
        click.echo(f"\n⏱️  Completed in {elapsed:.0f}s")

    except Exception as e:
        click.echo(f"\n❌ Error: {e}", err=True)
        import traceback
        traceback.print_exc()
    finally:
        close_connection()


# ------------------------------------------------------------------
# campaign-overview-refresh
# ------------------------------------------------------------------

@click.command()
@click.option(
    "--user", "-u",
    default="default",
    show_default=True,
    help="User slug",
)
def campaign_overview_refresh(user: str) -> None:
    """Refresh campaign_analytics (name/leads/sent/reply-rate) for every campaign.

    Fast, homepage-only sync — does not pull per-mailbox detail data
    (use campaign-mailbox-refresh for that).
    """
    from .campaign_etl import sync_all_campaign_overviews

    def _callback(msg):
        click.echo(msg)

    start = __import__("time").time()

    try:
        init_db(user)
        click.echo("\n📊 Refreshing campaign overviews...")
        updated = sync_all_campaign_overviews(session=user, callback=_callback)
        click.echo(f"\n✅ Updated {updated} campaigns")

        elapsed = __import__("time").time() - start
        click.echo(f"\n⏱️  Completed in {elapsed:.0f}s")

    except Exception as e:
        click.echo(f"\n❌ Error: {e}", err=True)
        import traceback
        traceback.print_exc()
    finally:
        close_connection()


# ------------------------------------------------------------------
# campaign-accounts
# ------------------------------------------------------------------

@click.command()
@click.option(
    "--campaign-id", "-c",
    required=True,
    help="Campaign ID to show accounts for",
)
@click.option(
    "--user", "-u",
    default="default",
    show_default=True,
    help="User slug",
)
def campaign_accounts(campaign_id: str, user: str) -> None:
    """Show per-account breakdown for a campaign.

    Reads from campaign_account_metrics and campaign_analytics tables.
    Uses Rich console for formatted table output.
    """
    try:
        init_db(user)
    finally:
        pass

    from .db import get_connection as _get_connection
    conn = _get_connection(user)

    # Get campaign name
    ca = conn.execute(
        "SELECT campaign_name, campaign_status FROM campaign_analytics WHERE campaign_id = ?",
        (campaign_id,),
    ).fetchone()

    if not ca:
        click.echo(f"No analytics found for campaign {campaign_id[:12]}...")
        click.echo("Run 'campaign-mailbox-refresh' first to populate data.")
        close_connection()
        return

    camp_name = ca["campaign_name"] or campaign_id[:12]
    status = ca["campaign_status"]

    # Get per-account metrics
    rows = conn.execute(
        """SELECT account_email, total_sent, replies, last_activity_at, last_synced_at
           FROM campaign_account_metrics
           WHERE campaign_id = ?
           ORDER BY total_sent DESC""",
        (campaign_id,),
    ).fetchall()

    if not rows:
        click.echo(f"No account metrics found for campaign '{camp_name}'")
        click.echo("Run 'campaign-mailbox-refresh' first to populate data.")
        close_connection()
        return

    # Build table data
    table_data = []
    for r in rows:
        sent = r["total_sent"] or 0
        replies = r["replies"] or 0
        reply_rate = round((replies / sent * 100), 2) if sent > 0 else 0.0
        last_activity = r["last_activity_at"] or "—"
        if last_activity and last_activity != "—":
            try:
                last_activity = last_activity[:10]  # Just the date part
            except Exception:
                pass

        table_data.append({
            "email": r["account_email"],
            "sent": sent,
            "replies": replies,
            "reply_rate": reply_rate,
            "last_activity": last_activity,
        })

    # Use Rich console
    from rich.console import Console
    from rich.table import Table

    console = Console()
    console.print(f"\n📊 [bold]Campaign: {camp_name}[/bold]")
    console.print(f"   Status: {status or 'N/A'} | Accounts: {len(table_data)}")

    table = Table(title=f"Account Metrics ({len(table_data)} accounts)")
    table.add_column("Email", style="cyan", no_wrap=True)
    table.add_column("Sent", justify="right", style="magenta")
    table.add_column("Replies", justify="right", style="green")
    table.add_column("Reply %", justify="right", style="yellow")
    table.add_column("Last Activity", style="white")

    for d in table_data:
        table.add_row(
            d["email"],
            str(d["sent"]),
            str(d["replies"]),
            f"{d['reply_rate']}%",
            d["last_activity"],
        )

    console.print(table)
    console.print(f"\nTotal sent: {sum(d['sent'] for d in table_data)}")
    console.print(f"Total replies: {sum(d['replies'] for d in table_data)}")

    close_connection()


# ------------------------------------------------------------------
# Main CLI group
# ------------------------------------------------------------------

class DomainTrackerCLI(click.MultiCommand):
    """Custom group that lazy-loads commands to speed up help."""

# ------------------------------------------------------------------
# add-user
# ------------------------------------------------------------------

@click.command()
@click.option("--slug", required=True, help="User slug (e.g. 'client_acme')")
@click.option("--name", "-n", required=True, help="Display name")
@click.option("--api-key", "-k", required=True, help="Instantly API key")
@click.option("--admin", is_flag=True, help="Mark as admin user")
def add_user(slug: str, name: str, api_key: str, admin: bool) -> None:
    """Add a new user to the registry."""
    from .users import User, UserRegistry

    slug = slug.lower().replace(" ", "_")
    registry = UserRegistry()

    if registry.get_user(slug):
        click.echo(f"Error: User '{slug}' already exists.", err=True)
        sys.exit(1)

    # Validate API key by calling list_accounts
    from .api import InstantlyClient, InstantlyAPIError

    try:
        client = InstantlyClient(api_key=api_key)
        accounts = client.list_accounts()
        click.echo(f"  API key valid — {len(accounts)} accounts found.")
    except InstantlyAPIError as e:
        click.echo(f"Error: Invalid API key: {e}", err=True)
        sys.exit(1)
    except Exception as e:
        click.echo(f"Warning: Could not validate API key: {e}", err=True)
        click.echo("  Adding user anyway...")

    user = User(
        id=slug,
        name=name,
        instantly_api_key=api_key,
        is_admin=admin,
        active=True,
    )
    registry.add_user(user)
    click.echo(f"Added user '{slug}' ({name})")

    # Init DB
    from .db import init_db
    init_db(slug)
    click.echo(f"  Database initialized: {user.db_path}")


# ------------------------------------------------------------------
# list-users
# ------------------------------------------------------------------

@click.command()
def list_users() -> None:
    """List all registered users."""
    from .users import UserRegistry

    registry = UserRegistry()
    users = registry.list_users()

    if not users:
        click.echo("No users registered.")
        return

    click.echo()
    click.echo(f"Users ({len(users)}):")
    click.echo("-" * 60)
    for u in users:
        status = "[active]" if u.active else "[inactive]"
        admin = "[admin]" if u.is_admin else ""
        db_exists = "✓" if u.db_path.exists() else "✗"
        click.echo(
            f"  {u.id:<20} {u.name:<25} {status:<10} {admin:<8} DB: {db_exists}"
        )
    click.echo()


# ------------------------------------------------------------------
# remove-user
# ------------------------------------------------------------------

@click.command()
@click.argument("slug")
def remove_user(slug: str) -> None:
    """Remove a user from the registry."""
    from .users import UserRegistry

    if slug == "default":
        click.echo("Error: Cannot remove the default user.", err=True)
        sys.exit(1)

    registry = UserRegistry()
    if not registry.get_user(slug):
        click.echo(f"Error: User '{slug}' not found.", err=True)
        sys.exit(1)

    registry.remove_user(slug)
    click.echo(f"Removed user '{slug}' from registry.")


# ------------------------------------------------------------------
# Main CLI group
# ------------------------------------------------------------------

class DomainTrackerCLI(click.MultiCommand):
    """Custom group that lazy-loads commands to speed up help."""

    def list_commands(self, ctx):
        return [
            "run",
            "report",
            "detail",
            "trend",
            "accounts",
            "campaigns",
            "campaign-overview-refresh",
            "campaign-mailbox-refresh",
            "campaign-accounts",
            "detect-providers",
            "add-user",
            "list-users",
            "remove-user",
            "version",
        ]

    def get_command(self, ctx, name):
        commands = {
            "run": run,
            "report": report,
            "detail": detail,
            "trend": trend,
            "accounts": accounts,
            "campaigns": campaigns,
            "campaign-overview-refresh": campaign_overview_refresh,
            "campaign-mailbox-refresh": campaign_mailbox_refresh,
            "campaign-accounts": campaign_accounts,
            "detect-providers": detect_providers,
            "add-user": add_user,
            "list-users": list_users,
            "remove-user": remove_user,
            "version": version,
        }
        return commands.get(name)


cli = DomainTrackerCLI(
    help="Instantly Domain Burn Detector — daily ETL + CLI for domain health."
)


if __name__ == "__main__":
    cli()
