"""Analytics engine — domain stats, mailbox drill-down, burn classification."""

from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from .config import settings
from .db import get_connection


# ------------------------------------------------------------------
# Domain health summary
# ------------------------------------------------------------------

def _default_window() -> Tuple[str, str]:
    """Return (start_date, end_date) for the configured lookback window."""
    end = datetime.utcnow()
    start = end - timedelta(days=settings.LOOKBACK_DAYS)
    return start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")


# ------------------------------------------------------------------
# Common metric helpers
# ------------------------------------------------------------------

def _pct(numerator: int, denominator: int) -> float:
    """Safe percentage calculation."""
    if denominator <= 0:
        return 0.0
    return (numerator / denominator) * 100.0


# ------------------------------------------------------------------
# Domain stats query
# ------------------------------------------------------------------

def get_domain_stats(
    window_days: Optional[int] = None,
    min_volume: Optional[int] = None,
    min_date: Optional[str] = None,
    max_date: Optional[str] = None,
    user_slug: str = "default",
) -> List[dict]:
    """Aggregate daily_metrics by domain over the lookback window.

    Args:
        window_days: Lookback window in days (overrides config).
        min_volume: Minimum sent volume threshold (overrides config).
        min_date: Explicit start date (YYYY-MM-DD).
        max_date: Explicit end date (YYYY-MM-DD).
        user_slug: User identifier for multi-user DB support.

    Returns a list of dicts with fields matching the report schema.
    """
    win = window_days or settings.LOOKBACK_DAYS
    mv = min_volume or settings.MIN_VOLUME_THRESHOLD

    if min_date and max_date:
        start_date, end_date = min_date, max_date
    else:
        start_date, end_date = _default_window()

    conn = get_connection(user_slug)
    rows = conn.execute(
        """
        SELECT
            a.domain,
            COUNT(DISTINCT dm.email_account) AS active_accounts,
            SUM(dm.sent)                     AS total_sent,
            SUM(dm.bounced)                  AS total_bounced,
            SUM(dm.unique_replies) + COALESCE(SUM(dm.unique_replies_automatic), 0) AS total_replies,
            SUM(dm.unique_replies)           AS human_replies,
            SUM(dm.unique_opened)            AS total_unique_opened,
            SUM(dm.opened)                   AS total_opened,
            SUM(dm.unique_clicks)            AS total_clicks,
            MAX(a.hosted_country)            AS hosted_country
        FROM daily_metrics dm
        JOIN accounts a ON a.email = dm.email_account
        WHERE dm.date >= ? AND dm.date <= ?
          AND a.status = 1
        GROUP BY a.domain
        HAVING total_sent >= ?
        ORDER BY a.domain
        """,
        (start_date, end_date, mv),
    ).fetchall()

    results = []
    for row in rows:
        reply_rate = _pct(row["total_replies"], row["total_sent"])
        bounce_rate = _pct(row["total_bounced"], row["total_sent"])

        results.append(
            {
                "domain": row["domain"],
                "active_accounts": row["active_accounts"],
                "total_sent": row["total_sent"],
                "total_replies": row["total_replies"],
                "human_replies": row["human_replies"],
                "total_bounced": row["total_bounced"],
                "total_unique_opened": row["total_unique_opened"],
                "total_clicks": row["total_clicks"],
                "reply_rate_pct": round(reply_rate, 2),
                "bounce_rate_pct": round(bounce_rate, 2),
                "hosted_country": row["hosted_country"],
            }
        )

    return results


# ------------------------------------------------------------------
# Burn classification
# ------------------------------------------------------------------

Classifier = Dict[str, Any]


def _get_provider_thresholds(provider: str) -> dict:
    """Return provider-specific burn thresholds."""
    if provider == "Gmail":
        return {
            "burn_reply": settings.GOOGLE_BURN_REPLY_RATE,
            "at_risk_reply": settings.GOOGLE_AT_RISK_REPLY_RATE,
            "burn_bounce": settings.GOOGLE_BURN_BOUNCE_RATE,
            "at_risk_bounce": settings.GOOGLE_AT_RISK_BOUNCE_RATE,
        }
    if provider == "Outlook":
        return {
            "burn_reply": settings.OUTLOOK_BURN_REPLY_RATE,
            "at_risk_reply": settings.OUTLOOK_AT_RISK_REPLY_RATE,
            "burn_bounce": settings.OUTLOOK_BURN_BOUNCE_RATE,
            "at_risk_bounce": settings.OUTLOOK_AT_RISK_BOUNCE_RATE,
        }
    # Other — use global defaults
    return {
        "burn_reply": settings.BURN_REPLY_RATE_THRESHOLD,
        "at_risk_reply": settings.AT_RISK_REPLY_RATE_THRESHOLD,
        "burn_bounce": settings.BURN_BOUNCE_RATE_THRESHOLD,
        "at_risk_bounce": settings.AT_RISK_BOUNCE_RATE_THRESHOLD,
    }


def classify_domain(
    reply_rate: float,
    bounce_rate: float,
    best_domain_reply_rate: Optional[float] = None,
    provider: Optional[str] = None,
) -> str:
    """Classify a domain as BURNED / AT_RISK / HEALTHY / RELATIVE_FLAG.

    Args:
        reply_rate: percentage (0-100)
        bounce_rate: percentage (0-100)
        best_domain_reply_rate: best rate among peers (for relative check)
        provider: 'Gmail', 'Outlook', or None (uses provider-specific thresholds)

    Returns:
        One of "BURNED", "AT_RISK", "HEALTHY", "RELATIVE_FLAG"
    """
    thresholds = _get_provider_thresholds(provider or "Other")

    # Absolute thresholds — BURNED takes priority
    if reply_rate < thresholds["burn_reply"]:
        return "BURNED"
    if bounce_rate > thresholds["burn_bounce"]:
        return "BURNED"

    # AT_RISK
    if reply_rate < thresholds["at_risk_reply"]:
        return "AT_RISK"
    if bounce_rate > thresholds["at_risk_bounce"]:
        return "AT_RISK"

    # Relative underperformer check
    if (
        best_domain_reply_rate is not None
        and best_domain_reply_rate > 0
        and reply_rate < (best_domain_reply_rate * settings.RELATIVE_RATE_MULTIPLIER)
        and reply_rate < settings.RELATIVE_MAX_REPLY_RATE
    ):
        return "RELATIVE_FLAG"

    return "HEALTHY"


def classify_domains(
    domain_stats: List[dict],
) -> List[dict]:
    """Add status classification to domain stats.

    Each domain dict gets a 'status' key added.
    """
    if not domain_stats:
        return []

    # Find best reply rate for relative comparison
    best_rate = max(d["reply_rate_pct"] for d in domain_stats)

    result = []
    for d in domain_stats:
        status = classify_domain(
            reply_rate=d["reply_rate_pct"],
            bounce_rate=d["bounce_rate_pct"],
            best_domain_reply_rate=best_rate,
        )
        d["status"] = status
        result.append(d)

    return result


# ------------------------------------------------------------------
# Mailbox drill-down
# ------------------------------------------------------------------

def classify_domains_with_provider(
    domain_stats: List[dict],
    user_slug: str = "default",
) -> List[dict]:
    """Add provider-aware status classification to domain stats.

    Fetches provider from accounts table for each domain.
    Each domain dict gets a 'status' and 'provider' key added.

    Args:
        domain_stats: List of domain stats dicts from get_domain_stats().
        user_slug: User identifier for multi-user DB support.
    """
    if not domain_stats:
        return []

    # Build {domain: provider} lookup
    conn = get_connection(user_slug)
    rows = conn.execute(
        "SELECT DISTINCT domain, provider FROM accounts"
    ).fetchall()
    domain_provider = {
        r["domain"]: r["provider"] or "Other"
        for r in rows
    }

    # Find best reply rate for relative comparison
    best_rate = max(d["reply_rate_pct"] for d in domain_stats)

    result = []
    for d in domain_stats:
        provider = domain_provider.get(d["domain"], "Other")
        status = classify_domain(
            reply_rate=d["reply_rate_pct"],
            bounce_rate=d["bounce_rate_pct"],
            best_domain_reply_rate=best_rate,
            provider=provider,
        )
        d["status"] = status
        d["provider"] = provider
        result.append(d)

    return result


def get_mailbox_stats(
    domain: str,
    window_days: Optional[int] = None,
    min_date: Optional[str] = None,
    max_date: Optional[str] = None,
    user_slug: str = "default",
) -> List[dict]:
    """Per-mailbox stats for a specific domain.

    Args:
        domain: Domain to analyze.
        window_days: Lookback window in days (overrides config).
        min_date: Explicit start date (YYYY-MM-DD).
        max_date: Explicit end date (YYYY-MM-DD).
        user_slug: User identifier for multi-user DB support.
    """
    win = window_days or settings.LOOKBACK_DAYS

    if min_date and max_date:
        start_date, end_date = min_date, max_date
    else:
        start_date, end_date = _default_window()

    conn = get_connection(user_slug)
    rows = conn.execute(
        """
        SELECT
            dm.email_account,
            SUM(dm.sent)              AS total_sent,
            SUM(dm.bounced)           AS total_bounced,
            SUM(dm.unique_replies) + COALESCE(SUM(dm.unique_replies_automatic), 0) AS total_replies,
            SUM(dm.unique_replies)    AS human_replies,
            SUM(dm.unique_opened)     AS total_unique_opened
        FROM daily_metrics dm
        JOIN accounts a ON a.email = dm.email_account
        WHERE a.domain = ?
          AND dm.date >= ? AND dm.date <= ?
          AND a.status = 1
        GROUP BY dm.email_account
        ORDER BY dm.email_account
        """,
        (domain, start_date, end_date),
    ).fetchall()

    results = []
    for row in rows:
        reply_rate = _pct(row["total_replies"], row["total_sent"])
        bounce_rate = _pct(row["total_bounced"], row["total_sent"])
        results.append(
            {
                "email": row["email_account"],
                "total_sent": row["total_sent"],
                "total_replies": row["total_replies"],
                "human_replies": row["human_replies"],
                "total_bounced": row["total_bounced"],
                "reply_rate_pct": round(reply_rate, 2),
                "bounce_rate_pct": round(bounce_rate, 2),
                "total_unique_opened": row["total_unique_opened"],
            }
        )

    return results


# ------------------------------------------------------------------
# Trend data
# ------------------------------------------------------------------

def get_domain_trend(
    domain: str,
    days: int = 30,
    min_date: Optional[str] = None,
    max_date: Optional[str] = None,
    user_slug: str = "default",
) -> List[dict]:
    """Daily aggregate metrics for a domain over N days.

    Returns one row per date with domain-wide sent/replies/reply_rate.

    Args:
        domain: Domain to analyze.
        days: Number of days to look back.
        min_date: Explicit start date (YYYY-MM-DD).
        max_date: Explicit end date (YYYY-MM-DD).
        user_slug: User identifier for multi-user DB support.
    """
    if min_date and max_date:
        start_date, end_date = min_date, max_date
    else:
        end = datetime.utcnow()
        start = end - timedelta(days=days)
        start_date = start.strftime("%Y-%m-%d")
        end_date = end.strftime("%Y-%m-%d")

    conn = get_connection(user_slug)
    rows = conn.execute(
        """
        SELECT
            dm.date,
            SUM(dm.sent)           AS total_sent,
            SUM(dm.unique_replies) + COALESCE(SUM(dm.unique_replies_automatic), 0) AS total_replies,
            SUM(dm.unique_replies) AS human_replies,
            SUM(dm.bounced)        AS total_bounced,
            SUM(dm.unique_opened)  AS total_unique_opened
        FROM daily_metrics dm
        JOIN accounts a ON a.email = dm.email_account
        WHERE a.domain = ?
          AND dm.date >= ? AND dm.date <= ?
        GROUP BY dm.date
        ORDER BY dm.date
        """,
        (domain, start_date, end_date),
    ).fetchall()

    results = []
    for row in rows:
        reply_rate = _pct(row["total_replies"], row["total_sent"])
        bounce_rate = _pct(row["total_bounced"], row["total_sent"])
        results.append(
            {
                "date": row["date"],
                "total_sent": row["total_sent"],
                "total_replies": row["total_replies"],
                "human_replies": row["human_replies"],
                "total_bounced": row["total_bounced"],
                "reply_rate_pct": round(reply_rate, 2),
                "bounce_rate_pct": round(bounce_rate, 2),
                "total_unique_opened": row["total_unique_opened"],
            }
        )

    return results


# ------------------------------------------------------------------
# Campaign info for an account
# ------------------------------------------------------------------

def get_account_campaigns(email: str, user_slug: str = "default") -> List[dict]:
    """Return campaign mappings for a given email account."""
    conn = get_connection(user_slug)
    rows = conn.execute(
        """
        SELECT campaign_id, campaign_name, campaign_status
        FROM account_campaigns
        WHERE email_account = ?
        ORDER BY campaign_name
        """,
        (email,),
    ).fetchall()
    return [dict(r) for r in rows]


# ------------------------------------------------------------------
# Campaign-level analytics
# ------------------------------------------------------------------

def get_campaign_stats(
    lookback_days: Optional[int] = None,
    campaign_name_filter: Optional[str] = None,
    user_slug: str = "default",
) -> List[dict]:
    """Aggregate daily_metrics by campaign via account_campaigns join.

    Returns per-campaign: campaign_id, campaign_name, domain_count,
    account_count, total_sent, total_replies, total_bounced,
    reply_rate_pct, bounce_rate_pct, status.
    """
    win = lookback_days or settings.LOOKBACK_DAYS
    end = datetime.utcnow()
    start = end - timedelta(days=win)
    start_date = start.strftime("%Y-%m-%d")
    end_date = end.strftime("%Y-%m-%d")

    conn = get_connection(user_slug)

    query = """
        SELECT
            ac.campaign_id,
            ac.campaign_name,
            COUNT(DISTINCT a.domain)             AS domain_count,
            COUNT(DISTINCT dm.email_account)     AS account_count,
            SUM(dm.sent)                         AS total_sent,
            SUM(dm.unique_replies) + COALESCE(SUM(dm.unique_replies_automatic), 0) AS total_replies,
            SUM(dm.unique_replies)               AS human_replies,
            SUM(dm.bounced)                      AS total_bounced,
            SUM(dm.unique_opened)                AS total_unique_opened
        FROM account_campaigns ac
        JOIN daily_metrics dm ON dm.email_account = ac.email_account
        JOIN accounts a ON a.email = ac.email_account
        WHERE dm.date >= ? AND dm.date <= ?
          AND a.status = 1
    """
    params: List = [start_date, end_date]

    if campaign_name_filter:
        query += " AND ac.campaign_name LIKE ?"
        params.append(f"%{campaign_name_filter}%")

    query += """
        GROUP BY ac.campaign_id, ac.campaign_name
        HAVING total_sent >= ?
        ORDER BY ac.campaign_name
    """
    params.append(settings.MIN_VOLUME_THRESHOLD)

    rows = conn.execute(query, params).fetchall()

    results = []
    for row in rows:
        reply_rate = _pct(row["total_replies"], row["total_sent"])
        bounce_rate = _pct(row["total_bounced"], row["total_sent"])

        # Use same burn classification thresholds applied at campaign level
        status = classify_domain(
            reply_rate=reply_rate,
            bounce_rate=bounce_rate,
        )

        results.append({
            "campaign_id": row["campaign_id"],
            "campaign_name": row["campaign_name"],
            "domain_count": row["domain_count"],
            "account_count": row["account_count"],
            "total_sent": row["total_sent"],
            "total_replies": row["total_replies"],
            "human_replies": row["human_replies"],
            "total_bounced": row["total_bounced"],
            "total_unique_opened": row["total_unique_opened"],
            "reply_rate_pct": round(reply_rate, 2),
            "bounce_rate_pct": round(bounce_rate, 2),
            "status": status,
        })

    return results


def get_campaign_domains(
    campaign_id: str,
    lookback_days: Optional[int] = None,
    user_slug: str = "default",
) -> List[dict]:
    """Return per-domain breakdown for accounts in a specific campaign."""
    win = lookback_days or settings.LOOKBACK_DAYS
    end = datetime.utcnow()
    start = end - timedelta(days=win)
    start_date = start.strftime("%Y-%m-%d")
    end_date = end.strftime("%Y-%m-%d")

    conn = get_connection(user_slug)
    rows = conn.execute(
        """
        SELECT
            a.domain,
            COUNT(DISTINCT dm.email_account)     AS active_accounts,
            SUM(dm.sent)                         AS total_sent,
            SUM(dm.unique_replies) + COALESCE(SUM(dm.unique_replies_automatic), 0) AS total_replies,
            SUM(dm.unique_replies)               AS human_replies,
            SUM(dm.bounced)                      AS total_bounced,
            SUM(dm.unique_opened)                AS total_unique_opened
        FROM account_campaigns ac
        JOIN daily_metrics dm ON dm.email_account = ac.email_account
        JOIN accounts a ON a.email = ac.email_account
        WHERE ac.campaign_id = ?
          AND dm.date >= ? AND dm.date <= ?
          AND a.status = 1
        GROUP BY a.domain
        HAVING total_sent >= ?
        ORDER BY a.domain
        """,
        (campaign_id, start_date, end_date, settings.MIN_VOLUME_THRESHOLD),
    ).fetchall()

    results = []
    for row in rows:
        reply_rate = _pct(row["total_replies"], row["total_sent"])
        bounce_rate = _pct(row["total_bounced"], row["total_sent"])
        results.append({
            "domain": row["domain"],
            "active_accounts": row["active_accounts"],
            "total_sent": row["total_sent"],
            "total_replies": row["total_replies"],
            "human_replies": row["human_replies"],
            "total_bounced": row["total_bounced"],
            "total_unique_opened": row["total_unique_opened"],
            "reply_rate_pct": round(reply_rate, 2),
            "bounce_rate_pct": round(bounce_rate, 2),
        })

    return results


# ------------------------------------------------------------------
# Utility helpers
# ------------------------------------------------------------------

def get_all_domains(user_slug: str = "default") -> List[str]:
    """Return sorted list of all tracked domains."""
    conn = get_connection(user_slug)
    rows = conn.execute(
        "SELECT DISTINCT domain FROM accounts ORDER BY domain"
    ).fetchall()
    return [r["domain"] for r in rows]


def get_account_count(user_slug: str = "default") -> int:
    """Total number of tracked accounts."""
    conn = get_connection(user_slug)
    row = conn.execute("SELECT COUNT(*) AS cnt FROM accounts").fetchone()
    return row["cnt"] if row else 0


def get_data_date_range(user_slug: str = "default") -> Tuple[Optional[str], Optional[str]]:
    """Return (earliest_date, latest_date) in daily_metrics."""
    conn = get_connection(user_slug)
    row = conn.execute(
        "SELECT MIN(date) AS min_d, MAX(date) AS max_d FROM daily_metrics"
    ).fetchone()
    return (row["min_d"] if row else None, row["max_d"] if row else None)
