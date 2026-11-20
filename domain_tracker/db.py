"""SQLite database layer — schema init, connection management, UPSERT helpers.

Supports multi-user: each user gets their own SQLite database.
Call get_connection(user_slug) to get the connection for a specific user.
"""

import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .config import settings
from .users import UserRegistry

# ---------------------------------------------------------------------------
# Schema DDL
# ---------------------------------------------------------------------------

SCHEMA_SQL = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS accounts (
    email           TEXT PRIMARY KEY,
    domain          TEXT NOT NULL,
    status          INTEGER,
    warmup_status   INTEGER,
    tracking_domain TEXT,
    provider_code   INTEGER,
    provider        TEXT DEFAULT 'Other',
    first_name      TEXT,
    last_name       TEXT,
    daily_limit     INTEGER,
    hosted_ip       TEXT,
    hosted_country  TEXT,
    first_seen_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_accounts_domain ON accounts(domain);
CREATE INDEX IF NOT EXISTS idx_accounts_status ON accounts(status);

CREATE TABLE IF NOT EXISTS daily_metrics (
    date                        TEXT NOT NULL,
    email_account               TEXT NOT NULL,
    sent                        INTEGER DEFAULT 0,
    bounced                     INTEGER DEFAULT 0,
    contacted                   INTEGER DEFAULT 0,
    new_leads_contacted         INTEGER DEFAULT 0,
    opened                      INTEGER DEFAULT 0,
    unique_opened               INTEGER DEFAULT 0,
    replies                     INTEGER DEFAULT 0,
    unique_replies              INTEGER DEFAULT 0,
    replies_automatic           INTEGER DEFAULT 0,
    unique_replies_automatic    INTEGER DEFAULT 0,
    clicks                      INTEGER DEFAULT 0,
    unique_clicks               INTEGER DEFAULT 0,
    fetched_at                  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (date, email_account),
    FOREIGN KEY (email_account) REFERENCES accounts(email)
);

CREATE INDEX IF NOT EXISTS idx_dm_date ON daily_metrics(date);
CREATE INDEX IF NOT EXISTS idx_dm_account ON daily_metrics(email_account);
CREATE INDEX IF NOT EXISTS idx_dm_date_account ON daily_metrics(date, email_account);

CREATE TABLE IF NOT EXISTS account_campaigns (
    email_account   TEXT NOT NULL,
    campaign_id     TEXT NOT NULL,
    campaign_name   TEXT,
    campaign_status INTEGER,
    mapped_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (email_account, campaign_id),
    FOREIGN KEY (email_account) REFERENCES accounts(email)
);

CREATE INDEX IF NOT EXISTS idx_ac_campaign ON account_campaigns(campaign_id);

CREATE TABLE IF NOT EXISTS email_tags (
    email_account   TEXT NOT NULL,
    tag             TEXT NOT NULL COLLATE NOCASE,
    synced_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (email_account, tag),
    FOREIGN KEY (email_account) REFERENCES accounts(email)
);

CREATE INDEX IF NOT EXISTS idx_email_tags_tag ON email_tags(tag);
CREATE INDEX IF NOT EXISTS idx_email_tags_account ON email_tags(email_account);

CREATE TABLE IF NOT EXISTS campaign_account_metrics (
    campaign_id     TEXT NOT NULL,
    account_email   TEXT NOT NULL,
    total_sent      INTEGER DEFAULT 0,
    replies         INTEGER DEFAULT 0,
    subject_line    TEXT,
    i_status        INTEGER,
    classification  TEXT DEFAULT NULL,
    last_activity_at TEXT,
    last_synced_at  TEXT,
    PRIMARY KEY (campaign_id, account_email)
);

CREATE TABLE IF NOT EXISTS campaign_analytics (
    campaign_id              TEXT PRIMARY KEY,
    campaign_name            TEXT,
    campaign_status          INTEGER,
    leads_count              INTEGER DEFAULT 0,
    contacted_count          INTEGER DEFAULT 0,
    emails_sent_count        INTEGER DEFAULT 0,
    reply_count              INTEGER DEFAULT 0,
    reply_count_unique       INTEGER DEFAULT 0,
    reply_count_automatic    INTEGER DEFAULT 0,
    reply_count_automatic_unique INTEGER DEFAULT 0,
    bounced_count            INTEGER DEFAULT 0,
    unsubscribed_count       INTEGER DEFAULT 0,
    completed_count          INTEGER DEFAULT 0,
    total_opportunities      INTEGER DEFAULT 0,
    total_opportunity_value  INTEGER DEFAULT 0,
    last_synced_at           TEXT
);

CREATE TABLE IF NOT EXISTS domain_notes (
    domain          TEXT PRIMARY KEY,
    note            TEXT,
    updated_at      TEXT
);
"""

# ---------------------------------------------------------------------------
# Connection helpers
# ---------------------------------------------------------------------------

_connections: dict[str, sqlite3.Connection] = {}


# ---------------------------------------------------------------------------
# Migration: move old single-user DB to multi-user layout
# ---------------------------------------------------------------------------


def _migrate_old_db() -> None:
    """Move data/instantly_tracker.db → data/users/default/tracker.db.

    Only runs once: if the old DB exists and the default user dir does not.
    """
    project_root = Path(__file__).resolve().parent.parent
    old_db = project_root / "data" / "instantly_tracker.db"
    new_dir = project_root / "data" / "users" / "default"
    new_db = new_dir / "tracker.db"

    if old_db.exists() and not new_db.exists():
        new_dir.mkdir(parents=True, exist_ok=True)
        shutil.move(str(old_db), str(new_db))
        # Also move WAL/SHM files if they exist
        for ext in (".db-wal", ".db-shm"):
            old_extra = old_db.with_suffix(ext)
            if old_extra.exists():
                new_extra = new_db.with_suffix(ext)
                shutil.move(str(old_extra), str(new_extra))


# Run migration on import
_migrate_old_db()


# ---------------------------------------------------------------------------
# Connection helpers
# ---------------------------------------------------------------------------


def _get_db_path(user_slug: str = "default") -> Path:
    """Resolve the database path for a given user slug."""
    project_root = Path(__file__).resolve().parent.parent
    user_dir = project_root / "data" / "users" / user_slug
    db_path = user_dir / "tracker.db"
    # If user_slug is "default" and no users/default/tracker.db, try old location
    if user_slug == "default" and not db_path.exists():
        old = project_root / "data" / "instantly_tracker.db"
        if old.exists():
            return old
    return db_path


def get_connection(user_slug: str = "default") -> sqlite3.Connection:
    """Return a SQLite connection for the given user (WAL mode, row factory).

    Connections are cached per path. Default user_slug="default" maintains
    backward compatibility.
    """
    # When settings.DATABASE_PATH is explicitly set (non-empty) and user is
    # the default user, use it instead of auto-resolving via _get_db_path.
    # This allows the test fixture (temp_db) to redirect connections to a
    # temporary database by overriding settings.DATABASE_PATH.
    if user_slug == "default" and settings.DATABASE_PATH:
        db_path = Path(settings.DATABASE_PATH).resolve()
    else:
        db_path = _get_db_path(user_slug)
    cache_key = str(db_path.resolve())

    if cache_key not in _connections:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(db_path), timeout=10, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        _connections[cache_key] = conn
    return _connections[cache_key]


def close_connection(user_slug: Optional[str] = None) -> None:
    """Close connection(s).

    Args:
        user_slug: If given, close only that user's connection.
                   If None, close all cached connections.
    """
    global _connections
    if user_slug is not None:
        db_path = _get_db_path(user_slug)
        cache_key = str(db_path.resolve())
        conn = _connections.pop(cache_key, None)
        if conn is not None:
            conn.close()
    else:
        for conn in _connections.values():
            conn.close()
        _connections = {}


def init_db(user_slug: str = "default") -> None:
    """Create schema if it doesn't exist for a given user."""
    conn = get_connection(user_slug)
    conn.executescript(SCHEMA_SQL)
    conn.commit()
    _migrate_schema(conn)


def _migrate_schema(conn: sqlite3.Connection) -> None:
    """Apply any schema migrations for existing databases."""
    # Migration 1: Add provider column (added in v0.5.0)
    try:
        conn.execute("SELECT provider FROM accounts LIMIT 0")
    except sqlite3.OperationalError:
        conn.execute("ALTER TABLE accounts ADD COLUMN provider TEXT")
        conn.commit()

    # Migration 2: Add email_tags table (added in v0.7.0)
    try:
        conn.execute("SELECT COUNT(*) FROM email_tags LIMIT 0")
    except sqlite3.OperationalError:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS email_tags (
                email_account   TEXT NOT NULL,
                tag             TEXT NOT NULL COLLATE NOCASE,
                synced_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (email_account, tag),
                FOREIGN KEY (email_account) REFERENCES accounts(email)
            );
            CREATE INDEX IF NOT EXISTS idx_email_tags_tag ON email_tags(tag);
            CREATE INDEX IF NOT EXISTS idx_email_tags_account ON email_tags(email_account);
        """)
        conn.commit()

    # Migration 3: Add synced_at column if missing (added in v0.7.1)
    try:
        conn.execute("SELECT synced_at FROM email_tags LIMIT 0")
    except sqlite3.OperationalError:
        try:
            conn.execute("ALTER TABLE email_tags ADD COLUMN synced_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP")
            conn.commit()
        except sqlite3.OperationalError:
            pass

    # Migration 4: Add campaign_account_metrics table
    try:
        conn.execute("SELECT COUNT(*) FROM campaign_account_metrics LIMIT 0")
    except sqlite3.OperationalError:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS campaign_account_metrics (
                campaign_id     TEXT NOT NULL,
                account_email   TEXT NOT NULL,
                total_sent      INTEGER DEFAULT 0,
                replies         INTEGER DEFAULT 0,
                last_activity_at TEXT,
                last_synced_at  TEXT,
                PRIMARY KEY (campaign_id, account_email)
            );
        """)
        conn.commit()

    # Migration 5: Add campaign_analytics table
    try:
        conn.execute("SELECT COUNT(*) FROM campaign_analytics LIMIT 0")
    except sqlite3.OperationalError:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS campaign_analytics (
                campaign_id              TEXT PRIMARY KEY,
                campaign_name            TEXT,
                campaign_status          INTEGER,
                leads_count              INTEGER DEFAULT 0,
                contacted_count          INTEGER DEFAULT 0,
                emails_sent_count        INTEGER DEFAULT 0,
                reply_count              INTEGER DEFAULT 0,
                reply_count_unique       INTEGER DEFAULT 0,
                reply_count_automatic    INTEGER DEFAULT 0,
                reply_count_automatic_unique INTEGER DEFAULT 0,
                bounced_count            INTEGER DEFAULT 0,
                unsubscribed_count       INTEGER DEFAULT 0,
                completed_count          INTEGER DEFAULT 0,
                total_opportunities      INTEGER DEFAULT 0,
                total_opportunity_value  INTEGER DEFAULT 0,
                last_synced_at           TEXT
            );
        """)
        conn.commit()

    # Migration 6: Add hosted_ip and hosted_country to accounts
    try:
        conn.execute("SELECT hosted_ip FROM accounts LIMIT 0")
    except sqlite3.OperationalError:
        try:
            conn.execute("ALTER TABLE accounts ADD COLUMN hosted_ip TEXT")
            conn.execute("ALTER TABLE accounts ADD COLUMN hosted_country TEXT")
            conn.commit()
        except sqlite3.OperationalError:
            pass

    # Migration 7: Add subject_line, i_status, classification to campaign_account_metrics
    try:
        conn.execute("SELECT subject_line FROM campaign_account_metrics LIMIT 0")
    except sqlite3.OperationalError:
        try:
            conn.execute("ALTER TABLE campaign_account_metrics ADD COLUMN subject_line TEXT")
            conn.execute("ALTER TABLE campaign_account_metrics ADD COLUMN i_status INTEGER")
            conn.execute("ALTER TABLE campaign_account_metrics ADD COLUMN classification TEXT")
            conn.commit()
        except sqlite3.OperationalError:
            pass

    # Migration 8: Add domain_notes table
    try:
        conn.execute("SELECT COUNT(*) FROM domain_notes LIMIT 0")
    except sqlite3.OperationalError:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS domain_notes (
                domain          TEXT PRIMARY KEY,
                note            TEXT,
                updated_at      TEXT
            );
        """)
        conn.commit()


# ---------------------------------------------------------------------------
# Helpers — domain extraction
# ---------------------------------------------------------------------------

def extract_domain(email: str) -> str:
    """Extract domain from email (e.g. user@Example.COM → example.com)."""
    try:
        return email.split("@")[1].lower()
    except (IndexError, AttributeError):
        return "unknown"


# ---------------------------------------------------------------------------
# UPSERT helpers
# ---------------------------------------------------------------------------

def now_ts() -> str:
    return datetime.now(timezone.utc).isoformat()


def upsert_account(
    conn: sqlite3.Connection,
    email: str,
    domain: str,
    status: Optional[int] = None,
    warmup_status: Optional[int] = None,
    tracking_domain: Optional[str] = None,
    provider_code: Optional[int] = None,
    provider: Optional[str] = None,
    first_name: Optional[str] = None,
    last_name: Optional[str] = None,
    daily_limit: Optional[int] = None,
    hosted_ip: Optional[str] = None,
    hosted_country: Optional[str] = None,
) -> None:
    """Upsert a single account row."""
    conn.execute(
        """
        INSERT INTO accounts
            (email, domain, status, warmup_status, tracking_domain,
             provider_code, provider, first_name, last_name, daily_limit,
             hosted_ip, hosted_country,
             last_updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(email) DO UPDATE SET
            domain            = excluded.domain,
            status            = COALESCE(excluded.status, accounts.status),
            warmup_status     = COALESCE(excluded.warmup_status, accounts.warmup_status),
            tracking_domain   = COALESCE(excluded.tracking_domain, accounts.tracking_domain),
            provider_code     = COALESCE(excluded.provider_code, accounts.provider_code),
            provider          = COALESCE(excluded.provider, accounts.provider),
            first_name        = COALESCE(excluded.first_name, accounts.first_name),
            last_name         = COALESCE(excluded.last_name, accounts.last_name),
            daily_limit       = COALESCE(excluded.daily_limit, accounts.daily_limit),
            hosted_ip         = COALESCE(excluded.hosted_ip, accounts.hosted_ip),
            hosted_country    = COALESCE(excluded.hosted_country, accounts.hosted_country),
            last_updated_at   = excluded.last_updated_at
        """,
        (
            email,
            domain,
            status,
            warmup_status,
            tracking_domain,
            provider_code,
            provider,
            first_name,
            last_name,
            daily_limit,
            hosted_ip,
            hosted_country,
            now_ts(),
        ),
    )


def ensure_account_exists(conn: sqlite3.Connection, email: str) -> None:
    """Create a stub account row if it doesn't exist (avoids FK violations)."""
    domain = extract_domain(email)
    conn.execute(
        """
        INSERT OR IGNORE INTO accounts (email, domain)
        VALUES (?, ?)
        """,
        (email, domain),
    )


def upsert_daily_metrics(
    conn: sqlite3.Connection,
    date: str,
    email_account: str,
    sent: int = 0,
    bounced: int = 0,
    contacted: int = 0,
    new_leads_contacted: int = 0,
    opened: int = 0,
    unique_opened: int = 0,
    replies: int = 0,
    unique_replies: int = 0,
    replies_automatic: int = 0,
    unique_replies_automatic: int = 0,
    clicks: int = 0,
    unique_clicks: int = 0,
) -> None:
    """Upsert a single daily_metrics row."""
    conn.execute(
        """
        INSERT INTO daily_metrics
            (date, email_account, sent, bounced, contacted,
             new_leads_contacted, opened, unique_opened,
             replies, unique_replies, replies_automatic, unique_replies_automatic,
             clicks, unique_clicks, fetched_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(date, email_account) DO UPDATE SET
            sent                        = excluded.sent,
            bounced                     = excluded.bounced,
            contacted                   = excluded.contacted,
            new_leads_contacted         = excluded.new_leads_contacted,
            opened                      = excluded.opened,
            unique_opened               = excluded.unique_opened,
            replies                     = excluded.replies,
            unique_replies              = excluded.unique_replies,
            replies_automatic           = excluded.replies_automatic,
            unique_replies_automatic    = excluded.unique_replies_automatic,
            clicks                      = excluded.clicks,
            unique_clicks               = excluded.unique_clicks,
            fetched_at                  = excluded.fetched_at
        """,
        (
            date,
            email_account,
            sent,
            bounced,
            contacted,
            new_leads_contacted,
            opened,
            unique_opened,
            replies,
            unique_replies,
            replies_automatic,
            unique_replies_automatic,
            clicks,
            unique_clicks,
            now_ts(),
        ),
    )


def upsert_email_tags(
    conn: sqlite3.Connection,
    email_account: str,
    tags: list[str],
) -> None:
    """Replace all tags for an account.

    Deletes existing tags first, then inserts the new ones.
    Handles comma-separated splitting and empty/None gracefully.
    Uses executemany for bulk insert efficiency.
    """
    conn.execute(
        "DELETE FROM email_tags WHERE email_account = ?",
        (email_account,),
    )
    # Filter out empty / whitespace-only tags before bulk insert
    clean_tags = [t.strip() for t in tags if t and t.strip()]
    if clean_tags:
        conn.executemany(
            """INSERT OR IGNORE INTO email_tags
               (email_account, tag)
               VALUES (?, ?)""",
            [(email_account, tag) for tag in clean_tags],
        )


def delete_email_tags_for_account(
    conn: sqlite3.Connection,
    email_account: str,
) -> None:
    """Delete all tags for a specific account."""
    conn.execute(
        "DELETE FROM email_tags WHERE email_account = ?",
        (email_account,),
    )


def upsert_domain_note(
    conn: sqlite3.Connection,
    domain: str,
    note: str,
) -> None:
    """Upsert the free-text note for a domain. Empty note clears it."""
    conn.execute(
        """
        INSERT INTO domain_notes (domain, note, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(domain) DO UPDATE SET
            note       = excluded.note,
            updated_at = excluded.updated_at
        """,
        (domain, note, now_ts()),
    )
    conn.commit()


def upsert_account_campaign(
    conn: sqlite3.Connection,
    email_account: str,
    campaign_id: str,
    campaign_name: Optional[str] = None,
    campaign_status: Optional[int] = None,
) -> None:
    """Upsert a single account-campaign mapping."""
    conn.execute(
        """
        INSERT INTO account_campaigns
            (email_account, campaign_id, campaign_name, campaign_status, mapped_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(email_account, campaign_id) DO UPDATE SET
            campaign_name   = COALESCE(excluded.campaign_name, account_campaigns.campaign_name),
            campaign_status = COALESCE(excluded.campaign_status, account_campaigns.campaign_status),
            mapped_at       = excluded.mapped_at
        """,
        (email_account, campaign_id, campaign_name, campaign_status, now_ts()),
    )


# ---------------------------------------------------------------------------
# Campaign metrics UPSERT helpers
# ---------------------------------------------------------------------------


def upsert_campaign_account_metrics(
    conn: sqlite3.Connection,
    campaign_id: str,
    account_email: str,
    total_sent: int,
    replies: int,
    last_activity_at: Optional[str] = None,
    subject_line: Optional[str] = None,
    i_status: Optional[int] = None,
    classification: Optional[str] = None,
) -> None:
    """Upsert per-account metrics for a campaign.

    total_sent is cumulative (added on conflict).
    replies is replaced on conflict.
    subject_line, i_status, classification are only updated when the
    incoming value is not None (to preserve the most recent received email's data).
    """
    conn.execute(
        """
        INSERT INTO campaign_account_metrics
            (campaign_id, account_email, total_sent, replies,
             subject_line, i_status, classification,
             last_activity_at, last_synced_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(campaign_id, account_email) DO UPDATE SET
            total_sent      = campaign_account_metrics.total_sent + excluded.total_sent,
            replies         = excluded.replies,
            subject_line    = CASE WHEN excluded.subject_line IS NOT NULL THEN excluded.subject_line ELSE campaign_account_metrics.subject_line END,
            i_status        = CASE WHEN excluded.i_status IS NOT NULL THEN excluded.i_status ELSE campaign_account_metrics.i_status END,
            classification  = CASE WHEN excluded.classification IS NOT NULL THEN excluded.classification ELSE campaign_account_metrics.classification END,
            last_activity_at = COALESCE(excluded.last_activity_at, campaign_account_metrics.last_activity_at),
            last_synced_at  = excluded.last_synced_at
        """,
        (campaign_id, account_email, total_sent, replies,
         subject_line, i_status, classification,
         last_activity_at, now_ts()),
    )


def upsert_campaign_analytics(
    conn: sqlite3.Connection,
    campaign_id: str,
    data_dict: dict,
) -> None:
    """INSERT OR REPLACE campaign analytics from API response dict.

    Maps API fields to DB columns. Unknown fields are ignored.
    """
    camp_name = data_dict.get("campaign_name", data_dict.get("name", ""))
    status = data_dict.get("status", data_dict.get("campaign_status"))

    conn.execute(
        """
        INSERT OR REPLACE INTO campaign_analytics
            (campaign_id, campaign_name, campaign_status,
             leads_count, contacted_count,
             emails_sent_count, reply_count, reply_count_unique,
             reply_count_automatic, reply_count_automatic_unique,
             bounced_count, unsubscribed_count, completed_count,
             total_opportunities, total_opportunity_value,
             last_synced_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            campaign_id,
            camp_name,
            status,
            data_dict.get("leads_count", 0) or 0,
            data_dict.get("contacted_count", 0) or 0,
            data_dict.get("emails_sent_count", 0) or 0,
            data_dict.get("reply_count", 0) or 0,
            data_dict.get("reply_count_unique", 0) or 0,
            data_dict.get("reply_count_automatic", 0) or 0,
            data_dict.get("reply_count_automatic_unique", 0) or 0,
            data_dict.get("bounced_count", 0) or 0,
            data_dict.get("unsubscribed_count", 0) or 0,
            data_dict.get("completed_count", 0) or 0,
            data_dict.get("total_opportunities", 0) or 0,
            data_dict.get("total_opportunity_value", 0) or 0,
            now_ts(),
        ),
    )
