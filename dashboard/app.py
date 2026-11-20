"""
Instantly Domain Burn Detector — Web Dashboard

A simple Flask app that reads from the SQLite database populated by the
daily ETL and surfaces domain health metrics for cold email campaigns.

Views:
  /                          — Domain overview (health table)
  /domain/<domain>           — Per-mailbox breakdown for a domain
  /domain/<domain>/trend     — Daily reply rate trend (Chart.js)
  /domain/<domain>/trend/data — JSON data endpoint for trend chart
"""

import sys
from pathlib import Path

# Ensure project root is on sys.path so domain_tracker imports work
# when running from the dashboard/ directory
_project_root = str(Path(__file__).resolve().parent.parent)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

import hmac
import json
import os
import re
import sqlite3
from datetime import datetime, timedelta
import threading
import urllib.parse
from typing import Optional

from flask import Flask, g, jsonify, redirect, render_template, request, session, url_for

from domain_tracker.config import settings
from domain_tracker.users import User, UserRegistry

app = Flask(__name__)
app.secret_key = os.environ.get(
    "FLASK_SECRET_KEY",
    "instantly-domain-tracker-dev-secret-key-change-in-production",
)
if os.environ.get("DASHBOARD_PASSWORD"):
    # Only harden cookies once a password wall is actually in play — assumes
    # production is served over HTTPS (see deploy/ + README).
    app.config.update(
        SESSION_COOKIE_SECURE=True,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
    )

# ── Configuration ──────────────────────────────────────────────────────

USER_DATA_DIR = os.path.join(
    os.path.dirname(__file__), "..", "data", "users",
)

# Burn classification thresholds — loaded from settings.json with provider awareness
MIN_VOLUME = settings.MIN_VOLUME_THRESHOLD
DEFAULT_LOOKBACK_DAYS = settings.LOOKBACK_DAYS
TREND_DAYS = 30             # Default trend window

# Settings JSON path (persisted settings overrides)
SETTINGS_JSON_PATH = os.path.join(
    os.path.dirname(__file__), "..", "data", "settings.json",
)


# ── User-aware helpers ──────────────────────────────────────────────────

@app.after_request
def add_cache_headers(response):
    """Add no-cache headers to prevent browser caching of HTML and static files."""
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


@app.before_request
def require_login():
    """Gate the whole app behind a single shared password.

    Only active when DASHBOARD_PASSWORD is set (e.g. in production) — local
    dev with no password configured behaves exactly as before.
    """
    password = os.environ.get("DASHBOARD_PASSWORD")
    if not password:
        return None
    if request.path in ("/login", "/health") or request.path.startswith("/static/"):
        return None
    if session.get("authed"):
        return None
    return redirect(url_for("login", next=request.path))


@app.route("/login", methods=["GET", "POST"])
def login():
    password = os.environ.get("DASHBOARD_PASSWORD")
    if not password:
        return redirect(url_for("domain_overview"))
    if request.method == "POST":
        submitted = request.form.get("password", "")
        if hmac.compare_digest(submitted.encode("utf-8"), password.encode("utf-8")):
            session["authed"] = True
            dest = request.args.get("next") or url_for("domain_overview")
            return redirect(dest)
        return render_template("login.html", error="Incorrect password"), 401
    return render_template("login.html", error=None)


@app.route("/logout", methods=["POST"])
def logout():
    session.pop("authed", None)
    return redirect(url_for("login"))


@app.before_request
def load_user():
    """Load current user from session before each request."""
    slug = session.get("current_user_id", "default")
    registry = UserRegistry()
    g.current_user = registry.get_user(slug)
    g.is_admin = g.current_user is not None and g.current_user.is_admin
    g.all_users = [u for u in registry.list_users() if u.active]
    g.current_user_id = slug if g.current_user else "default"


def _get_user_db_path(slug: str = None) -> str:
    """Get the database path for a given user slug."""
    if slug is None:
        slug = g.get("current_user_id", "default")
    return os.path.join(USER_DATA_DIR, slug, "tracker.db")


# ── Database Helpers ───────────────────────────────────────────────────

def get_db():
    """Get a SQLite connection for this request (per current user)."""
    if "db" not in g:
        db_path = _get_user_db_path()
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        g.db = sqlite3.connect(db_path, timeout=10, check_same_thread=False)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA journal_mode=WAL")
        # Apply any missing schema migrations (e.g. new columns)
        _migrate_flask_db(g.db)
        g.db_path = db_path
    return g.db


def _migrate_flask_db(db):
    """Apply schema migrations for new columns (same as domain_tracker.db._migrate_schema)."""
    for col in ("hosted_ip", "hosted_country"):
        try:
            db.execute(f"SELECT {col} FROM accounts LIMIT 0")
        except sqlite3.OperationalError:
            try:
                db.execute(f"ALTER TABLE accounts ADD COLUMN {col} TEXT")
            except sqlite3.OperationalError:
                pass

    try:
        db.execute("SELECT COUNT(*) FROM domain_notes LIMIT 0")
    except sqlite3.OperationalError:
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS domain_notes (
                domain          TEXT PRIMARY KEY,
                note            TEXT,
                updated_at      TEXT
            )
            """
        )


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def db_exists() -> bool:
    """Check if the current user's database file has the expected tables."""
    db_path = _get_user_db_path()
    if not os.path.isfile(db_path):
        return False
    try:
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        cur = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            ("daily_metrics",),
        )
        exists = cur.fetchone() is not None
        conn.close()
        return exists
    except sqlite3.Error:
        return False


# ── Burn Classification ────────────────────────────────────────────────

PROVIDER_MAP = {
    1: "IMAP",
    2: "Google",
    3: "Microsoft",
    4: "AWS",
    8: "AirMail",
}


def get_provider_label(provider_code: Optional[int]) -> str:
    """Map provider code to display label: Gmail / Outlook / Other."""
    if provider_code == 2:
        return "Gmail"
    if provider_code == 3:
        return "Outlook"
    if provider_code and provider_code in PROVIDER_MAP:
        return PROVIDER_MAP[provider_code]
    return "Other"


def load_dashboard_settings() -> dict:
    """Load settings from the JSON file. Returns dict with defaults."""
    defaults = {
        "min_volume": 50,
        "google_burn_reply_rate": 1.0,
        "google_at_risk_reply_rate": 2.0,
        "google_burn_bounce_rate": 8.0,
        "google_at_risk_bounce_rate": 5.0,
        "outlook_burn_reply_rate": 0.5,
        "outlook_at_risk_reply_rate": 1.0,
        "outlook_burn_bounce_rate": 10.0,
        "outlook_at_risk_bounce_rate": 7.0,
        "other_burn_reply_rate": 1.0,
        "other_at_risk_reply_rate": 2.0,
        "other_burn_bounce_rate": 8.0,
        "other_at_risk_bounce_rate": 5.0,
    }
    try:
        with open(SETTINGS_JSON_PATH) as f:
            data = json.load(f)
            defaults.update(data)
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return defaults


def save_dashboard_settings(data: dict):
    """Save settings to the JSON file."""
    os.makedirs(os.path.dirname(SETTINGS_JSON_PATH), exist_ok=True)
    with open(SETTINGS_JSON_PATH, "w") as f:
        json.dump(data, f, indent=2)


def classify_domain(
    reply_rate: Optional[float],
    bounce_rate: Optional[float],
    provider_code: Optional[int] = None,
) -> str:
    """
    Classify a domain's health based on reply rate and bounce rate.

    Uses provider-specific thresholds from settings.json.
    Falls back to hardcoded defaults if settings.json is absent.

    Returns one of: 'BURNED', 'AT_RISK', 'HEALTHY'
    """
    if reply_rate is None:
        return "UNKNOWN"

    settings_data = load_dashboard_settings()

    # Map provider code to settings key prefix
    if provider_code == 2:
        prefix = "google"
    elif provider_code == 3:
        prefix = "outlook"
    else:
        prefix = "other"

    burn_reply = float(settings_data.get(f"{prefix}_burn_reply_rate", 1.0))
    at_risk_reply = float(settings_data.get(f"{prefix}_at_risk_reply_rate", 2.0))
    burn_bounce = float(settings_data.get(f"{prefix}_burn_bounce_rate", 8.0))
    at_risk_bounce = float(settings_data.get(f"{prefix}_at_risk_bounce_rate", 5.0))

    if reply_rate < burn_reply:
        return "BURNED"
    if bounce_rate is not None and bounce_rate > burn_bounce:
        return "BURNED"
    if reply_rate < at_risk_reply:
        return "AT_RISK"
    if bounce_rate is not None and bounce_rate > at_risk_bounce:
        return "AT_RISK"
    return "HEALTHY"


def classify_mailbox(
    reply_rate: Optional[float],
    bounce_rate: Optional[float],
    provider_code: Optional[int] = None,
) -> str:
    """Same logic for individual mailboxes, provider-aware."""
    return classify_domain(reply_rate, bounce_rate, provider_code=provider_code)


# ── Tag Filter Helper ────────────────────────────────────────────────

# Whitelist of allowed table aliases for _maybe_filter_tags
_ALLOWED_TAG_ALIASES = frozenset({"dm"})


def _maybe_filter_tags(request, query, params, alias="dm"):
    """Inject tag filter into query if ?tags= param is present.

    If "unlabeled" is among the tags, it matches accounts with no email_tags rows.
    Multiple regular tags use OR semantics (account needs any of them).
    "unlabeled" combined with regular tags means accounts matching EITHER.

    Args:
        alias: Table alias for the daily_metrics table (default "dm").
               Must be in _ALLOWED_TAG_ALIASES whitelist.

    Returns (modified_query, modified_params).
    """
    if alias not in _ALLOWED_TAG_ALIASES:
        raise ValueError(f"Invalid tag filter alias: {alias!r}. Allowed: {_ALLOWED_TAG_ALIASES}")

    tags_param = request.args.get("tags", "")
    if not tags_param:
        return query, params

    tags = [t.strip() for t in tags_param.split(",") if t.strip()]
    if not tags:
        return query, params

    has_unlabeled = "unlabeled" in [t.lower() for t in tags]
    regular_tags = [t for t in tags if t.lower() != "unlabeled"]

    conditions = []

    if regular_tags:
        placeholders = ", ".join(["?" for _ in regular_tags])
        conditions.append(
            f"EXISTS (SELECT 1 FROM email_tags et WHERE et.email_account = {alias}.email_account AND et.tag IN ({placeholders}))"
        )
        params = params + tuple(regular_tags)

    if has_unlabeled:
        conditions.append(
            f"NOT EXISTS (SELECT 1 FROM email_tags et WHERE et.email_account = {alias}.email_account)"
        )

    if conditions:
        query += " AND (" + " OR ".join(conditions) + ")"

    return query, params


# ── Burn Trajectory Helpers ──────────────────────────────────────────


def _compute_reply_rate(daily_slice: list[dict]) -> Optional[float]:
    """Compute reply rate percentage for a list of daily data dicts."""
    total_sent = sum(d.get("sent", 0) for d in daily_slice)
    total_replies = sum(d.get("replies", 0) for d in daily_slice)
    if total_sent >= MIN_VOLUME:
        return round((total_replies / total_sent) * 100, 2)
    return None


def _get_month_name(date_str: Optional[str]) -> Optional[str]:
    """Convert YYYY-MM-DD to month name."""
    if not date_str:
        return None
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        return dt.strftime("%B")
    except (ValueError, TypeError):
        return None


def _find_best_14d_period(daily_data: list[dict]) -> tuple:
    """
    Find the best 14-day rolling reply rate within the data.
    Returns (best_14d_rate, best_14d_month).
    Filters to days with >= 10 sent.
    Falls back to smaller windows if < 14 days available.
    """
    valid_days = [d for d in daily_data if d.get("sent", 0) >= 10]

    if len(valid_days) < 14:
        # Not enough data for a full 14d window — try smaller window
        if len(valid_days) < 2:
            return (None, None)
        window = max(2, len(valid_days) // 2)
        best = 0.0
        best_date = None
        for i in range(len(valid_days) - window + 1):
            chunk = valid_days[i:i + window]
            total_sent = sum(d["sent"] for d in chunk)
            total_replies = sum(d["replies"] for d in chunk)
            if total_sent >= 50:
                rr = (total_replies / total_sent) * 100
                if rr > best:
                    best = rr
                    best_date = chunk[window // 2]["date"]
        return (round(best, 2), _get_month_name(best_date) if best_date else None)

    best = 0.0
    best_date = None
    for i in range(len(valid_days) - 13):
        window = valid_days[i:i + 14]
        total_sent = sum(d["sent"] for d in window)
        total_replies = sum(d["replies"] for d in window)
        if total_sent >= 50:
            rr = (total_replies / total_sent) * 100
            if rr > best:
                best = rr
                best_date = window[7]["date"]  # middle of window for month

    return (round(best, 2), _get_month_name(best_date) if best_date else None)


def _classify_trajectory(rr_7d: Optional[float], best_14d: Optional[float]) -> str:
    """
    Classify domain trajectory into one of five labels.

    | Label                  | Condition                                       |
    |------------------------|-------------------------------------------------|
    | Consistently Healthy   | rr_7d >= 2.0% AND best_14d >= 2.0%              |
    | Recently Burned        | rr_7d < 1.0% AND best_14d >= 2.0%              |
    | Never Performed        | rr_7d < 1.0% AND best_14d < 2.0%              |
    | Recovering             | rr_7d >= 1.5% AND best_14d < 1.5%             |
    | At Risk                | rr_7d >= 1.0% AND rr_7d < 2.0% (catch-all)    |
    """
    if rr_7d is None or best_14d is None:
        return "Insufficient Data"

    if rr_7d >= 2.0 and best_14d >= 2.0:
        return "Consistently Healthy"
    if rr_7d < 1.0 and best_14d >= 2.0:
        return "Recently Burned"
    if rr_7d < 1.0 and best_14d < 2.0:
        return "Never Performed"
    if rr_7d >= 1.5 and best_14d < 1.5:
        return "Recovering"
    # Catch-all: rr_7d between 1.0% and 2.0%
    return "At Risk"


def _trajectory_description(
    trajectory: str,
    rr_7d: Optional[float],
    best_14d: Optional[float],
    best_month: Optional[str],
) -> str:
    """Generate human-readable description for the trajectory banner."""
    descriptions = {
        "Consistently Healthy": (
            "This domain has maintained healthy reply rates across all periods."
        ),
        "Recently Burned": (
            f"Was performing well at ~{best_14d}% "
            f"(best 14d period{' in ' + best_month if best_month else ''}). "
            f"Dropped to {rr_7d}% in the last 7 days."
        ),
        "Never Performed": (
            f"This domain has never had healthy reply rates. "
            f"Best 14d period was only {best_14d}%."
        ),
        "Recovering": (
            f"Was performing poorly but has improved to {rr_7d}% "
            f"in the last 7 days. Monitor to confirm."
        ),
        "At Risk": (
            "Reply rate is between 1-2%. At risk of burning — monitor closely."
        ),
    }
    return descriptions.get(
        trajectory,
        "Not enough data to determine trajectory."
    )


def _compute_trend_arrow(current_rr: Optional[float], prior_rr: Optional[float]) -> str:
    """
    Compare current vs prior reply rate and return trend direction.
    Returns 'improving', 'declining', or 'stable'.
    """
    if current_rr is None or prior_rr is None:
        return "stable"
    diff = current_rr - prior_rr
    if diff > 0.2:
        return "improving"
    if diff < -0.2:
        return "declining"
    return "stable"


def _compute_multi_timeframe_for_domain(
    db, domain: str,
    provider_code: Optional[int],
    tags: Optional[list] = None,
) -> Optional[dict]:
    """
    Compute aggregated multi-timeframe data for a domain.
    Used by both the JSON endpoint and domain_detail route for pre-rendering.

    Args:
        tags: Optional list of tag strings. If provided, only accounts
              matching these tags contribute to the aggregates.
              "unlabeled" matches accounts with no tags.
    """
    today = datetime.utcnow().date()
    cutoff_7d = today - timedelta(days=7)
    cutoff_14d = today - timedelta(days=14)
    cutoff_30d = today - timedelta(days=30)
    cutoff_90d = today - timedelta(days=90)
    cutoff_7d_prior = today - timedelta(days=14)
    cutoff_14d_prior = today - timedelta(days=28)
    cutoff_30d_prior = today - timedelta(days=60)
    cutoff_90d_prior = today - timedelta(days=180)
    cutoff_earliest = cutoff_90d_prior

    # Build tag filter conditions
    tag_where = ""
    tag_params = {}
    if tags:
        has_unlabeled = "unlabeled" in [t.lower() for t in tags]
        regular_tags = [t for t in tags if t.lower() != "unlabeled"]
        tag_conditions = []
        if regular_tags:
            named_phs = ", ".join([f":tag_{i}" for i in range(len(regular_tags))])
            for i, t in enumerate(regular_tags):
                tag_params[f"tag_{i}"] = t
            tag_conditions.append(
                f"EXISTS (SELECT 1 FROM email_tags et WHERE et.email_account = dm.email_account AND et.tag IN ({named_phs}))"
            )
        if has_unlabeled:
            tag_conditions.append(
                "NOT EXISTS (SELECT 1 FROM email_tags et WHERE et.email_account = dm.email_account)"
            )
        if tag_conditions:
            tag_where = " AND (" + " OR ".join(tag_conditions) + ")"

    query = f"""
        SELECT
            COALESCE(SUM(CASE WHEN dm.date >= :c7  THEN dm.sent ELSE 0 END), 0) AS sent_7d,
            COALESCE(SUM(CASE WHEN dm.date >= :c7  THEN dm.unique_replies ELSE 0 END), 0) AS human_7d,
            COALESCE(SUM(CASE WHEN dm.date >= :c7  THEN dm.unique_replies_automatic ELSE 0 END), 0) AS auto_7d,
            COALESCE(SUM(CASE WHEN dm.date >= :c7  THEN dm.bounced ELSE 0 END), 0) AS bounced_7d,
            COALESCE(SUM(CASE WHEN dm.date >= :c7p AND dm.date < :c7  THEN dm.sent ELSE 0 END), 0) AS sent_7d_p,
            COALESCE(SUM(CASE WHEN dm.date >= :c7p AND dm.date < :c7  THEN dm.unique_replies ELSE 0 END), 0) AS human_7d_p,
            COALESCE(SUM(CASE WHEN dm.date >= :c7p AND dm.date < :c7  THEN dm.unique_replies_automatic ELSE 0 END), 0) AS auto_7d_p,
            COALESCE(SUM(CASE WHEN dm.date >= :c7p AND dm.date < :c7  THEN dm.bounced ELSE 0 END), 0) AS bounced_7d_p,
            COALESCE(SUM(CASE WHEN dm.date >= :c14 THEN dm.sent ELSE 0 END), 0) AS sent_14d,
            COALESCE(SUM(CASE WHEN dm.date >= :c14 THEN dm.unique_replies ELSE 0 END), 0) AS human_14d,
            COALESCE(SUM(CASE WHEN dm.date >= :c14 THEN dm.unique_replies_automatic ELSE 0 END), 0) AS auto_14d,
            COALESCE(SUM(CASE WHEN dm.date >= :c14 THEN dm.bounced ELSE 0 END), 0) AS bounced_14d,
            COALESCE(SUM(CASE WHEN dm.date >= :c14p AND dm.date < :c14 THEN dm.sent ELSE 0 END), 0) AS sent_14d_p,
            COALESCE(SUM(CASE WHEN dm.date >= :c14p AND dm.date < :c14 THEN dm.unique_replies ELSE 0 END), 0) AS human_14d_p,
            COALESCE(SUM(CASE WHEN dm.date >= :c14p AND dm.date < :c14 THEN dm.unique_replies_automatic ELSE 0 END), 0) AS auto_14d_p,
            COALESCE(SUM(CASE WHEN dm.date >= :c14p AND dm.date < :c14 THEN dm.bounced ELSE 0 END), 0) AS bounced_14d_p,
            COALESCE(SUM(CASE WHEN dm.date >= :c30 THEN dm.sent ELSE 0 END), 0) AS sent_30d,
            COALESCE(SUM(CASE WHEN dm.date >= :c30 THEN dm.unique_replies ELSE 0 END), 0) AS human_30d,
            COALESCE(SUM(CASE WHEN dm.date >= :c30 THEN dm.unique_replies_automatic ELSE 0 END), 0) AS auto_30d,
            COALESCE(SUM(CASE WHEN dm.date >= :c30 THEN dm.bounced ELSE 0 END), 0) AS bounced_30d,
            COALESCE(SUM(CASE WHEN dm.date >= :c30p AND dm.date < :c30 THEN dm.sent ELSE 0 END), 0) AS sent_30d_p,
            COALESCE(SUM(CASE WHEN dm.date >= :c30p AND dm.date < :c30 THEN dm.unique_replies ELSE 0 END), 0) AS human_30d_p,
            COALESCE(SUM(CASE WHEN dm.date >= :c30p AND dm.date < :c30 THEN dm.unique_replies_automatic ELSE 0 END), 0) AS auto_30d_p,
            COALESCE(SUM(CASE WHEN dm.date >= :c30p AND dm.date < :c30 THEN dm.bounced ELSE 0 END), 0) AS bounced_30d_p,
            COALESCE(SUM(CASE WHEN dm.date >= :c90 THEN dm.sent ELSE 0 END), 0) AS sent_90d,
            COALESCE(SUM(CASE WHEN dm.date >= :c90 THEN dm.unique_replies ELSE 0 END), 0) AS human_90d,
            COALESCE(SUM(CASE WHEN dm.date >= :c90 THEN dm.unique_replies_automatic ELSE 0 END), 0) AS auto_90d,
            COALESCE(SUM(CASE WHEN dm.date >= :c90 THEN dm.bounced ELSE 0 END), 0) AS bounced_90d,
            COALESCE(SUM(CASE WHEN dm.date >= :c90p AND dm.date < :c90 THEN dm.sent ELSE 0 END), 0) AS sent_90d_p,
            COALESCE(SUM(CASE WHEN dm.date >= :c90p AND dm.date < :c90 THEN dm.unique_replies ELSE 0 END), 0) AS human_90d_p,
            COALESCE(SUM(CASE WHEN dm.date >= :c90p AND dm.date < :c90 THEN dm.unique_replies_automatic ELSE 0 END), 0) AS auto_90d_p,
            COALESCE(SUM(CASE WHEN dm.date >= :c90p AND dm.date < :c90 THEN dm.bounced ELSE 0 END), 0) AS bounced_90d_p
        FROM daily_metrics dm
        JOIN accounts a ON a.email = dm.email_account
        WHERE a.domain = :domain
          AND a.status = 1
          AND dm.date >= :earliest
          {tag_where}
    """

    params = {
        "c7": str(cutoff_7d),
        "c7p": str(cutoff_7d_prior),
        "c14": str(cutoff_14d),
        "c14p": str(cutoff_14d_prior),
        "c30": str(cutoff_30d),
        "c30p": str(cutoff_30d_prior),
        "c90": str(cutoff_90d),
        "c90p": str(cutoff_90d_prior),
        "earliest": str(cutoff_earliest),
        "domain": domain,
        **tag_params,
    }

    row = db.execute(query, params).fetchone()

    if not row:
        return None

    def _tf(sent, human, auto, bounced, sent_p, human_p, auto_p, bounced_p):
        total_replies = human + auto
        prior_total = human_p + auto_p
        reply_rate = round((total_replies / sent * 100), 2) if sent >= MIN_VOLUME else None
        bounce_rate = round((bounced / sent * 100), 2) if sent >= MIN_VOLUME else None
        prior_rr = round((prior_total / sent_p * 100), 2) if sent_p >= MIN_VOLUME else None
        status = classify_domain(reply_rate, bounce_rate, provider_code=provider_code)
        trend = _compute_trend_arrow(reply_rate, prior_rr)
        return {
            "sent": sent,
            "replies": total_replies,
            "human_replies": human,
            "bounced": bounced,
            "reply_rate": reply_rate,
            "bounce_rate": bounce_rate,
            "status": status,
            "trend": trend,
        }

    return {
        "domain": domain,
        "timeframes": {
            "7d": _tf(
                row["sent_7d"], row["human_7d"], row["auto_7d"], row["bounced_7d"],
                row["sent_7d_p"], row["human_7d_p"], row["auto_7d_p"], row["bounced_7d_p"],
            ),
            "14d": _tf(
                row["sent_14d"], row["human_14d"], row["auto_14d"], row["bounced_14d"],
                row["sent_14d_p"], row["human_14d_p"], row["auto_14d_p"], row["bounced_14d_p"],
            ),
            "30d": _tf(
                row["sent_30d"], row["human_30d"], row["auto_30d"], row["bounced_30d"],
                row["sent_30d_p"], row["human_30d_p"], row["auto_30d_p"], row["bounced_30d_p"],
            ),
            "90d": _tf(
                row["sent_90d"], row["human_90d"], row["auto_90d"], row["bounced_90d"],
                row["sent_90d_p"], row["human_90d_p"], row["auto_90d_p"], row["bounced_90d_p"],
            ),
        },
    }


def _compute_trajectory_for_domain(
    db, domain: str,
    tags: Optional[list] = None,
) -> Optional[dict]:
    """
    Compute burn trajectory data for a domain.
    Used by both the JSON endpoint and domain_detail route for pre-rendering.

    Args:
        tags: Optional list of tag strings. If provided, only accounts
              matching these tags contribute to the aggregates.
    """
    today = datetime.utcnow().date()
    cutoff_90d = today - timedelta(days=90)

    query = """SELECT dm.date,
                  SUM(dm.sent) AS total_sent,
                  SUM(dm.unique_replies) + SUM(dm.unique_replies_automatic) AS total_replies
           FROM daily_metrics dm
           JOIN accounts a ON a.email = dm.email_account
           WHERE a.domain = ? AND dm.date >= ? AND a.status = 1"""

    params_list = [domain, str(cutoff_90d)]

    if tags:
        has_unlabeled = "unlabeled" in [t.lower() for t in tags]
        regular_tags = [t for t in tags if t.lower() != "unlabeled"]
        conds = []
        if regular_tags:
            placeholders = ", ".join(["?" for _ in regular_tags])
            conds.append(
                f"EXISTS (SELECT 1 FROM email_tags et WHERE et.email_account = dm.email_account AND et.tag IN ({placeholders}))"
            )
            params_list.extend(regular_tags)
        if has_unlabeled:
            conds.append(
                "NOT EXISTS (SELECT 1 FROM email_tags et WHERE et.email_account = dm.email_account)"
            )
        if conds:
            query += " AND (" + " OR ".join(conds) + ")"

    query += " GROUP BY dm.date ORDER BY dm.date"

    rows = db.execute(query, params_list).fetchall()

    if not rows:
        return None

    daily = [
        {"date": r["date"], "sent": r["total_sent"], "replies": r["total_replies"]}
        for r in rows
    ]

    # Compute rr_7d from last 7 days
    if len(daily) >= 7:
        rr_7d = _compute_reply_rate(daily[-7:])
    elif daily:
        rr_7d = _compute_reply_rate(daily[-min(len(daily), 7):])
    else:
        rr_7d = None

    best_14d, best_month = _find_best_14d_period(daily)
    trajectory = _classify_trajectory(rr_7d, best_14d)
    description = _trajectory_description(trajectory, rr_7d, best_14d, best_month)

    data_points = len(daily)
    sent_7d = sum(d["sent"] for d in daily[-7:]) if len(daily) >= 7 else sum(d["sent"] for d in daily)
    is_new = data_points < 14
    is_low_volume = sent_7d < MIN_VOLUME if sent_7d else True

    return {
        "domain": domain,
        "rr_7d": rr_7d,
        "best_14d": best_14d,
        "best_14d_month": best_month,
        "trajectory": trajectory,
        "description": description,
        "data_points": data_points,
        "is_new": is_new,
        "is_low_volume": is_low_volume,
        "limited_history": data_points < 90,
    }


# ── Routes ─────────────────────────────────────────────────────────────

@app.route("/settings", methods=["GET", "POST"])
def settings_page():
    """Settings page — configure burn detection thresholds."""
    if request.method == "POST":
        # Merge with existing to preserve any non-posted keys
        data = load_dashboard_settings()
        for key in [
            "min_volume",
            "google_burn_reply_rate", "google_at_risk_reply_rate",
            "google_burn_bounce_rate", "google_at_risk_bounce_rate",
            "outlook_burn_reply_rate", "outlook_at_risk_reply_rate",
            "outlook_burn_bounce_rate", "outlook_at_risk_bounce_rate",
            "other_burn_reply_rate", "other_at_risk_reply_rate",
            "other_burn_bounce_rate", "other_at_risk_bounce_rate",
        ]:
            val = request.form.get(key)
            if val is not None:
                try:
                    data[key] = float(val)
                except ValueError:
                    pass
        if data:
            save_dashboard_settings(data)
        return render_template("settings.html", settings=load_dashboard_settings(), saved=True)

    return render_template("settings.html", settings=load_dashboard_settings(), saved=False)


@app.route("/")
def domain_overview():
    """Main page: table of all domains with aggregated health metrics."""
    if not db_exists():
        return render_template("no_data.html")

    # Overview page defaults to a 7-day window (other pages keep their default).
    lookback = request.args.get("window", 7, type=int)
    start_param = request.args.get("start")
    end_param = request.args.get("end")
    show_all = request.args.get('show_all', 'false') == 'true'

    if start_param and end_param:
        cutoff = start_param
        end_cutoff = end_param
    else:
        cutoff = (datetime.utcnow() - timedelta(days=lookback)).strftime("%Y-%m-%d")
        end_cutoff = None

    db = get_db()

    extra_date_clause = "AND dm.date <= ? " if end_cutoff else ""
    params = (cutoff, end_cutoff) if end_cutoff else (cutoff,)
    # Provider filter
    provider_filter = request.args.get("provider", "")

    # Build provider mapping (domain -> most common provider_code)
    provider_rows = db.execute(
        """
        SELECT domain, provider_code, COUNT(*) AS cnt
        FROM accounts
        WHERE provider_code IS NOT NULL
          AND status = 1
        GROUP BY domain, provider_code
        ORDER BY domain, cnt DESC
        """
    ).fetchall()
    domain_providers = {}
    for pr in provider_rows:
        if pr["domain"] not in domain_providers:
            domain_providers[pr["domain"]] = pr["provider_code"]

    # Per-domain mailbox roster count (all accounts, not just in-window senders)
    domain_roster = {
        r["domain"]: r["c"]
        for r in db.execute(
            "SELECT domain, COUNT(*) AS c FROM accounts WHERE status = 1 GROUP BY domain"
        ).fetchall()
    }

    # Per-domain notes (free-text, manually entered)
    domain_notes = {
        r["domain"]: r["note"]
        for r in db.execute("SELECT domain, note FROM domain_notes").fetchall()
    }

    provider_join = ""
    provider_having = ""
    if provider_filter:
        if provider_filter == "gmail":
            provider_join = (
                "JOIN (SELECT domain FROM accounts WHERE provider_code = 2 GROUP BY domain) apf "
                "ON apf.domain = a.domain "
            )
        elif provider_filter == "outlook":
            provider_join = (
                "JOIN (SELECT domain FROM accounts WHERE provider_code = 3 GROUP BY domain) apf "
                "ON apf.domain = a.domain "
            )
        elif provider_filter == "other":
            # Domains where the most common provider_code is NOT 2 or 3
            provider_join = (
                "JOIN (SELECT domain FROM accounts WHERE provider_code NOT IN (2, 3) OR provider_code IS NULL GROUP BY domain) apf "
                "ON apf.domain = a.domain "
            )

    query_str = f"""
        SELECT
            a.domain,
            COUNT(DISTINCT dm.email_account) AS account_count,
            SUM(dm.sent) AS total_sent,
            SUM(dm.unique_replies) + SUM(dm.unique_replies_automatic) AS total_replies,
            SUM(dm.unique_replies) AS human_replies,
            SUM(dm.bounced) AS total_bounced,
            SUM(dm.opened) AS total_opened
        FROM daily_metrics dm
        JOIN accounts a ON a.email = dm.email_account
        {provider_join}
        WHERE dm.date >= ?
          {extra_date_clause}
          AND dm.sent > 0
          AND a.status = 1
    """

    query_str, params = _maybe_filter_tags(request, query_str, params, alias="dm")
    query_str += "\n        GROUP BY a.domain\n        ORDER BY a.domain"

    rows = db.execute(query_str, params).fetchall()

    domains = []
    for row in rows:
        sent = row["total_sent"] or 0
        replies = row["total_replies"] or 0
        human_replies = row["human_replies"] or 0
        bounced = row["total_bounced"] or 0
        opened = row["total_opened"] or 0

        reply_rate = (replies / sent * 100) if sent >= MIN_VOLUME else None
        bounce_rate = (bounced / sent * 100) if sent >= MIN_VOLUME else None
        open_rate = (opened / sent * 100) if sent > 0 else None

        provider_code = domain_providers.get(row["domain"])
        status = classify_domain(reply_rate, bounce_rate, provider_code=provider_code)
        provider_label = get_provider_label(provider_code)

        # Roster vs in-window senders → idle count for this domain/window
        roster = domain_roster.get(row["domain"], row["account_count"] or 0)
        window_senders = row["account_count"] or 0
        idle_count = max(roster - window_senders, 0)

        domains.append({
            "domain": row["domain"],
            "provider": provider_label,
            "provider_code": provider_code,
            # Total mailboxes on the domain (full roster), not just in-window senders
            "account_count": roster,
            "idle_count": idle_count,
            "total_sent": sent,
            "total_replies": replies,
            "human_replies": human_replies,
            "reply_rate": round(reply_rate, 2) if reply_rate is not None else None,
            "total_bounced": bounced,
            "bounce_rate": round(bounce_rate, 2) if bounce_rate is not None else None,
            "bounce_display": f"{round(bounce_rate, 2)}%({bounced})" if bounce_rate is not None else "—",
            "open_rate": round(open_rate, 2) if open_rate is not None else None,
            "status": status,
            "low_volume": sent < MIN_VOLUME,
            "qualifies": sent >= MIN_VOLUME,
            "note": domain_notes.get(row["domain"]) or "",
        })

    # Calculate reply rate scale max for progress bars
    valid_rates = [d["reply_rate"] for d in domains if d["reply_rate"] is not None]
    max_rate = max(valid_rates) if valid_rates else 0
    if max_rate <= 10:
        scale_max = 10
    elif max_rate <= 20:
        scale_max = 20
    else:
        scale_max = 30

    # Sort by URL parameter (survives page refresh)
    sort_by = request.args.get('sort', 'reply_rate')
    sort_order = request.args.get('order', 'desc')
    reverse = sort_order == 'desc'

    if sort_by == 'domain':
        domains.sort(key=lambda d: d.get('domain') or '', reverse=reverse)
    elif sort_by == 'provider':
        domains.sort(key=lambda d: d.get('provider') or '', reverse=reverse)
    elif sort_by == 'account_count':
        domains.sort(key=lambda d: d.get('account_count') or 0, reverse=reverse)
    elif sort_by == 'idle_count':
        domains.sort(key=lambda d: d.get('idle_count') or 0, reverse=reverse)
    elif sort_by == 'sent':
        domains.sort(key=lambda d: d.get('total_sent') or 0, reverse=reverse)
    elif sort_by == 'replies':
        domains.sort(key=lambda d: d.get('total_replies') or 0, reverse=reverse)
    elif sort_by == 'reply_rate':
        # Treat None (low-volume / idle, shown as "—") as 0% so they sink
        # to the bottom on a descending sort. Tie-break by total_sent.
        domains.sort(key=lambda d: (d.get('reply_rate') if d.get('reply_rate') is not None else 0.0, d.get('total_sent') or 0), reverse=reverse)
    elif sort_by == 'bounce_rate':
        domains.sort(key=lambda d: d.get('bounce_rate') or 0, reverse=reverse)
    elif sort_by == 'bounced':
        domains.sort(key=lambda d: d.get('total_bounced') or 0, reverse=reverse)
    elif sort_by == 'status':
        status_order = {"BURNED": 0, "AT_RISK": 1, "HEALTHY": 2, "UNKNOWN": 3}
        domains.sort(key=lambda d: (status_order.get(d.get('status'), 99), d.get('domain') or ''), reverse=reverse)
    else:
        # Default: sort by reply_rate descending; "—" treated as 0%
        domains.sort(key=lambda d: (d.get('reply_rate') if d.get('reply_rate') is not None else 0.0, d.get('total_sent') or 0), reverse=True)

    # Show all domains (the "min 50 sent to qualify" gate was removed).
    burned = sum(1 for d in domains if d["status"] == "BURNED")
    at_risk = sum(1 for d in domains if d["status"] == "AT_RISK")
    healthy = sum(1 for d in domains if d["status"] == "HEALTHY")
    # Total mailbox roster (all accounts), independent of whether they sent in
    # the window — counts from the accounts table so mailboxes with no daily
    # metrics are still included.
    total_emails = db.execute("SELECT COUNT(*) AS c FROM accounts WHERE status = 1").fetchone()["c"]

    return render_template(
        "overview.html",
        domains=domains,
        lookback=lookback,
        start=start_param,
        end=end_param,
        burned=burned,
        at_risk=at_risk,
        healthy=healthy,
        total_emails=total_emails,
        provider_filter=provider_filter,
        scale_max=scale_max,
        sort_by=sort_by,
        sort_order=sort_order,
        show_all=show_all,
        now=datetime.utcnow(),
        tags_filter=request.args.get("tags", ""),
    )


@app.route("/domain/<domain>")
def domain_detail(domain: str):
    """Per-mailbox breakdown for a specific domain."""
    if not db_exists():
        return render_template("no_data.html")

    lookback = request.args.get("window", DEFAULT_LOOKBACK_DAYS, type=int)
    start_param = request.args.get("start")
    end_param = request.args.get("end")

    if start_param and end_param:
        cutoff = start_param
        end_cutoff = end_param
    else:
        cutoff = (datetime.utcnow() - timedelta(days=lookback)).strftime("%Y-%m-%d")
        end_cutoff = None

    db = get_db()

    # Get domain info
    accounts = db.execute(
        "SELECT COUNT(*) AS cnt FROM accounts WHERE domain = ?",
        (domain,),
    ).fetchone()

    if not accounts or accounts["cnt"] == 0:
        return render_template("domain_not_found.html", domain=domain)

    # Get per-mailbox metrics — LEFT JOIN from accounts so EVERY mailbox on
    # the domain shows, including idle ones that didn't send in the window
    # (they come through with NULL/zero metrics). Date + sent filters live in
    # the ON clause so they don't turn the LEFT JOIN into an inner join.
    extra_date_clause = "AND dm.date <= ? " if end_cutoff else ""
    params_list = [cutoff]
    if end_cutoff:
        params_list.append(end_cutoff)
    params_list.append(domain)
    params = tuple(params_list)

    mailbox_query = f"""
        SELECT
            a.email AS email_account,
            a.status,
            a.warmup_status,
            SUM(dm.sent) AS total_sent,
            SUM(dm.unique_replies) + SUM(dm.unique_replies_automatic) AS total_replies,
            SUM(dm.unique_replies) AS human_replies,
            SUM(dm.bounced) AS total_bounced,
            SUM(dm.opened) AS total_opened,
            SUM(dm.unique_opened) AS total_unique_opened,
            SUM(dm.clicks) AS total_clicks,
            SUM(dm.unique_clicks) AS total_unique_clicks,
            SUM(dm.contacted) AS total_contacted
        FROM accounts a
        LEFT JOIN daily_metrics dm
            ON dm.email_account = a.email
           AND dm.date >= ?
           {extra_date_clause}
           AND dm.sent > 0
        WHERE a.domain = ?
    """

    mailbox_query, mb_params = _maybe_filter_tags(request, mailbox_query, params, alias="dm")
    mailbox_query += "\n        GROUP BY a.email\n        ORDER BY total_sent DESC, a.email"

    rows = db.execute(mailbox_query, mb_params).fetchall()

    # Get provider code for domain-level thresholds
    provider_row = db.execute(
        "SELECT provider_code FROM accounts WHERE domain = ? AND status = 1 AND provider_code IS NOT NULL LIMIT 1",
        (domain,),
    ).fetchone()
    domain_provider_code = provider_row["provider_code"] if provider_row else None

    # Get hosted country
    country_row = db.execute(
        "SELECT hosted_country FROM accounts WHERE domain = ? AND hosted_country IS NOT NULL LIMIT 1",
        (domain,),
    ).fetchone()
    domain_hosted_country = country_row["hosted_country"] if country_row else None

    mailboxes = []
    for row in rows:
        sent = row["total_sent"] or 0
        replies = row["total_replies"] or 0
        human_replies = row["human_replies"] or 0
        bounced = row["total_bounced"] or 0

        reply_rate = (replies / sent * 100) if sent >= MIN_VOLUME else None
        bounce_rate = (bounced / sent * 100) if sent >= MIN_VOLUME else None

        status = classify_mailbox(reply_rate, bounce_rate, provider_code=domain_provider_code)

        mailboxes.append({
            "email": row["email_account"],
            "status_code": row["status"],
            "warmup_status": row["warmup_status"],
            "total_sent": sent,
            "total_replies": replies,
            "human_replies": human_replies,
            "reply_rate": round(reply_rate, 2) if reply_rate is not None else None,
            "total_bounced": bounced,
            "bounce_rate": round(bounce_rate, 2) if bounce_rate is not None else None,
            "bounce_display": f"{round(bounce_rate, 2)}%({bounced})" if bounce_rate is not None else "—",
            "total_opened": row["total_opened"] or 0,
            "total_unique_opened": row["total_unique_opened"] or 0,
            "total_clicks": row["total_clicks"] or 0,
            "total_contacted": row["total_contacted"] or 0,
            "status": status,
            "low_volume": sent < MIN_VOLUME,
        })

    # Domain-level aggregate
    total_sent = sum(m["total_sent"] for m in mailboxes)
    total_replies = sum(m["total_replies"] for m in mailboxes)
    total_bounced = sum(m["total_bounced"] for m in mailboxes)
    domain_reply_rate = round((total_replies / total_sent * 100), 2) if total_sent >= MIN_VOLUME else None
    domain_bounce_rate = round((total_bounced / total_sent * 100), 2) if total_sent >= MIN_VOLUME else None
    domain_status = classify_domain(domain_reply_rate, domain_bounce_rate, provider_code=domain_provider_code)

    # Calculate reply rate scale max for mailbox progress bars
    valid_rates = [m["reply_rate"] for m in mailboxes if m["reply_rate"] is not None]
    max_rate = max(valid_rates) if valid_rates else 0
    if max_rate <= 10:
        scale_max = 10
    elif max_rate <= 20:
        scale_max = 20
    else:
        scale_max = 30

    # Sort mailboxes by URL parameter (survives page refresh)
    sort_by = request.args.get('sort', 'reply_rate')
    sort_order = request.args.get('order', 'desc')
    reverse = sort_order == 'desc'

    if sort_by == 'email':
        mailboxes.sort(key=lambda m: m.get('email') or '', reverse=reverse)
    elif sort_by == 'sent':
        mailboxes.sort(key=lambda m: m.get('total_sent') or 0, reverse=reverse)
    elif sort_by == 'replies':
        mailboxes.sort(key=lambda m: m.get('total_replies') or 0, reverse=reverse)
    elif sort_by == 'reply_rate':
        # Treat low-volume / idle (no reply rate, shown as "—") as 0% so they
        # sink to the bottom on a descending sort. Tie-break by volume (more
        # sent first) so real 0%-senders rank above never-sent mailboxes.
        mailboxes.sort(key=lambda m: (m.get('reply_rate') if m.get('reply_rate') is not None else 0.0, m.get('total_sent') or 0), reverse=reverse)
    elif sort_by == 'bounce_rate':
        mailboxes.sort(key=lambda m: m.get('bounce_rate') or 0, reverse=reverse)
    elif sort_by == 'bounced':
        mailboxes.sort(key=lambda m: m.get('total_bounced') or 0, reverse=reverse)
    elif sort_by == 'opens':
        mailboxes.sort(key=lambda m: m.get('total_opened') or 0, reverse=reverse)
    elif sort_by == 'clicks':
        mailboxes.sort(key=lambda m: m.get('total_clicks') or 0, reverse=reverse)
    elif sort_by == 'status':
        status_order = {"BURNED": 0, "AT_RISK": 1, "HEALTHY": 2, "UNKNOWN": 3}
        mailboxes.sort(key=lambda m: (status_order.get(m.get('status'), 99), m.get('email') or ''), reverse=reverse)
    else:
        # Default: reply rate descending; "—" (low-vol/idle) treated as 0% so
        # they sink, tie-broken by volume (more sent first).
        mailboxes.sort(key=lambda m: (m.get('reply_rate') if m.get('reply_rate') is not None else 0.0, m.get('total_sent') or 0), reverse=True)

    # Get campaigns for this domain
    campaigns = db.execute(
        """
        SELECT DISTINCT ac.campaign_name, ac.campaign_id
        FROM account_campaigns ac
        JOIN accounts a ON a.email = ac.email_account
        WHERE a.domain = ?
          AND a.status = 1
        ORDER BY ac.campaign_name
        """,
        (domain,),
    ).fetchall()

    # ── Pre-compute trajectory + timeframe data for the template ──

    # Parse tags for compute functions
    tags_param = request.args.get("tags", "")
    tags_list = [t.strip() for t in tags_param.split(",") if t.strip()] if tags_param else None

    # Build multi-timeframe data (single CASE query)
    try:
        timeframe_data = _compute_multi_timeframe_for_domain(db, domain, domain_provider_code, tags=tags_list)
    except Exception:
        timeframe_data = None

    # Build trajectory data (90d daily aggregation)
    try:
        trajectory_data = _compute_trajectory_for_domain(db, domain, tags=tags_list)
    except Exception:
        trajectory_data = None

    return render_template(
        "domain_detail.html",
        domain=domain,
        domain_status=domain_status,
        domain_reply_rate=domain_reply_rate,
        domain_bounce_rate=domain_bounce_rate,
        total_sent=total_sent,
        total_replies=total_replies,
        total_bounced=total_bounced,
        mailboxes=mailboxes,
        campaigns=campaigns,
        lookback=lookback,
        start=start_param,
        end=end_param,
        count=len(mailboxes),
        scale_max=scale_max,
        sort_by=sort_by,
        sort_order=sort_order,
        now=datetime.utcnow(),
        # New context for domain detail redesign
        trajectory_data=json.dumps(trajectory_data) if trajectory_data else None,
        timeframe_data=json.dumps(timeframe_data) if timeframe_data else None,
        chart_range_default="1m",
        tags_filter=tags_param,
        hosted_country=domain_hosted_country,
    )


@app.route("/domain/<domain>/trend")
def domain_trend(domain: str):
    """Daily reply rate trend for a specific domain (server-rendered Chart.js)."""
    if not db_exists():
        return render_template("no_data.html")

    days = request.args.get("days", TREND_DAYS, type=int)
    start_param = request.args.get("start")
    end_param = request.args.get("end")

    db = get_db()

    # Check domain exists
    acct = db.execute(
        "SELECT COUNT(*) AS cnt FROM accounts WHERE domain = ?",
        (domain,),
    ).fetchone()
    if not acct or acct["cnt"] == 0:
        return render_template("domain_not_found.html", domain=domain)

    if start_param and end_param:
        cutoff = start_param
        end_cutoff = end_param
    else:
        cutoff = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d")
        end_cutoff = None

    extra_date_clause = "AND dm.date <= ? " if end_cutoff else ""
    params = (domain, cutoff, end_cutoff) if end_cutoff else (domain, cutoff)

    query = """
        SELECT
            dm.date,
            SUM(dm.sent) AS total_sent,
            SUM(dm.unique_replies) + SUM(dm.unique_replies_automatic) AS total_replies,
            SUM(dm.bounced) AS total_bounced
        FROM daily_metrics dm
        JOIN accounts a ON a.email = dm.email_account
        WHERE a.domain = ?
          AND dm.date >= ?
          {extra_date_clause}
          AND dm.sent > 0
          AND a.status = 1
        GROUP BY dm.date
        ORDER BY dm.date ASC
    """.format(extra_date_clause=extra_date_clause)

    query, td_params = _maybe_filter_tags(request, query, params, alias="dm")

    rows = db.execute(query, td_params).fetchall()

    # Build lookup by date
    data_by_date = {}
    for row in rows:
        sent = row["total_sent"] or 0
        total_replies = row["total_replies"] or 0
        bounced = row["total_bounced"] or 0
        reply_rate = round(total_replies / sent * 100, 2) if sent >= 10 else None
        bounce_rate = round(bounced / sent * 100, 2) if sent >= 10 else None
        data_by_date[row["date"]] = (reply_rate, bounce_rate, sent)

    # Fill in missing dates with null/zero placeholders
    if start_param and end_param:
        end_date = datetime.strptime(end_cutoff, "%Y-%m-%d").date()
    else:
        end_date = datetime.utcnow().date()
    start_date = datetime.strptime(cutoff, "%Y-%m-%d").date()

    dates = []
    reply_rates = []
    bounce_rates = []
    sent_counts = []

    current = start_date
    while current <= end_date:
        date_str = current.strftime("%Y-%m-%d")
        dates.append(date_str)
        if date_str in data_by_date:
            rr, br, sc = data_by_date[date_str]
            reply_rates.append(rr)
            bounce_rates.append(br)
            sent_counts.append(sc)
        else:
            reply_rates.append(None)
            bounce_rates.append(None)
            sent_counts.append(0)
        current += timedelta(days=1)

    return render_template(
        "trend.html",
        domain=domain,
        days=days,
        start=start_param,
        end=end_param,
        dates=dates,
        reply_rates=reply_rates,
        bounce_rates=bounce_rates,
        sent_counts=sent_counts,
    )


@app.route("/trend/data")
def all_domains_trend_data():
    """JSON endpoint for the COMBINED (all-domains) trend chart.

    Same shape/logic as domain_trend_data() but aggregates across every
    domain instead of filtering to one. Backs the overview page's combined
    chart via the shared trend-chart.js component.

    Supports the same params: ?days=N, ?range=, ?start=&end=, ?tags=
    """
    if not db_exists():
        return jsonify({"error": "No data"}), 404

    range_param = request.args.get("range", "")
    range_map = {
        "30d": 30, "90d": 90, "180d": 180,
        "1m": 30, "3m": 90, "6m": 180, "all": 365,
    }
    if range_param and range_param in range_map:
        days = range_map[range_param]
    else:
        days = request.args.get("days", TREND_DAYS, type=int)

    start_param = request.args.get("start")
    end_param = request.args.get("end")

    db = get_db()

    if start_param and end_param:
        cutoff = start_param
        end_cutoff = end_param
    else:
        cutoff = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d")
        end_cutoff = None

    extra_date_clause = "AND dm.date <= ? " if end_cutoff else ""
    params = (cutoff, end_cutoff) if end_cutoff else (cutoff,)
    trend_query = f"""
        SELECT
            dm.date,
            SUM(dm.sent) AS total_sent,
            SUM(dm.unique_replies) AS total_human_replies,
            SUM(dm.unique_replies_automatic) AS total_auto_replies,
            SUM(dm.bounced) AS total_bounced
        FROM daily_metrics dm
        JOIN accounts a ON a.email = dm.email_account
        WHERE dm.date >= ?
          {extra_date_clause}
          AND dm.sent > 0
          AND a.status = 1
    """

    trend_query, td_params = _maybe_filter_tags(request, trend_query, params, alias="dm")
    trend_query += "\n        GROUP BY dm.date\n        ORDER BY dm.date ASC"

    rows = db.execute(trend_query, td_params).fetchall()

    data = []
    for row in rows:
        sent = row["total_sent"] or 0
        human_replies = row["total_human_replies"] or 0
        auto_replies = row["total_auto_replies"] or 0
        total_replies = human_replies + auto_replies
        bounced = row["total_bounced"] or 0
        human_reply_rate = round(human_replies / sent * 100, 2) if sent >= 10 else None
        total_reply_rate = round(total_replies / sent * 100, 2) if sent >= 10 else None
        auto_reply_rate = round(auto_replies / sent * 100, 2) if sent >= 10 else None
        data.append({
            "date": row["date"],
            "sent": sent,
            "replies": human_replies,
            "reply_rate": human_reply_rate,
            "bounced": bounced,
            "bounce_rate": round(bounced / sent * 100, 2) if sent >= 10 else None,
            "human_replies": human_replies,
            "auto_replies": auto_replies,
            "total_replies": total_replies,
            "human_reply_rate": human_reply_rate,
            "total_reply_rate": total_reply_rate,
            "auto_reply_rate": auto_reply_rate,
        })

    # Fill missing dates with zero entries (matches per-domain endpoint)
    start_date = datetime.strptime(cutoff, "%Y-%m-%d").date()
    end_date = datetime.strptime(end_cutoff, "%Y-%m-%d").date() if end_cutoff else datetime.utcnow().date()
    data_by_date = {d["date"]: d for d in data}
    filled_data = []
    current = start_date
    while current <= end_date:
        date_str = current.strftime("%Y-%m-%d")
        if date_str in data_by_date:
            filled_data.append(data_by_date[date_str])
        else:
            filled_data.append({
                "date": date_str, "sent": 0,
                "replies": 0, "reply_rate": None,
                "bounced": 0, "bounce_rate": None,
                "human_replies": 0, "auto_replies": 0, "total_replies": 0,
                "human_reply_rate": None, "total_reply_rate": None, "auto_reply_rate": None,
            })
        current += timedelta(days=1)

    return jsonify(filled_data)


@app.route("/domain/<domain>/trend/data")
def domain_trend_data(domain: str):
    """JSON endpoint for the trend chart data.

    Supports:
      ?days=N        — Number of days to look back (default 30)
      ?range=1m|3m|6m|all  — Convenience param: 1m=30d, 3m=90d, 6m=180d, all=365d
      ?start=&end=   — Explicit date range (overrides days/range)

    If both `range` and `days` are provided, `range` takes precedence.
    """
    if not db_exists():
        return jsonify({"error": "No data"}), 404

    # Resolve range convenience param
    range_param = request.args.get("range", "")
    range_map = {
        "30d": 30,
        "90d": 90,
        "180d": 180,
        "1m": 30,
        "3m": 90,
        "6m": 180,
        "all": 365,
    }
    if range_param and range_param in range_map:
        days = range_map[range_param]
    else:
        days = request.args.get("days", TREND_DAYS, type=int)

    start_param = request.args.get("start")
    end_param = request.args.get("end")

    db = get_db()

    if start_param and end_param:
        cutoff = start_param
        end_cutoff = end_param
    else:
        cutoff = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d")
        end_cutoff = None

    extra_date_clause = "AND dm.date <= ? " if end_cutoff else ""
    params = (domain, cutoff, end_cutoff) if end_cutoff else (domain, cutoff)
    trend_query = f"""
        SELECT
            dm.date,
            SUM(dm.sent) AS total_sent,
            SUM(dm.unique_replies) AS total_human_replies,
            SUM(dm.unique_replies_automatic) AS total_auto_replies,
            SUM(dm.bounced) AS total_bounced
        FROM daily_metrics dm
        JOIN accounts a ON a.email = dm.email_account
        WHERE a.domain = ?
          AND dm.date >= ?
          {extra_date_clause}
          AND dm.sent > 0
          AND a.status = 1
    """

    trend_query, td_params = _maybe_filter_tags(request, trend_query, params, alias="dm")
    trend_query += "\n        GROUP BY dm.date\n        ORDER BY dm.date ASC"

    rows = db.execute(trend_query, td_params).fetchall()

    data = []
    for row in rows:
        sent = row["total_sent"] or 0
        human_replies = row["total_human_replies"] or 0
        auto_replies = row["total_auto_replies"] or 0
        total_replies = human_replies + auto_replies
        bounced = row["total_bounced"] or 0
        human_reply_rate = round(human_replies / sent * 100, 2) if sent >= 10 else None
        total_reply_rate = round(total_replies / sent * 100, 2) if sent >= 10 else None
        auto_reply_rate = round(auto_replies / sent * 100, 2) if sent >= 10 else None
        data.append({
            "date": row["date"],
            "sent": sent,
            # Old fields (backward compat)
            "replies": human_replies,
            "reply_rate": human_reply_rate,
            "bounced": bounced,
            "bounce_rate": round(bounced / sent * 100, 2) if sent >= 10 else None,
            # New fields
            "human_replies": human_replies,
            "auto_replies": auto_replies,
            "total_replies": total_replies,
            "human_reply_rate": human_reply_rate,
            "total_reply_rate": total_reply_rate,
            "auto_reply_rate": auto_reply_rate,
        })

    # ── Fill in missing dates with zero values ──
    # Determine the complete date range from start to end
    start_date = datetime.strptime(cutoff, "%Y-%m-%d").date()
    if end_cutoff:
        end_date = datetime.strptime(end_cutoff, "%Y-%m-%d").date()
    else:
        end_date = datetime.utcnow().date()

    # Build lookup dict by date string
    data_by_date = {d["date"]: d for d in data}

    # Generate complete daily range, filling gaps with 0-value entries
    filled_data = []
    current = start_date
    while current <= end_date:
        date_str = current.strftime("%Y-%m-%d")
        if date_str in data_by_date:
            filled_data.append(data_by_date[date_str])
        else:
            filled_data.append({
                "date": date_str,
                "sent": 0,
                # Old fields (backward compat)
                "replies": 0,
                "reply_rate": None,
                "bounced": 0,
                "bounce_rate": None,
                # New fields
                "human_replies": 0,
                "auto_replies": 0,
                "total_replies": 0,
                "human_reply_rate": None,
                "total_reply_rate": None,
                "auto_reply_rate": None,
            })
        current += timedelta(days=1)

    return jsonify(filled_data)


# ── Multi-Timeframe Endpoint ──────────────────────────────────────────


@app.route("/domain/<domain>/multi-timeframe")
def domain_multi_timeframe(domain: str):
    """
    JSON: Aggregated stats for 7d/14d/30d/90d windows with trend arrows.

    Uses a single CASE-based SQL query for efficiency, computing both
    current-window and prior-window aggregates in one scan.
    """
    if not db_exists():
        return jsonify({"error": "No data"}), 404

    db = get_db()

    # Check domain exists
    acct = db.execute(
        "SELECT COUNT(*) AS cnt FROM accounts WHERE domain = ?",
        (domain,),
    ).fetchone()
    if not acct or acct["cnt"] == 0:
        return jsonify({"error": "Domain not found"}), 404

    today = datetime.utcnow().date()
    cutoff_7d = today - timedelta(days=7)
    cutoff_14d = today - timedelta(days=14)
    cutoff_30d = today - timedelta(days=30)
    cutoff_90d = today - timedelta(days=90)
    cutoff_7d_prior = today - timedelta(days=14)    # start of prior 7d window
    cutoff_14d_prior = today - timedelta(days=28)    # start of prior 14d
    cutoff_30d_prior = today - timedelta(days=60)    # start of prior 30d
    cutoff_90d_prior = today - timedelta(days=180)   # start of prior 90d
    cutoff_earliest = cutoff_90d_prior  # 180 days ago

    # Parse tags from request
    tags_param = request.args.get("tags", "")
    tags_list = [t.strip() for t in tags_param.split(",") if t.strip()] if tags_param else None

    # Get provider code for domain-level thresholds
    provider_row = db.execute(
        "SELECT provider_code FROM accounts WHERE domain = ? AND status = 1 AND provider_code IS NOT NULL LIMIT 1",
        (domain,),
    ).fetchone()
    provider_code = provider_row["provider_code"] if provider_row else None

    result = _compute_multi_timeframe_for_domain(db, domain, provider_code, tags=tags_list)
    if result is None:
        return jsonify({"error": "Domain not found"}), 404
    return jsonify(result)


# ── Burn Trajectory Endpoint ──────────────────────────────────────────


@app.route("/domain/<domain>/burn-trajectory")
def domain_burn_trajectory(domain: str):
    """
    JSON: Burn trajectory classification for a domain.

    Supports tag filter via ?tags= param.
    """
    if not db_exists():
        return jsonify({"error": "No data"}), 404

    db = get_db()

    # Check domain exists
    acct = db.execute(
        "SELECT COUNT(*) AS cnt FROM accounts WHERE domain = ?",
        (domain,),
    ).fetchone()
    if not acct or acct["cnt"] == 0:
        return jsonify({"error": "Domain not found"}), 404

    # Parse tags from request
    tags_param = request.args.get("tags", "")
    tags_list = [t.strip() for t in tags_param.split(",") if t.strip()] if tags_param else None

    result = _compute_trajectory_for_domain(db, domain, tags=tags_list)
    if result is None:
        return jsonify({"error": "No trajectory data"}), 404
    return jsonify(result)


@app.route("/tags/data")
def tags_data():
    """JSON: List all tags with account counts (scoped to current window)."""
    if not db_exists():
        return jsonify({"tags": []})

    db = get_db()
    lookback = request.args.get("window", DEFAULT_LOOKBACK_DAYS, type=int)
    start_param = request.args.get("start")
    end_param = request.args.get("end")

    if start_param and end_param:
        cutoff = start_param
    else:
        cutoff = (datetime.utcnow() - timedelta(days=lookback)).strftime("%Y-%m-%d")

    rows = db.execute(
        """
        SELECT
            et.tag,
            COUNT(DISTINCT dm.email_account) AS account_count
        FROM email_tags et
        JOIN daily_metrics dm ON dm.email_account = et.email_account
        JOIN accounts a ON a.email = dm.email_account
        WHERE dm.date >= ?
          AND dm.sent > 0
          AND a.status = 1
        GROUP BY et.tag
        ORDER BY account_count DESC
        """,
        (cutoff,),
    ).fetchall()

    # Also count "unlabeled" accounts (in daily_metrics but no email_tags)
    unlabeled = db.execute(
        """
        SELECT COUNT(DISTINCT dm.email_account) AS count
        FROM daily_metrics dm
        JOIN accounts a ON a.email = dm.email_account
        WHERE dm.date >= ?
          AND dm.sent > 0
          AND a.status = 1
          AND NOT EXISTS (
              SELECT 1 FROM email_tags et
              WHERE et.email_account = dm.email_account
          )
        """,
        (cutoff,),
    ).fetchone()

    tags = [{"tag": r["tag"], "count": r["account_count"]} for r in rows]
    if unlabeled and unlabeled["count"] > 0:
        tags.append({"tag": "unlabeled", "count": unlabeled["count"]})

    return jsonify({"tags": tags})


@app.route("/api/domains/hosting-info")
def api_domains_hosting_info():
    """JSON: Return hosting info (IP + country) for all domains.

    Returns {domain: {ip: str|null, country: str|null}, ...}
    Read from accounts table — data populated by `detect-providers` CLI command.
    Frontend uses this to lazy-load the "Hosted" column.
    """
    if not db_exists():
        return jsonify({})

    db = get_db()
    rows = db.execute(
        """
        SELECT domain, hosted_ip, hosted_country
        FROM accounts
        WHERE hosted_ip IS NOT NULL OR hosted_country IS NOT NULL
        GROUP BY domain
        ORDER BY domain
        """
    ).fetchall()

    result = {}
    for r in rows:
        result[r["domain"]] = {
            "ip": r["hosted_ip"],
            "country": r["hosted_country"],
        }
    return jsonify(result)


@app.route("/api/domains/<domain>/note", methods=["POST"])
def api_save_domain_note(domain: str):
    """Save (or clear) the free-text note for a domain."""
    if not db_exists():
        return jsonify({"ok": False, "error": "No data"}), 404

    data = request.get_json(silent=True) or {}
    note = (data.get("note") or "").strip()

    db = get_db()
    db.execute(
        """
        INSERT INTO domain_notes (domain, note, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(domain) DO UPDATE SET
            note       = excluded.note,
            updated_at = excluded.updated_at
        """,
        (domain, note, datetime.utcnow().isoformat()),
    )
    db.commit()
    return jsonify({"ok": True, "note": note})


@app.route("/api/domains/refresh-hosting", methods=["POST"])
def api_refresh_hosting():
    """Refresh hosted IP and country for all domains via DNS MX + ipinfo.io.

    Runs batch_detect_providers(force=True) in a background thread so
    provider, hosted_ip, and hosted_country all get re-detected.
    Progress tracked via _backfill_progress dict.
    """
    user_slug = g.get("current_user_id", "default")

    hosting_key = f"{user_slug}:refresh_hosting"

    with _backfill_progress_lock:
        _backfill_progress[hosting_key] = {
            "status": "running",
            "step": "Starting hosting detection...",
            "pct": 0,
        }

    def _progress(msg):
        with _backfill_progress_lock:
            _backfill_progress[hosting_key] = {
                "status": "running",
                "step": msg,
                "pct": 50,
            }

    def _run():
        try:
            from domain_tracker.etl import batch_detect_providers
            from domain_tracker.db import init_db

            init_db(user_slug)

            _progress("Detecting MX hosts via DNS + ipinfo.io...")
            updated = batch_detect_providers(
                force=True,
                progress=_progress,
                user_slug=user_slug,
            )

            with _backfill_progress_lock:
                _backfill_progress[hosting_key] = {
                    "status": "completed",
                    "step": f"✅ Hosting info refreshed for {updated} domains",
                    "pct": 100,
                }
        except Exception as exc:
            with _backfill_progress_lock:
                _backfill_progress[hosting_key] = {
                    "status": "error",
                    "step": f"❌ Error: {exc}",
                    "pct": 0,
                }

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"ok": True, "message": "Hosting refresh started"})


@app.route("/add-user")
def add_user_page():
    return render_template("add_user.html")


# ── User Management API ────────────────────────────────────────────────

@app.route("/api/users", methods=["GET"])
def api_list_users():
    """List all active users."""
    registry = UserRegistry()
    users = []
    for u in registry.list_users():
        db_ok = u.db_path.exists()
        users.append({
            "id": u.id,
            "name": u.name,
            "is_admin": u.is_admin,
            "active": u.active,
            "db_exists": db_ok,
            "current": u.id == g.get("current_user_id", "default"),
        })
    return jsonify({"users": users, "current": g.get("current_user_id", "default")})


@app.route("/api/users/add", methods=["POST"])
def api_add_user():
    """Add a new user with API key validation."""
    data = request.get_json()
    if not data:
        return jsonify({"error": "Request body required"}), 400

    slug = data.get("slug", "")
    name = data.get("name", "").strip()
    api_key = data.get("api_key", "").strip()

    if not slug:
        slug = name.lower().replace(" ", "_").replace("/", "_").replace("\\", "_")
    else:
        slug = slug.lower().replace(" ", "_").replace("/", "_").replace("\\", "_")

    if not slug or not name or not api_key:
        return jsonify({"error": "Name and API key are required"}), 400

    registry = UserRegistry()
    if registry.get_user(slug):
        return jsonify({"error": f"User '{slug}' already exists"}), 409

    # Validate API key by calling list_accounts
    try:
        from domain_tracker.api import InstantlyClient
        client = InstantlyClient(api_key=api_key)
        accounts = client.list_accounts()
    except Exception as e:
        return jsonify({"error": f"Invalid API key: {str(e)}"}), 400

    # Create user
    user = User(id=slug, name=name, instantly_api_key=api_key)
    registry.add_user(user)

    # Init DB + start backfill in background with progress tracking
    from domain_tracker.db import init_db
    init_db(slug)

    from domain_tracker.etl import run_etl
    progress_cb = _make_backfill_progress_callback(slug)

    # Initialize progress
    with _backfill_progress_lock:
        _backfill_progress[slug] = {
            "status": "running",
            "step": "Starting backfill...",
            "pct": 0,
        }

    def _run_backfill():
        try:
            run_etl(
                user_slug=slug,
                full_backfill=True,
                progress=progress_cb,
            )
            with _backfill_progress_lock:
                _backfill_progress[slug] = {
                    "status": "completed",
                    "step": "✅ Backfill complete",
                    "pct": 100,
                }
        except Exception as exc:
            with _backfill_progress_lock:
                _backfill_progress[slug] = {
                    "status": "error",
                    "step": f"❌ Error: {exc}",
                    "pct": 0,
                }

    threading.Thread(target=_run_backfill, daemon=True).start()

    return jsonify({
        "ok": True,
        "slug": slug,
        "name": name,
        "accounts_found": len(accounts),
    })


@app.route("/api/users/remove", methods=["POST"])
def api_remove_user():
    """Remove a user from the registry."""
    data = request.get_json()
    if not data:
        return jsonify({"error": "Request body required"}), 400

    slug = data.get("slug", "")
    if slug == "default":
        return jsonify({"error": "Cannot remove default user"}), 400

    registry = UserRegistry()
    if not registry.get_user(slug):
        return jsonify({"error": f"User '{slug}' not found"}), 404

    registry.remove_user(slug)
    return jsonify({"ok": True, "slug": slug})


@app.route("/switch-user/<slug>", methods=["POST"])
def switch_user(slug: str):
    """Switch the current user in the session."""
    registry = UserRegistry()
    user = registry.get_user(slug)
    if not user or not user.active:
        return jsonify({"error": "User not found or inactive"}), 404

    session["current_user_id"] = slug
    return jsonify({"ok": True, "slug": slug, "name": user.name})


@app.route("/api/backfill", methods=["POST"])
def api_trigger_backfill():
    """Trigger a full backfill for the current user in a background thread."""
    user_slug = g.get("current_user_id", "default")
    from domain_tracker.etl import run_etl

    progress_cb = _make_backfill_progress_callback(user_slug)

    # Initialize progress
    with _backfill_progress_lock:
        _backfill_progress[user_slug] = {
            "status": "running",
            "step": "Starting backfill...",
            "pct": 0,
        }

    def _run_with_completion():
        try:
            run_etl(
                user_slug=user_slug,
                full_backfill=True,
                progress=progress_cb,
            )
            with _backfill_progress_lock:
                _backfill_progress[user_slug] = {
                    "status": "completed",
                    "step": "✅ Backfill complete",
                    "pct": 100,
                }
        except Exception as exc:
            with _backfill_progress_lock:
                _backfill_progress[user_slug] = {
                    "status": "error",
                    "step": f"❌ Error: {exc}",
                    "pct": 0,
                }

    threading.Thread(target=_run_with_completion, daemon=True).start()
    return jsonify({"ok": True, "message": f"Backfill started for {user_slug}"})


@app.route("/api/settings/clear-and-backfill", methods=["POST"])
def api_clear_and_backfill():
    """Clear all data for current user and restart backfill."""
    user_slug = g.get("current_user_id", "default")
    db_path = _get_user_db_path(user_slug)

    conn = sqlite3.connect(db_path)
    try:
        tables = ["daily_metrics", "account_campaigns", "email_tags", "accounts", "burn_analysis"]
        for table in tables:
            conn.execute(f"DELETE FROM {table}")
        conn.commit()
    finally:
        conn.close()

    from domain_tracker.etl import run_etl
    progress_cb = _make_backfill_progress_callback(user_slug)

    with _backfill_progress_lock:
        _backfill_progress[user_slug] = {
            "status": "running",
            "step": "Starting backfill...",
            "pct": 0,
        }

    def _run_with_completion():
        try:
            run_etl(
                user_slug=user_slug,
                full_backfill=True,
                progress=progress_cb,
            )
            with _backfill_progress_lock:
                _backfill_progress[user_slug] = {
                    "status": "completed",
                    "step": "✅ Backfill complete",
                    "pct": 100,
                }
        except Exception as exc:
            with _backfill_progress_lock:
                _backfill_progress[user_slug] = {
                    "status": "error",
                    "step": f"❌ Error: {exc}",
                    "pct": 0,
                }

    threading.Thread(target=_run_with_completion, daemon=True).start()
    return jsonify({"ok": True, "message": "Data cleared. Backfill started."})


@app.route("/api/settings/refresh-tags", methods=["POST"])
def api_refresh_tags():
    """Refresh email tags from Instantly (non-destructive)."""
    user_slug = g.get("current_user_id", "default")
    registry = UserRegistry()
    user = registry.get_user(user_slug)
    if not user or not user.instantly_api_key:
        return jsonify({"error": "User not found or no API key"}), 400

    from domain_tracker.api import InstantlyClient
    from domain_tracker.etl import refresh_tags

    client = InstantlyClient(api_key=user.instantly_api_key)

    tag_key = f"{user_slug}:refresh_tags"

    with _backfill_progress_lock:
        _backfill_progress[tag_key] = {
            "status": "running",
            "step": "Syncing email tags...",
            "pct": 0,
        }

    def _tag_callback(msg):
        with _backfill_progress_lock:
            _backfill_progress[tag_key] = {
                "status": "running",
                "step": msg,
                "pct": 50,
            }

    def _run_tags():
        try:
            refresh_tags(
                client=client,
                progress=_tag_callback,
                user_slug=user_slug,
            )
            with _backfill_progress_lock:
                _backfill_progress[tag_key] = {
                    "status": "completed",
                    "step": "✅ Tags refreshed",
                    "pct": 100,
                }
        except Exception as exc:
            with _backfill_progress_lock:
                _backfill_progress[tag_key] = {
                    "status": "error",
                    "step": f"❌ Error: {exc}",
                    "pct": 0,
                }

    threading.Thread(target=_run_tags, daemon=True).start()
    return jsonify({"ok": True})


# ── Backfill Gap Detection Helpers ────────────────────────────────────

BACKFILL_WINDOW_DAYS = 90


def _detect_backfill_gaps(user_slug: str = "default") -> dict:
    """Query daily_metrics for the user and find missing dates.

    Returns dict with pending_dates, gap_count, date_range info.
    """
    from domain_tracker.db import get_connection

    conn = get_connection(user_slug)
    cur = conn.execute("SELECT DISTINCT date FROM daily_metrics ORDER BY date")
    data_dates = set(r["date"] for r in cur.fetchall())

    today = datetime.utcnow().date()
    start = today - timedelta(days=BACKFILL_WINDOW_DAYS)

    gap_dates = []
    for i in range(BACKFILL_WINDOW_DAYS + 1):
        d = (start + timedelta(days=i)).isoformat()
        if d not in data_dates:
            gap_dates.append(d)

    # Earliest and latest dates with data
    date_range = conn.execute(
        "SELECT MIN(date) AS min_d, MAX(date) AS max_d FROM daily_metrics"
    ).fetchone()

    return {
        "ok": True,
        "total_pending_days": len(gap_dates),
        "pending_dates": gap_dates,
        "date_range": {
            "window_start": start.isoformat(),
            "window_end": today.isoformat(),
            "total_days_in_window": BACKFILL_WINDOW_DAYS + 1,
            "data_days": len(data_dates),
            "gap_days": len(gap_dates),
        },
        "oldest_data_date": date_range["min_d"] if date_range["min_d"] else None,
        "newest_data_date": date_range["max_d"] if date_range["max_d"] else None,
    }


@app.route("/api/sync/backfill-gaps", methods=["GET"])
def api_backfill_gaps():
    """Return gap information for the current user.

    Shows which dates in the last 90 days are missing data.
    """
    user_slug = g.get("current_user_id", "default")
    try:
        result = _detect_backfill_gaps(user_slug)
        return jsonify(result)
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/backfill/pending-days", methods=["GET"])
def api_backfill_pending_days():
    """Get pending days for the backfill panel.

    Returns clean format for the frontend panel component.
    """
    user_slug = g.get("current_user_id", "default")
    try:
        result = _detect_backfill_gaps(user_slug)
        return jsonify({
            "ok": True,
            "pending_dates": result.get("pending_dates", []),
            "total_pending": result.get("total_pending_days", 0),
            "latest_data_date": result.get("newest_data_date"),
            "oldest_data_date": result.get("oldest_data_date"),
        })
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/sync/backfill-pending", methods=["POST"])
def api_backfill_pending():
    """Backfill ALL missing days in the last 90 days.

    Fetches daily metrics for the full gap range from the Instantly API,
    then runs burn analysis.
    """
    user_slug = g.get("current_user_id", "default")
    registry = UserRegistry()
    user = registry.get_user(user_slug)
    if not user or not user.instantly_api_key:
        return jsonify({"error": "User not found or no API key"}), 400

    from domain_tracker.api import InstantlyClient
    from domain_tracker.etl import (
        pull_daily_metrics,
        run_burn_analysis,
        refresh_accounts,
        refresh_tags,
    )
    from domain_tracker.db import init_db

    client = InstantlyClient(api_key=user.instantly_api_key)

    today = datetime.utcnow()
    start_date = (today - timedelta(days=BACKFILL_WINDOW_DAYS)).strftime("%Y-%m-%d")
    end_date = today.strftime("%Y-%m-%d")

    sync_key = f"{user_slug}:backfill_pending"

    with _backfill_progress_lock:
        _backfill_progress[sync_key] = {
            "status": "running",
            "step": f"Backfilling pending days ({start_date} -> {end_date})...",
            "pct": 0,
        }

    def _progress(msg):
        with _backfill_progress_lock:
            current = _backfill_progress.get(sync_key, {})
            pct = current.get("pct", 0)
            _backfill_progress[sync_key] = {
                "status": "running",
                "step": msg,
                "pct": pct,
            }

    def _run_backfill():
        try:
            init_db(user_slug)

            # Refresh accounts
            _progress("Refreshing accounts...")
            with _backfill_progress_lock:
                _backfill_progress[sync_key]["pct"] = 5
            refresh_accounts(client=client, progress=_progress, user_slug=user_slug)
            with _backfill_progress_lock:
                _backfill_progress[sync_key]["pct"] = 10

            # Refresh tags
            _progress("Syncing custom tags...")
            refresh_tags(client=client, progress=_progress, user_slug=user_slug)
            with _backfill_progress_lock:
                _backfill_progress[sync_key]["pct"] = 15

            # Pull daily metrics for the full 90-day window (15% -> 85%)
            _progress(
                "Fetching daily metrics for {} -> {}...".format(start_date, end_date)
            )
            total = pull_daily_metrics(
                client=client,
                start_date=start_date,
                end_date=end_date,
                progress=_progress,
                user_slug=user_slug,
            )
            with _backfill_progress_lock:
                _backfill_progress[sync_key]["pct"] = 85

            # Run burn analysis
            _progress("Running burn analysis...")
            run_burn_analysis(progress=_progress, user_slug=user_slug)

            with _backfill_progress_lock:
                _backfill_progress[sync_key] = {
                    "status": "completed",
                    "step": "Backfilled {} metric rows for last {} days".format(
                        total, BACKFILL_WINDOW_DAYS
                    ),
                    "pct": 100,
                }
        except Exception as exc:
            with _backfill_progress_lock:
                _backfill_progress[sync_key] = {
                    "status": "error",
                    "step": "Error: {}".format(exc),
                    "pct": 0,
                }
            import traceback

            traceback.print_exc()

    threading.Thread(target=_run_backfill, daemon=True).start()
    return jsonify({
        "ok": True,
        "message": "Backfilling pending days ({} -> {})...".format(start_date, end_date),
    })


@app.route("/api/sync-today", methods=["POST"])
def api_sync_today():
    """Sync today's data (last 3 days) in a background thread.

    Lightweight version of sync-days that defaults to 3 days
    and requires no request body. Useful for the "Fill Today's Data"
    button on the overview page when cron isn't working.
    """
    user_slug = g.get("current_user_id", "default")
    registry = UserRegistry()
    user = registry.get_user(user_slug)
    if not user or not user.instantly_api_key:
        return jsonify({"error": "User not found or no API key"}), 400

    from domain_tracker.api import InstantlyClient
    from domain_tracker.etl import pull_daily_metrics, run_burn_analysis, refresh_accounts, refresh_tags

    client = InstantlyClient(api_key=user.instantly_api_key)

    today = datetime.utcnow()
    start_date = (today - timedelta(days=3)).strftime("%Y-%m-%d")
    end_date = today.strftime("%Y-%m-%d")

    sync_key = f"{user_slug}:sync_today"

    with _backfill_progress_lock:
        _backfill_progress[sync_key] = {
            "status": "running",
            "step": f"Syncing today's data ({start_date} \u2192 {end_date})...",
            "pct": 0,
        }

    def _progress(msg):
        with _backfill_progress_lock:
            current = _backfill_progress.get(sync_key, {})
            pct = current.get("pct", 0)
            _backfill_progress[sync_key] = {
                "status": "running",
                "step": msg,
                "pct": pct,
            }

    def _run_sync():
        try:
            from domain_tracker.db import init_db
            init_db(user_slug)

            # Step 1: Refresh accounts (0% \u2192 15%)
            _progress("Refreshing accounts...")
            with _backfill_progress_lock:
                _backfill_progress[sync_key]["pct"] = 5
            refresh_accounts(client=client, progress=_progress, user_slug=user_slug)
            with _backfill_progress_lock:
                _backfill_progress[sync_key]["pct"] = 15

            # Step 2: Refresh tags (15% \u2192 25%)
            _progress("Syncing custom tags...")
            refresh_tags(client=client, progress=_progress, user_slug=user_slug)
            with _backfill_progress_lock:
                _backfill_progress[sync_key]["pct"] = 25

            # Step 3: Pull daily metrics for last 3 days (25% \u2192 80%)
            _progress(f"Fetching daily metrics for {start_date} \u2192 {end_date}...")
            with _backfill_progress_lock:
                _backfill_progress[sync_key]["pct"] = 30
            total = pull_daily_metrics(
                client=client,
                start_date=start_date,
                end_date=end_date,
                progress=_progress,
                user_slug=user_slug,
            )
            with _backfill_progress_lock:
                _backfill_progress[sync_key]["pct"] = 80

            # Step 4: Run burn analysis (80% \u2192 100%)
            _progress("Running burn analysis...")
            classified = run_burn_analysis(progress=_progress, user_slug=user_slug)

            with _backfill_progress_lock:
                _backfill_progress[sync_key] = {
                    "status": "completed",
                    "step": f"\u2705 Synced {total} metric rows for last 3 days",
                    "pct": 100,
                }
        except Exception as exc:
            with _backfill_progress_lock:
                _backfill_progress[sync_key] = {
                    "status": "error",
                    "step": f"\u274c Error: {exc}",
                    "pct": 0,
                }

    threading.Thread(target=_run_sync, daemon=True).start()
    return jsonify({
        "ok": True,
        "message": f"Syncing today's data ({start_date} \u2192 {end_date})...",
    })


@app.route("/api/settings/sync-days", methods=["POST"])
def api_sync_days():
    """Sync daily metrics + burn analysis for the last N days.

    Useful when cron failed for a few days — pulls only Step 4 + Step 5
    for a specific date range without doing a full backfill.
    """
    data = request.get_json()
    if not data or "days" not in data:
        return jsonify({"error": "Missing 'days' in request body"}), 400

    try:
        n_days = int(data["days"])
    except (ValueError, TypeError):
        return jsonify({"error": "'days' must be an integer"}), 400

    if n_days < 1 or n_days > 365:
        return jsonify({"error": "'days' must be between 1 and 365"}), 400

    user_slug = g.get("current_user_id", "default")
    registry = UserRegistry()
    user = registry.get_user(user_slug)
    if not user or not user.instantly_api_key:
        return jsonify({"error": "User not found or no API key"}), 400

    from domain_tracker.api import InstantlyClient
    from domain_tracker.etl import pull_daily_metrics, run_burn_analysis

    client = InstantlyClient(api_key=user.instantly_api_key)

    today = datetime.utcnow()
    start_date = (today - timedelta(days=n_days)).strftime("%Y-%m-%d")
    end_date = today.strftime("%Y-%m-%d")

    sync_key = f"{user_slug}:sync_days"

    with _backfill_progress_lock:
        _backfill_progress[sync_key] = {
            "status": "running",
            "step": f"Syncing last {n_days} days ({start_date} → {end_date})...",
            "pct": 0,
        }

    def _progress(msg):
        with _backfill_progress_lock:
            current = _backfill_progress.get(sync_key, {})
            pct = current.get("pct", 0)
            _backfill_progress[sync_key] = {
                "status": "running",
                "step": msg,
                "pct": pct,
            }

    def _run_sync():
        try:
            # Step 1: Pull daily metrics (0% → 70%)
            _progress("Fetching daily metrics for {} → {}...".format(start_date, end_date))
            with _backfill_progress_lock:
                _backfill_progress[sync_key]["pct"] = 10
            total = pull_daily_metrics(
                client=client,
                start_date=start_date,
                end_date=end_date,
                progress=_progress,
                user_slug=user_slug,
            )
            with _backfill_progress_lock:
                _backfill_progress[sync_key]["pct"] = 70

            # Step 2: Run burn analysis (70% → 100%)
            _progress("Running burn analysis...")
            classified = run_burn_analysis(progress=_progress, user_slug=user_slug)

            with _backfill_progress_lock:
                _backfill_progress[sync_key] = {
                    "status": "completed",
                    "step": f"✅ Synced {total} metric rows for last {n_days} days",
                    "pct": 100,
                }
        except Exception as exc:
            with _backfill_progress_lock:
                _backfill_progress[sync_key] = {
                    "status": "error",
                    "step": f"❌ Error: {exc}",
                    "pct": 0,
                }

    threading.Thread(target=_run_sync, daemon=True).start()
    return jsonify({"ok": True, "message": f"Syncing last {n_days} days ({start_date} → {end_date})..."})


@app.route("/api/settings/pull-domain", methods=["POST"])
def api_pull_domain():
    """Pull data for a SINGLE domain only, over the last 180 days.

    Resolves the domain's mailboxes (fetching the account list from Instantly
    if the domain isn't known locally yet), pulls daily metrics scoped to
    those mailboxes, and re-runs burn analysis. Campaigns are not synced.
    """
    data = request.get_json()
    if not data or not data.get("domain"):
        return jsonify({"error": "Missing 'domain' in request body"}), 400

    domain = str(data["domain"]).strip().lower()
    if not domain or "." not in domain or " " in domain:
        return jsonify({"error": "Invalid domain"}), 400

    days = 180
    if data.get("days") is not None:
        try:
            days = int(data["days"])
        except (ValueError, TypeError):
            days = 180
    days = max(1, min(days, 365))

    user_slug = g.get("current_user_id", "default")
    registry = UserRegistry()
    user = registry.get_user(user_slug)
    if not user or not user.instantly_api_key:
        return jsonify({"error": "User not found or no API key"}), 400

    from domain_tracker.etl import run_domain_etl

    pull_key = f"{user_slug}:pull_domain"

    with _backfill_progress_lock:
        _backfill_progress[pull_key] = {
            "status": "running",
            "step": f"Pulling last {days} days for {domain}...",
            "pct": 0,
        }

    def _progress(msg):
        with _backfill_progress_lock:
            current = _backfill_progress.get(pull_key, {})
            _backfill_progress[pull_key] = {
                "status": "running",
                "step": msg,
                "pct": current.get("pct", 10),
            }

    def _run_pull():
        try:
            with _backfill_progress_lock:
                _backfill_progress[pull_key]["pct"] = 10
            result = run_domain_etl(
                user_slug=user_slug,
                domain=domain,
                days=days,
                progress=_progress,
            )
            if result.get("mailboxes", 0) == 0:
                with _backfill_progress_lock:
                    _backfill_progress[pull_key] = {
                        "status": "error",
                        "step": f"❌ No mailboxes found for {domain}",
                        "pct": 0,
                    }
                return
            with _backfill_progress_lock:
                _backfill_progress[pull_key] = {
                    "status": "completed",
                    "step": (
                        f"✅ {domain}: {result['rows']} metric rows across "
                        f"{result['mailboxes']} mailbox(es)"
                    ),
                    "pct": 100,
                }
        except Exception as exc:
            with _backfill_progress_lock:
                _backfill_progress[pull_key] = {
                    "status": "error",
                    "step": f"❌ Error: {exc}",
                    "pct": 0,
                }

    threading.Thread(target=_run_pull, daemon=True).start()
    return jsonify({"ok": True, "message": f"Pulling last {days} days for {domain}..."})


# ── Backfill Progress Tracking ────────────────────────────────────────

_backfill_progress = {}
_backfill_progress_lock = threading.Lock()


def _make_backfill_progress_callback(user_slug: str):
    """Create a progress callback for the ETL pipeline.

    Maps log messages from run_etl to percentage estimates:
      Step 1-3:  10% each  (accounts, tags, campaigns = 30%)
      Step 4:    50%       (daily metrics — heavy step)
      Step 5:    15%       (burn analysis)
      Step 6:    5%        (report)
    """
    tracker = {
        "step1_done": False,
        "step2_done": False,
        "step3_done": False,
        "step4_done": False,
    }

    def _set(status, step_text, pct):
        with _backfill_progress_lock:
            _backfill_progress[user_slug] = {
                "status": status,
                "step": step_text,
                "pct": pct,
            }

    def callback(msg: str):
        nonlocal tracker

        # Error detection
        if msg.startswith("ERROR") or msg.startswith("  ERROR"):
            _set("error", msg.strip(), 0)
            return

        # ── Step 1: Refresh accounts (0% → 10%) ──
        if "Step 1/6" in msg:
            _set("running", "Step 1/6: Refreshing accounts...", 2)
        elif "Upserted" in msg and "accounts" in msg and not tracker["step1_done"]:
            tracker["step1_done"] = True
            _set("running", "✅ Accounts refreshed", 10)

        # ── Step 2: Refresh email tags (10% → 20%) ──
        elif "Step 2/6" in msg or "Syncing email tags" in msg:
            _set("running", "Step 2/6: Syncing custom tags...", 12)
        elif "Synced tags" in msg and not tracker["step2_done"]:
            tracker["step2_done"] = True
            _set("running", "✅ Custom tags synced", 20)

        # ── Step 3: Refresh campaign mappings (20% → 30%) ──
        elif "Step 3/6" in msg:
            if "SKIPPED" in msg:
                tracker["step3_done"] = True
                _set("running", "⏭️ Campaign mappings skipped", 30)
            else:
                _set("running", "Step 3/6: Refreshing campaign mappings...", 22)
        elif "Campaign mappings [" in msg:
            m = re.search(r"\[(\d+)/(\d+)\]", msg)
            if m:
                done = int(m.group(1))
                total = int(m.group(2))
                # 20% → 30% range (10pp)
                pct = 20 + int((done / total) * 10)
                pct = min(pct, 29)
                _set("running", f"Step 3/6: Campaign mappings [{done}/{total}]", pct)
        elif "Upserted" in msg and "campaign" in msg and not tracker["step3_done"]:
            tracker["step3_done"] = True
            _set("running", "✅ Campaign mappings refreshed", 30)

        # ── Step 4: Pull daily metrics (30% → 80%) ──
        elif "Step 4/6" in msg or "Pulling daily metrics" in msg:
            _set("running", "Step 4/6: Pulling daily metrics...", 32)
        elif "already current" in msg:
            tracker["step4_done"] = True
            _set("running", "✅ Daily metrics already up to date", 80)
        elif "Chunk" in msg:
            m = re.search(r"Chunk (\d+)/(\d+)", msg)
            if m:
                chunk_num = int(m.group(1))
                chunk_total = int(m.group(2))
                # Step 4 spans 30% → 80% (50pp). Chunks map to 32% → 78%
                chunk_progress = (chunk_num / chunk_total) * 46
                pct = 32 + int(chunk_progress)
                _set("running",
                     f"Step 4/6: Fetching daily metrics ({chunk_num}/{chunk_total} ranges)...",
                     pct)
        elif "Upserted" in msg and ("daily metric" in msg or "metric rows" in msg) and not tracker["step4_done"]:
            tracker["step4_done"] = True
            _set("running", "✅ Daily metrics pulled", 80)

        # ── Step 5: Burn analysis (80% → 95%) ──
        elif "Step 5/6" in msg or "Running burn analysis" in msg:
            _set("running", "Step 5/6: Running burn analysis...", 82)
        elif "domains analysed" in msg:
            _set("running", "✅ Burn analysis complete", 95)

        # ── Step 6: Report (95% → 100%) ──
        elif "Step 6/6" in msg:
            _set("running", "Step 6/6: Generating report...", 97)

        # Intermediate messages: no progress update needed

    return callback


# ── Template Context Processors ───────────────────────────────────────


@app.context_processor
def utility_processors():
    def format_num(n):
        if n is None or n == 0:
            return "0"
        if n >= 1000000:
            return f"{n/1000000:.1f}M"
        if n >= 1000:
            return f"{n/1000:.1f}K"
        return str(n)

    def format_time(ts):
        if not ts:
            return "—"
        from datetime import datetime, timezone
        try:
            dt = datetime.fromisoformat(ts)
        except (ValueError, TypeError):
            return str(ts)[:10]

        now = datetime.now(timezone.utc)
        diff = now - dt
        days = diff.days
        seconds = diff.seconds

        if days < 0:
            return "just now"
        if days == 0:
            hours = seconds // 3600
            if hours > 0:
                return f"{hours}h ago"
            minutes = seconds // 60
            if minutes > 0:
                return f"{minutes}m ago"
            return "just now"
        if days == 1:
            return "yesterday"
        if days < 7:
            return f"{days}d ago"
        if dt.year == now.year:
            return dt.strftime("%b %d")
        return dt.strftime("%b %d, %Y")

    return dict(
        format_num=format_num,
        format_time=format_time,
        show_logout=bool(os.environ.get("DASHBOARD_PASSWORD")),
    )


# ── Campaign Routes ────────────────────────────────────────────────────


@app.route("/campaigns")
def campaign_listing():
    """Render campaign listing page with aggregated stats."""
    if not db_exists():
        return render_template("no_data.html")

    db = get_db()

    rows = db.execute("""
        SELECT
            ca.campaign_id,
            ca.campaign_name,
            ca.campaign_status,
            ca.leads_count,
            ca.emails_sent_count AS total_sent,
            ca.reply_count_unique AS human_replies,
            (ca.reply_count_unique + ca.reply_count_automatic_unique) AS total_replies,
            ca.bounced_count,
            ca.total_opportunities,
            ca.last_synced_at
        FROM campaign_analytics ca
        ORDER BY ca.last_synced_at DESC NULLS LAST
    """).fetchall()

    status_labels = {1: "Active", 2: "Paused", 3: "Completed", 0: "Draft"}
    status_emojis = {1: "🟢", 2: "⏸️", 3: "✅", 0: "📝"}
    # Default row grouping: active campaigns need attention first, then paused
    # (resumable), then completed (historical), then draft (not yet launched).
    status_priority = {1: 0, 2: 1, 3: 2, 0: 3}

    campaigns = []
    for r in rows:
        camp = dict(r)
        camp["status_label"] = status_labels.get(r["campaign_status"], "Unknown")
        camp["status_emoji"] = status_emojis.get(r["campaign_status"], "❔")
        camp["status"] = ("active" if r["campaign_status"] == 1
                          else "paused" if r["campaign_status"] == 2
                          else "completed" if r["campaign_status"] == 3
                          else "draft")

        sent = camp["total_sent"] or 0
        replies = camp["total_replies"] or 0
        bounced = camp["bounced_count"] or 0
        camp["reply_rate"] = round(replies / sent * 100, 2) if sent > 0 else None
        camp["bounce_rate"] = round(bounced / sent * 100, 2) if sent > 0 else None
        camp["bounce_display"] = f"{camp['bounce_rate']}%({bounced})" if camp["bounce_rate"] is not None else "—"

        campaigns.append(camp)

    valid_rates = [c["reply_rate"] for c in campaigns if c["reply_rate"] is not None]
    max_rate = max(valid_rates) if valid_rates else 0
    scale_max = 10 if max_rate <= 10 else (20 if max_rate <= 20 else 30)

    sync_times = [c["last_synced_at"] for c in campaigns if c["last_synced_at"]]
    last_synced_at = max(sync_times) if sync_times else None

    # Sort by URL parameter (survives page refresh). Default: active campaigns
    # first (see status_priority above), reply rate desc within each status.
    sort_by = request.args.get("sort", "status")
    sort_order = request.args.get("order", "asc" if sort_by == "status" else "desc")
    reverse = sort_order == "desc"

    if sort_by == "campaign_name":
        campaigns.sort(key=lambda c: (c.get("campaign_name") or "").lower(), reverse=reverse)
    elif sort_by == "status":
        campaigns.sort(key=lambda c: c.get("reply_rate") if c.get("reply_rate") is not None else -1, reverse=True)
        campaigns.sort(key=lambda c: status_priority.get(c.get("campaign_status"), 99), reverse=reverse)
    elif sort_by == "leads_count":
        campaigns.sort(key=lambda c: c.get("leads_count") or 0, reverse=reverse)
    elif sort_by == "total_sent":
        campaigns.sort(key=lambda c: c.get("total_sent") or 0, reverse=reverse)
    elif sort_by == "total_replies":
        campaigns.sort(key=lambda c: c.get("total_replies") or 0, reverse=reverse)
    elif sort_by == "bounce_rate":
        campaigns.sort(key=lambda c: c.get("bounce_rate") if c.get("bounce_rate") is not None else -1, reverse=reverse)
    elif sort_by == "total_opportunities":
        campaigns.sort(key=lambda c: c.get("total_opportunities") or 0, reverse=reverse)
    else:
        campaigns.sort(key=lambda c: c.get("reply_rate") if c.get("reply_rate") is not None else -1, reverse=reverse)

    return render_template(
        "campaigns.html",
        campaigns=campaigns,
        scale_max=scale_max,
        sort_by=sort_by,
        sort_order=sort_order,
        last_synced_at=last_synced_at,
    )


@app.route("/campaign/<campaign_id>/accounts")
def campaign_accounts(campaign_id: str):
    """Render per-account breakdown page for a campaign."""
    if not db_exists():
        return render_template("no_data.html")

    db = get_db()

    # Get campaign info from campaign_analytics
    camp = db.execute(
        """SELECT campaign_id, campaign_name, campaign_status,
                  emails_sent_count, reply_count_unique, reply_count,
                  bounced_count, total_opportunities, leads_count
           FROM campaign_analytics
           WHERE campaign_id = ?""",
        (campaign_id,),
    ).fetchone()

    if not camp:
        return render_template("no_data.html"), 404

    # Stats card data
    total_sent = camp["emails_sent_count"] or 0
    total_replies = camp["reply_count_unique"] or 0
    replied_incl_auto = camp["reply_count"] or 0
    bounced = camp["bounced_count"] or 0
    opportunities = camp["total_opportunities"] or 0
    reply_rate = round(total_replies * 100.0 / max(total_sent, 1), 2)

    # Auto-reply rate
    auto_replies = replied_incl_auto - total_replies if replied_incl_auto >= total_replies else 0
    auto_reply_rate = round(auto_replies * 100.0 / max(total_replies, 1), 2) if total_replies > 0 else 0.0

    # Count accounts with data (sent > 0)
    account_count = db.execute(
        "SELECT COUNT(*) AS cnt FROM campaign_account_metrics WHERE campaign_id = ? AND total_sent > 0",
        (campaign_id,),
    ).fetchone()["cnt"]

    stats = {
        "accounts_with_data": account_count,
        "total_sent": total_sent,
        "total_replies": total_replies,
        "reply_rate": reply_rate,
        "bounced": bounced,
        "opportunities": opportunities,
        "auto_reply_rate": auto_reply_rate,
    }

    # Per-account metrics
    rows = db.execute("""
        SELECT
            cam.account_email,
            a.domain,
            cam.total_sent AS sent,
            cam.replies,
            CASE
                WHEN cam.total_sent > 0
                THEN ROUND(cam.replies * 100.0 / cam.total_sent, 2)
                ELSE 0
            END AS reply_rate,
            cam.last_activity_at AS last_activity,
            cam.subject_line,
            cam.i_status,
            cam.classification,
            CASE
                WHEN cam.total_sent = 0 THEN 'na'
                WHEN cam.replies * 100.0 / cam.total_sent >= 2 THEN 'healthy'
                WHEN cam.replies * 100.0 / cam.total_sent >= 1 THEN 'at_risk'
                ELSE 'burned'
            END AS health
        FROM campaign_account_metrics cam
        JOIN accounts a ON a.email = cam.account_email
        WHERE cam.campaign_id = ?
        ORDER BY cam.total_sent DESC
    """, (campaign_id,)).fetchall()

    accounts_data = []
    for r in rows:
        accounts_data.append({
            "email": r["account_email"],
            "domain": r["domain"],
            "sent": r["sent"],
            "replies": r["replies"],
            "reply_rate": r["reply_rate"],
            "last_activity": r["last_activity"],
            "subject_line": r["subject_line"],
            "i_status": r["i_status"],
            "classification": r["classification"],
            "health": r["health"],
        })

    # Distinct domains for dropdown
    domain_rows = db.execute(
        """SELECT DISTINCT a.domain
           FROM campaign_account_metrics cam
           JOIN accounts a ON a.email = cam.account_email
           WHERE cam.campaign_id = ?
           ORDER BY a.domain""",
        (campaign_id,),
    ).fetchall()
    domains = [r["domain"] for r in domain_rows]

    return render_template(
        "campaign_accounts.html",
        campaign_id=campaign_id,
        campaign_name=camp["campaign_name"],
        stats=stats,
        accounts=accounts_data,
        domains=domains,
    )


@app.route("/campaign/<campaign_id>/account-metrics")
def campaign_account_metrics_json(campaign_id: str):
    """JSON: per-account metrics for dynamic table filtering/sorting.

    Query params: search, domain, health, sort_by, sort_order
    """
    if not db_exists():
        return jsonify({"error": "No data"}), 404

    db = get_db()
    search = request.args.get("search", "")
    domain_filter = request.args.get("domain", "")
    health_filter = request.args.get("health", "")
    sort_by = request.args.get("sort_by", "sent")
    sort_order = request.args.get("sort_order", "desc")

    # Build query with filters
    params_list: list = [campaign_id]
    conditions = []

    if search:
        conditions.append("LOWER(cam.account_email) LIKE ?")
        params_list.append(f"%{search.lower()}%")
    if domain_filter:
        conditions.append("a.domain = ?")
        params_list.append(domain_filter)

    where_clause = " AND ".join(conditions) if conditions else "1=1"

    # Allowed sort columns
    sort_map = {
        "email": "cam.account_email",
        "domain": "a.domain",
        "sent": "cam.total_sent",
        "replies": "cam.replies",
        "reply_rate": "reply_rate",
        "last_activity": "cam.last_activity_at",
    }
    sort_col = sort_map.get(sort_by, "cam.total_sent")
    sort_dir = "DESC" if sort_order == "desc" else "ASC"

    query = f"""
        SELECT
            cam.account_email,
            a.domain,
            cam.total_sent AS sent,
            cam.replies,
            CASE
                WHEN cam.total_sent > 0
                THEN ROUND(cam.replies * 100.0 / cam.total_sent, 2)
                ELSE 0
            END AS reply_rate,
            cam.last_activity_at AS last_activity,
            CASE
                WHEN cam.total_sent = 0 THEN 'na'
                WHEN cam.replies * 100.0 / cam.total_sent >= 2 THEN 'healthy'
                WHEN cam.replies * 100.0 / cam.total_sent >= 1 THEN 'at_risk'
                ELSE 'burned'
            END AS health
        FROM campaign_account_metrics cam
        JOIN accounts a ON a.email = cam.account_email
        WHERE cam.campaign_id = ? AND {where_clause}
    """

    if health_filter:
        health_condition = {
            "healthy": "cam.total_sent > 0 AND cam.replies * 100.0 / cam.total_sent >= 2",
            "at_risk": "cam.total_sent > 0 AND cam.replies * 100.0 / cam.total_sent >= 1 AND cam.replies * 100.0 / cam.total_sent < 2",
            "burned": "cam.total_sent > 0 AND cam.replies * 100.0 / cam.total_sent < 1",
            "na": "cam.total_sent = 0",
        }
        if health_filter in health_condition:
            query += f" AND ({health_condition[health_filter]})"

    query += f" ORDER BY {sort_col} {sort_dir}"

    rows = db.execute(query, tuple(params_list)).fetchall()

    accounts = []
    for r in rows:
        accounts.append({
            "email": r["account_email"],
            "domain": r["domain"],
            "sent": r["sent"],
            "replies": r["replies"],
            "reply_rate": r["reply_rate"],
            "last_activity": r["last_activity"],
            "health": r["health"],
        })

    return jsonify({"accounts": accounts, "total": len(accounts)})


@app.route("/campaign/<campaign_id>/stats")
def campaign_stats_json(campaign_id: str):
    """JSON: campaign-level stat card totals."""
    if not db_exists():
        return jsonify({"error": "No data"}), 404

    db = get_db()

    camp = db.execute(
        """SELECT campaign_name, emails_sent_count, reply_count_unique,
                  reply_count, bounced_count, total_opportunities
           FROM campaign_analytics WHERE campaign_id = ?""",
        (campaign_id,),
    ).fetchone()

    if not camp:
        return jsonify({"error": "Campaign not found"}), 404

    total_sent = camp["emails_sent_count"] or 0
    total_replies = camp["reply_count_unique"] or 0
    replied_incl_auto = camp["reply_count"] or 0
    bounced = camp["bounced_count"] or 0
    opportunities = camp["total_opportunities"] or 0
    reply_rate = round(total_replies * 100.0 / max(total_sent, 1), 2)

    auto_replies = replied_incl_auto - total_replies if replied_incl_auto >= total_replies else 0
    auto_reply_rate = round(auto_replies * 100.0 / max(total_replies, 1), 2) if total_replies > 0 else 0.0

    account_count = db.execute(
        "SELECT COUNT(*) AS cnt FROM campaign_account_metrics WHERE campaign_id = ? AND total_sent > 0",
        (campaign_id,),
    ).fetchone()["cnt"]

    return jsonify({
        "campaign_name": camp["campaign_name"],
        "accounts_with_data": account_count,
        "total_sent": total_sent,
        "total_replies": total_replies,
        "reply_rate": reply_rate,
        "bounced": bounced,
        "opportunities": opportunities,
        "auto_reply_rate": auto_reply_rate,
    })


@app.route("/campaign/<campaign_id>/backfill-status")
def campaign_backfill_status(campaign_id: str):
    """JSON: Returns backfill status for a campaign.

    Checks if campaign_analytics and campaign_account_metrics have data.
    """
    if not db_exists():
        return jsonify({"error": "No data"}), 404

    db = get_db()

    analytics = db.execute(
        "SELECT last_synced_at FROM campaign_analytics WHERE campaign_id = ?",
        (campaign_id,),
    ).fetchone()

    metrics_count = db.execute(
        "SELECT COUNT(*) AS cnt FROM campaign_account_metrics WHERE campaign_id = ?",
        (campaign_id,),
    ).fetchone()["cnt"]

    has_analytics = analytics is not None
    analytics_synced_at = analytics["last_synced_at"] if analytics else None

    return jsonify({
        "campaign_id": campaign_id,
        "has_analytics": has_analytics,
        "analytics_synced_at": analytics_synced_at,
        "accounts_with_metrics": metrics_count,
        "needs_backfill": not has_analytics or metrics_count == 0,
    })

# ── Health / Auth ──────────────────────────────────────────────────────


@app.route("/api/backfill/status")
def api_backfill_status():
    """Get backfill, tag-sync, and sync-days progress for the current user.

    Checks both the in-memory _backfill_progress dict AND a progress file at
    /tmp/{user_slug}_backfill_progress.json (used by CLI/direct backfills).
    File contents override dict values when both exist.
    """
    user_slug = g.get("current_user_id", "default")
    with _backfill_progress_lock:
        progress = _backfill_progress.get(user_slug, {})
        tag_key = f"{user_slug}:refresh_tags"
        tag_progress = _backfill_progress.get(tag_key, {})
        sync_key = f"{user_slug}:sync_days"
        sync_progress = _backfill_progress.get(sync_key, {})
        sync_today_key = f"{user_slug}:sync_today"
        sync_today_progress = _backfill_progress.get(sync_today_key, {})
        backfill_pending_key = f"{user_slug}:backfill_pending"
        backfill_pending_progress = _backfill_progress.get(backfill_pending_key, {})
        pull_domain_key = f"{user_slug}:pull_domain"
        pull_domain_progress = _backfill_progress.get(pull_domain_key, {})
        hosting_key = f"{user_slug}:refresh_hosting"
        hosting_progress = _backfill_progress.get(hosting_key, {})

    # Also read from the progress file (used by CLI/direct backfills)
    progress_file = f"/tmp/{user_slug}_backfill_progress.json"
    file_progress = {}
    try:
        if os.path.isfile(progress_file):
            with open(progress_file) as f:
                file_progress = json.load(f)
    except (OSError, json.JSONDecodeError):
        pass

    # Merge: file_progress overrides in-memory values
    if file_progress:
        for key in ("status", "step", "pct", "running"):
            if key in file_progress:
                progress[key] = file_progress[key]
        # Convert string 'pct' to int if needed
        if "pct" in progress and not isinstance(progress["pct"], int):
            try:
                progress["pct"] = int(progress["pct"])
            except (ValueError, TypeError):
                pass

    return jsonify({
        "running": progress.get("status") == "running" or progress.get("running", False),
        "pct": progress.get("pct", 0),
        "step": progress.get("step", ""),
        "status": progress.get("status", ""),
        "tags_running": tag_progress.get("status") == "running",
        "tags_status": tag_progress.get("status", ""),
        "sync_days_running": sync_progress.get("status") == "running",
        "sync_days_status": sync_progress.get("status", ""),
        "sync_days_step": sync_progress.get("step", ""),
        "sync_days_pct": sync_progress.get("pct", 0),
        "sync_today_running": sync_today_progress.get("status") == "running",
        "sync_today_status": sync_today_progress.get("status", ""),
        "sync_today_step": sync_today_progress.get("step", ""),
        "sync_today_pct": sync_today_progress.get("pct", 0),
        "backfill_pending_running": backfill_pending_progress.get("status") == "running",
        "backfill_pending_status": backfill_pending_progress.get("status", ""),
        "backfill_pending_step": backfill_pending_progress.get("step", ""),
        "backfill_pending_pct": backfill_pending_progress.get("pct", 0),
        "pull_domain_running": pull_domain_progress.get("status") == "running",
        "pull_domain_status": pull_domain_progress.get("status", ""),
        "pull_domain_step": pull_domain_progress.get("step", ""),
        "pull_domain_pct": pull_domain_progress.get("pct", 0),
        "hosting_refresh_running": hosting_progress.get("status") == "running",
        "hosting_refresh_status": hosting_progress.get("status", ""),
        "hosting_refresh_step": hosting_progress.get("step", ""),
        "hosting_refresh_pct": hosting_progress.get("pct", 0),
    })


@app.route("/health")
def health():
    """Simple health check."""
    return jsonify({
        "status": "ok",
        "db_exists": db_exists(),
        "db_path": _get_user_db_path(),
        "current_user": g.get("current_user_id", "default"),
        "all_users": [u.id for u in g.get("all_users", [])],
    })


# ── Entry Point ────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    debug = os.environ.get("FLASK_DEBUG", "1") == "1"
    registry = UserRegistry()
    users = registry.list_users()
    print(f"🚀 Instantly Domain Tracker — Dashboard (multi-user)")
    print(f"   Users in registry: {len(users)}")
    for u in users:
        db_ok = "✓" if u.db_path.exists() else "✗"
        print(f"     {u.id:<20} {u.name:<25} DB: {db_ok}")
    print(f"   URL:      http://localhost:{port}")
    app.run(host="0.0.0.0", port=port, debug=debug)
