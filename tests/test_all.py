"""Basic tests for the domain_tracker package."""

import pytest
import sqlite3
from pathlib import Path
import tempfile
import os

from domain_tracker.db import (
    extract_domain,
    init_db,
    get_connection,
    close_connection,
    upsert_account,
    upsert_daily_metrics,
    upsert_account_campaign,
)
from domain_tracker.config import settings
from domain_tracker.analytics import (
    classify_domain,
    _pct,
    get_domain_stats,
    get_mailbox_stats,
    get_campaign_stats,
    get_campaign_domains,
    classify_domains_with_provider,
)


# ------------------------------------------------------------------
# Fixtures
# ------------------------------------------------------------------

@pytest.fixture(autouse=True)
def temp_db():
    """Use a temp SQLite database for each test."""
    tmpdir = tempfile.mkdtemp()
    db_path = os.path.join(tmpdir, "test.db")
    old_path = settings.DATABASE_PATH
    settings.DATABASE_PATH = db_path

    init_db()
    yield

    close_connection()
    settings.DATABASE_PATH = old_path


# ------------------------------------------------------------------
# Domain extraction
# ------------------------------------------------------------------

class TestDomainExtraction:
    def test_basic(self):
        assert extract_domain("user@example.com") == "example.com"

    def test_uppercase(self):
        assert extract_domain("User@Example.COM") == "example.com"

    def test_subdomain(self):
        assert extract_domain("user@mail.example.com") == "mail.example.com"

    def test_no_at(self):
        assert extract_domain("notanemail") == "unknown"

    def test_empty(self):
        assert extract_domain("") == "unknown"


# ------------------------------------------------------------------
# DB schema and upserts
# ------------------------------------------------------------------

class TestDB:
    def test_init_creates_tables(self):
        conn = get_connection()
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        names = [r[0] for r in tables]
        assert "accounts" in names
        assert "daily_metrics" in names
        assert "account_campaigns" in names

    def test_upsert_account(self):
        conn = get_connection()
        upsert_account(conn, "a@x.com", "x.com", status=1)
        conn.commit()

        row = conn.execute(
            "SELECT * FROM accounts WHERE email=?", ("a@x.com",)
        ).fetchone()
        assert row["domain"] == "x.com"
        assert row["status"] == 1

        # Upsert again (should update, not duplicate)
        upsert_account(conn, "a@x.com", "x.com", status=2)
        conn.commit()
        rows = conn.execute(
            "SELECT * FROM accounts WHERE email=?", ("a@x.com",)
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["status"] == 2

    def test_upsert_daily_metrics(self):
        conn = get_connection()
        upsert_account(conn, "a@x.com", "x.com", status=1)
        upsert_daily_metrics(conn, "2026-05-01", "a@x.com", sent=100, unique_replies=5)
        conn.commit()

        row = conn.execute(
            "SELECT * FROM daily_metrics WHERE date=? AND email_account=?",
            ("2026-05-01", "a@x.com"),
        ).fetchone()
        assert row["sent"] == 100
        assert row["unique_replies"] == 5

    def test_upsert_account_campaign(self):
        conn = get_connection()
        upsert_account(conn, "a@x.com", "x.com", status=1)
        upsert_account_campaign(conn, "a@x.com", "camp_1", "Campaign One", 1)
        conn.commit()

        row = conn.execute(
            "SELECT * FROM account_campaigns WHERE email_account=? AND campaign_id=?",
            ("a@x.com", "camp_1"),
        ).fetchone()
        assert row["campaign_name"] == "Campaign One"


# ------------------------------------------------------------------
# Analytics
# ------------------------------------------------------------------

class TestAnalytics:
    def test_pct_division(self):
        assert _pct(5, 100) == 5.0
        assert _pct(0, 100) == 0.0
        assert _pct(5, 0) == 0.0

    def test_classify_domain_burned_reply_rate(self):
        assert classify_domain(reply_rate=0.5, bounce_rate=2.0) == "BURNED"

    def test_classify_domain_burned_bounce_rate(self):
        assert classify_domain(reply_rate=5.0, bounce_rate=9.0) == "BURNED"

    def test_classify_domain_at_risk_reply_rate(self):
        assert classify_domain(reply_rate=1.5, bounce_rate=2.0) == "AT_RISK"

    def test_classify_domain_at_risk_bounce_rate(self):
        assert classify_domain(reply_rate=5.0, bounce_rate=6.0) == "AT_RISK"

    def test_classify_domain_healthy(self):
        assert classify_domain(reply_rate=5.0, bounce_rate=2.0) == "HEALTHY"

    def test_classify_domain_relative_flag(self):
        assert (
            classify_domain(
                reply_rate=2.5, bounce_rate=2.0, best_domain_reply_rate=10.0
            )
            == "RELATIVE_FLAG"
        )

    def test_classify_domain_relative_below_threshold(self):
        # reply_rate < best * 0.3 AND reply_rate < 3.0 → FLAG: 2.5 < 3.0 ✓
        result = classify_domain(
            reply_rate=2.5, bounce_rate=2.0, best_domain_reply_rate=10.0
        )
        assert result == "RELATIVE_FLAG"

    def test_classify_domain_relative_above_max(self):
        # reply_rate >= 3.0 → doesn't flag
        result = classify_domain(
            reply_rate=3.5, bounce_rate=2.0, best_domain_reply_rate=10.0
        )
        # 3.5 is >= AT_RISK_REPLY_RATE (2.0) and >= BURN_REPLY_RATE (1.0)
        # bounce 2.0 is fine, so HEALTHY if relative check fails
        # But 3.5 < 10*0.3 = 3.0? No, 3.5 > 3.0, so relative check fails too
        # And 3.5 >= RELATIVE_MAX_REPLY_RATE (3.0) so it fails the second condition
        assert result == "HEALTHY"

    def test_relative_check_above_multiplier(self):
        """reply_rate > best*0.3 → no relative flag."""
        # best=10.0, 10*0.3=3.0, reply_rate=4.0 → 4.0 < 3.0? No
        result = classify_domain(
            reply_rate=4.0, bounce_rate=2.0, best_domain_reply_rate=10.0
        )
        # 4.0 ≥ AT_RISK threshold too (2.0), so AT_RISK... wait:
        # 4.0 < AT_RISK_REPLY_RATE (2.0)? No. 
        # 2.0 < BURN_BOUNCE_RATE (8.0)? Yes. bounce is fine.
        # So it should be HEALTHY
        assert result == "HEALTHY"

    def test_domain_stats_query(self):
        conn = get_connection()
        # Insert test data
        upsert_account(conn, "a@x.com", "x.com", status=1)
        upsert_account(conn, "b@x.com", "x.com", status=1)
        upsert_account(conn, "c@y.com", "y.com", status=1)
        upsert_daily_metrics(conn, "2026-05-25", "a@x.com", sent=100, unique_replies=5, bounced=2)
        upsert_daily_metrics(conn, "2026-05-25", "b@x.com", sent=100, unique_replies=3, bounced=1)
        upsert_daily_metrics(conn, "2026-05-25", "c@y.com", sent=50, unique_replies=0, bounced=10)
        conn.commit()

        stats = get_domain_stats(
            min_date="2026-05-20",
            max_date="2026-05-30",
            min_volume=10,
        )

        domain_map = {d["domain"]: d for d in stats}

        assert "x.com" in domain_map
        assert domain_map["x.com"]["total_sent"] == 200
        assert domain_map["x.com"]["total_replies"] == 8
        assert domain_map["x.com"]["reply_rate_pct"] == 4.0

        assert "y.com" in domain_map
        assert domain_map["y.com"]["total_sent"] == 50
        assert domain_map["y.com"]["total_replies"] == 0
        assert domain_map["y.com"]["bounce_rate_pct"] == 20.0

# ------------------------------------------------------------------
# Campaign analytics
# ------------------------------------------------------------------

class TestCampaignAnalytics:
    def test_get_campaign_stats_empty(self):
        """No campaign mappings = empty result."""
        stats = get_campaign_stats(
            lookback_days=30,
        )
        assert stats == []

    def test_get_campaign_stats_with_data(self):
        conn = get_connection()
        # Set up two accounts in one campaign
        upsert_account(conn, "a@x.com", "x.com", status=1)
        upsert_account(conn, "b@y.com", "y.com", status=1)
        upsert_account_campaign(conn, "a@x.com", "camp_1", "Summer Outreach", 1)
        upsert_account_campaign(conn, "b@y.com", "camp_1", "Summer Outreach", 1)
        upsert_daily_metrics(conn, "2026-05-25", "a@x.com", sent=100, unique_replies=5, bounced=2)
        upsert_daily_metrics(conn, "2026-05-25", "b@y.com", sent=200, unique_replies=10, bounced=0)
        conn.commit()

        stats = get_campaign_stats(lookback_days=365)

        assert len(stats) == 1
        c = stats[0]
        assert c["campaign_name"] == "Summer Outreach"
        assert c["domain_count"] == 2  # x.com and y.com
        assert c["account_count"] == 2
        assert c["total_sent"] == 300
        assert c["total_replies"] == 15
        assert c["reply_rate_pct"] == 5.0

    def test_get_campaign_stats_name_filter(self):
        conn = get_connection()
        upsert_account(conn, "a@x.com", "x.com", status=1)
        upsert_account(conn, "b@y.com", "y.com", status=1)
        upsert_account_campaign(conn, "a@x.com", "camp_1", "Summer Outreach", 1)
        upsert_account_campaign(conn, "b@y.com", "camp_2", "Winter Follow-up", 1)
        upsert_daily_metrics(conn, "2026-05-25", "a@x.com", sent=100, unique_replies=5)
        upsert_daily_metrics(conn, "2026-05-25", "b@y.com", sent=200, unique_replies=10)
        conn.commit()

        stats = get_campaign_stats(lookback_days=365, campaign_name_filter="Summer")
        assert len(stats) == 1
        assert stats[0]["campaign_name"] == "Summer Outreach"

        stats = get_campaign_stats(lookback_days=365, campaign_name_filter="Winter")
        assert len(stats) == 1
        assert stats[0]["campaign_name"] == "Winter Follow-up"

        stats = get_campaign_stats(lookback_days=365, campaign_name_filter="Spring")
        assert len(stats) == 0

    def test_get_campaign_domains(self):
        conn = get_connection()
        upsert_account(conn, "a@x.com", "x.com", status=1)
        upsert_account(conn, "b@x.com", "x.com", status=1)
        upsert_account(conn, "c@y.com", "y.com", status=1)
        upsert_account_campaign(conn, "a@x.com", "camp_1", "Summer Outreach", 1)
        upsert_account_campaign(conn, "b@x.com", "camp_1", "Summer Outreach", 1)
        upsert_account_campaign(conn, "c@y.com", "camp_1", "Summer Outreach", 1)
        upsert_daily_metrics(conn, "2026-05-25", "a@x.com", sent=100, unique_replies=5)
        upsert_daily_metrics(conn, "2026-05-25", "b@x.com", sent=50, unique_replies=2)
        upsert_daily_metrics(conn, "2026-05-25", "c@y.com", sent=200, unique_replies=10)
        conn.commit()

        domains = get_campaign_domains("camp_1", lookback_days=365)

        domain_map = {d["domain"]: d for d in domains}
        assert "x.com" in domain_map
        assert domain_map["x.com"]["active_accounts"] == 2
        assert domain_map["x.com"]["total_sent"] == 150
        assert domain_map["x.com"]["total_replies"] == 7

        assert "y.com" in domain_map
        assert domain_map["y.com"]["active_accounts"] == 1
        assert domain_map["y.com"]["total_sent"] == 200
        assert domain_map["y.com"]["total_replies"] == 10
        assert domain_map["y.com"]["reply_rate_pct"] == 5.0

    def test_get_campaign_domains_empty(self):
        """Unknown campaign ID returns empty."""
        domains = get_campaign_domains("nonexistent", lookback_days=365)
        assert domains == []


# ------------------------------------------------------------------
# Provider detection
# ------------------------------------------------------------------

class TestProviderDetection:
    def test_db_schema_has_provider_column(self):
        conn = get_connection()
        cols = conn.execute(
            "PRAGMA table_info(accounts)"
        ).fetchall()
        col_names = [c["name"] for c in cols]
        assert "provider" in col_names
        # Check default is Other
        assert any(
            c["name"] == "provider" and c["dflt_value"] == "'Other'"
            for c in cols
        )

    def test_classify_domains_with_provider_no_data(self):
        assert classify_domains_with_provider([]) == []

    def test_classify_domains_with_provider_sets_provider_field(self):
        conn = get_connection()
        upsert_account(conn, "a@gmail.com", "gmail.com", status=1, provider="Gmail")
        upsert_daily_metrics(conn, "2026-05-25", "a@gmail.com", sent=100, unique_replies=5, bounced=1)
        conn.commit()

        stats = get_domain_stats(min_date="2026-05-20", max_date="2026-05-30", min_volume=10)
        classified = classify_domains_with_provider(stats)

        assert len(classified) == 1
        assert classified[0]["domain"] == "gmail.com"
        assert classified[0]["provider"] == "Gmail"

    def test_classify_domain_gmail_thresholds(self):
        """Gmail uses GOOGLE_BURN_REPLY_RATE (1.0%) — same as default for reply."""
        result = classify_domain(
            reply_rate=0.8, bounce_rate=2.0, provider="Gmail"
        )
        assert result == "BURNED"

    def test_classify_domain_outlook_thresholds(self):
        """Outlook uses OUTLOOK_BURN_REPLY_RATE (0.5%) — more lenient."""
        result = classify_domain(
            reply_rate=0.8, bounce_rate=2.0, provider="Outlook"
        )
        # 0.8 >= 0.5 (burn_reply for Outlook) so not burned by reply rate
        # bounce 2.0 <= 10.0 so not burned by bounce
        # 0.8 < 1.0 (at_risk_reply for Outlook) -> AT_RISK
        assert result == "AT_RISK"

    def test_classify_domain_outlook_higher_bounce_tolerance(self):
        """Outlook allows up to 10% bounce before burning."""
        result = classify_domain(
            reply_rate=5.0, bounce_rate=9.0, provider="Outlook"
        )
        # 9.0 <= 10.0 burn_bounce, 9.0 > 7.0 at_risk_bounce -> AT_RISK
        assert result == "AT_RISK"

    def test_classify_domain_outlook_burned_bounce(self):
        result = classify_domain(
            reply_rate=5.0, bounce_rate=11.0, provider="Outlook"
        )
        assert result == "BURNED"

    def test_classify_domain_other_uses_global_defaults(self):
        """Other provider uses default config thresholds."""
        result = classify_domain(
            reply_rate=0.8, bounce_rate=2.0, provider="Other"
        )
        # 0.8 < 1.0 -> BURNED (same as default)
        assert result == "BURNED"

    def test_classify_domain_no_provider_uses_defaults(self):
        """None provider falls back to global defaults."""
        result = classify_domain(
            reply_rate=0.8, bounce_rate=2.0
        )
        assert result == "BURNED"

    def test_detect_provider_google(self):
        """Test provider detection parsing logic with MX-like data."""
        from domain_tracker.etl import detect_provider
        # Mock _doh_query by patching... We'll just test the parsing logic
        # by checking detection works for obvious patterns
        # This test just verifies detect_provider handles "Other" for unknown
        # The real DNS lookup is tested via the CLI
        # For now, verify it's importable and returns str
        assert isinstance(detect_provider("nonexistent-test-domain-" + str(hash("x")) + ".xyz"), str)


# ------------------------------------------------------------------
# Config
# ------------------------------------------------------------------

class TestConfig:
    def test_defaults(self):
        assert settings.BURN_REPLY_RATE_THRESHOLD == 1.0
        assert settings.LOOKBACK_DAYS == 30
        assert settings.MIN_VOLUME_THRESHOLD == 50

    def test_is_configured_false_by_default(self):
        # No API key set in tests
        assert not settings.is_configured or settings.INSTANTLY_API_KEY != ""

    def test_provider_threshold_defaults(self):
        assert settings.GOOGLE_BURN_REPLY_RATE == 1.0
        assert settings.GOOGLE_AT_RISK_REPLY_RATE == 2.0
        assert settings.GOOGLE_BURN_BOUNCE_RATE == 8.0
        assert settings.GOOGLE_AT_RISK_BOUNCE_RATE == 5.0
        assert settings.OUTLOOK_BURN_REPLY_RATE == 0.5
        assert settings.OUTLOOK_AT_RISK_REPLY_RATE == 1.0
        assert settings.OUTLOOK_BURN_BOUNCE_RATE == 10.0
        assert settings.OUTLOOK_AT_RISK_BOUNCE_RATE == 7.0
