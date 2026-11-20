"""Instantly REST API V2 client — typed wrappers for the key endpoints."""

import time
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin

import requests

from .config import settings


class InstantlyAPIError(Exception):
    """Raised on non-2xx API responses."""


class InstantlyClient:
    """Thin wrapper around Instantly REST API V2."""

    def __init__(self, api_key: Optional[str] = None):
        self.base_url = settings.INSTANTLY_BASE_URL
        self.api_key = api_key or settings.INSTANTLY_API_KEY
        self._session = requests.Session()
        self._session.headers.update(
            {
                "Authorization": f"Bearer {self.api_key}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            }
        )

    # ------------------------------------------------------------------
    # Request helpers
    # ------------------------------------------------------------------

    def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        # path is like "/api/v2/accounts" — base_url is like "https://api.instantly.ai"
        url = self.base_url.rstrip("/") + "/" + path.lstrip("/")
        resp = self._session.get(url, params=params, timeout=60)
        if not resp.ok:
            raise InstantlyAPIError(
                f"GET {path} -> {resp.status_code}: {resp.text[:500]}"
            )
        return resp.json()

    # ------------------------------------------------------------------
    # Rate-limited request helper
    # ------------------------------------------------------------------

    def _get_with_retry(
        self,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        rate_limit_delay: float = 1.1,
        max_retries: int = 5,
    ) -> Any:
        """GET with rate limiting and exponential backoff on 429."""
        for attempt in range(max_retries + 1):
            time.sleep(rate_limit_delay)
            try:
                return self._get(path, params=params)
            except InstantlyAPIError as e:
                if "429" in str(e) and attempt < max_retries:
                    wait = 2 ** (attempt + 1)
                    time.sleep(wait)
                    continue
                raise
        # Shouldn't reach here, but fallback
        return self._get(path, params=params)

    # ------------------------------------------------------------------
    # Accounts
    # ------------------------------------------------------------------

    def list_accounts(self, include_tags: bool = False) -> List[dict]:
        """Return ALL accounts, following cursor-based pagination.

        GET /api/v2/accounts?limit=100&cursor=...

        Args:
            include_tags: When True, adds ?include_tags=true so each account
                          response includes a tags[] array with Custom Tags.
        """
        accounts: List[dict] = []
        cursor: Optional[str] = None

        while True:
            params: Dict[str, Any] = {"limit": 100}
            if include_tags:
                params["include_tags"] = "true"
            if cursor:
                params["starting_after"] = cursor

            data = self._get("api/v2/accounts", params=params)

            # Response structure: {"accounts": [...], "next_starting_after": "..." or None}
            batch = data.get("items", data.get("accounts", data.get("data", [])))
            accounts.extend(batch)

            cursor = data.get("next_starting_after")
            if not cursor:
                break

        return accounts

    # ------------------------------------------------------------------
    # Daily Analytics  —  single goldmine call
    # ------------------------------------------------------------------

    def get_daily_analytics(
        self,
        start_date: str,
        end_date: str,
        emails: Optional[List[str]] = None,
    ) -> List[dict]:
        """Fetch per-account, per-day analytics for a date range.

        GET /api/v2/accounts/analytics/daily?start_date=X&end_date=Y&emails=a,b
        """
        params: Dict[str, Any] = {
            "start_date": start_date,
            "end_date": end_date,
        }
        if emails:
            params["emails"] = ",".join(emails)

        data = self._get("/api/v2/accounts/analytics/daily", params=params)

        # Response could be a list directly, or nested under a key.
        if isinstance(data, list):
            return data
        return data.get("analytics", data.get("data", []))

    # ------------------------------------------------------------------
    # Account-Campaign Mappings
    # ------------------------------------------------------------------

    def get_account_campaign_mappings(self, email: str) -> List[dict]:
        """Return campaign mappings for a single account.

        GET /api/v2/account-campaign-mappings/{email}
        """
        path = f"/api/v2/account-campaign-mappings/{email}"
        data = self._get(path)

        if isinstance(data, list):
            return data
        return data.get("mappings", data.get("data", []))

    # ------------------------------------------------------------------
    # Campaigns (supplementary)
    # ------------------------------------------------------------------

    def list_campaigns(self) -> List[dict]:
        """List ALL campaigns, following cursor-based pagination.

        GET /api/v2/campaigns?limit=100&starting_after=...

        Response format: {"items": [...], "next_starting_after": "..."}
        """
        campaigns: List[dict] = []
        cursor: Optional[str] = None

        while True:
            params: Dict[str, Any] = {"limit": 100}
            if cursor:
                params["starting_after"] = cursor

            data = self._get("/api/v2/campaigns", params=params)

            if isinstance(data, list):
                # API returned a plain list — no pagination needed
                return data

            batch = data.get("items", data.get("campaigns", data.get("data", [])))
            campaigns.extend(batch)

            cursor = data.get("next_starting_after")
            if not cursor:
                break

        return campaigns

    # ------------------------------------------------------------------
    # Campaign Analytics Steps (Option B — single-call analytics)
    # ------------------------------------------------------------------

    def get_campaign_analytics_steps(
        self,
        start_date: str,
        end_date: str,
        campaign_ids: Optional[List[str]] = None,
    ) -> List[dict]:
        """Fetch per-step analytics for all campaigns in a date range.

        GET /api/v2/campaigns/analytics/steps?start_date=X&end_date=Y

        Returns one entry per campaign-step with sent/opened/replied/bounced
        metrics. Single API call — much faster than Option A's per-account
        campaign mapping calls.
        """
        params: Dict[str, Any] = {
            "start_date": start_date,
            "end_date": end_date,
        }
        if campaign_ids:
            params["campaign_ids"] = ",".join(campaign_ids)

        data = self._get("/api/v2/campaigns/analytics/steps", params=params)
        if isinstance(data, list):
            return data
        return data.get("steps", data.get("data", []))

    # ------------------------------------------------------------------
    # Emails — per-campaign email list with cursor pagination
    # ------------------------------------------------------------------

    def get_emails(
        self,
        campaign_id: str,
        email_type: str,
        limit: int = 100,
        starting_after: Optional[str] = None,
        sort_by: str = "timestamp_created",
        sort_order: str = "desc",
    ) -> dict:
        """Fetch emails for a campaign with cursor-based pagination.

        GET /api/v2/emails?campaign_id=X&ue_type=1|2&limit=N&starting_after=...

        NOTE: The correct endpoint is /api/v2/emails (NOT /api/v2/campaigns/{id}/emails).

        NOTE: ue_type is the authoritative field:
          ue_type=1 = sent emails
          ue_type=2 = received emails
        email_type param is always null in the API.

        WARNING: min_timestamp_created returns HTTP 500 — do NOT add it.

        Args:
            campaign_id: The campaign UUID.
            email_type: Used to derive ue_type param (1=sent, 2=received).
            limit: Page size (default 100).
            starting_after: Cursor for next page.
            sort_by: Sort field (default "timestamp_created").
            sort_order: "asc" or "desc" (default "desc").

        Returns:
            dict with {"items": [...], "next_starting_after": "uuid"|None}
        """
        # Map email_type to ue_type (the authoritative field)
        ue_type = 1 if email_type == "sent" else 2 if email_type == "received" else None

        params: Dict[str, Any] = {
            "campaign_id": campaign_id,
            "limit": limit,
            "sort_by": sort_by,
            "sort_order": sort_order,
        }
        if ue_type is not None:
            params["ue_type"] = ue_type
        if starting_after:
            params["starting_after"] = starting_after

        return self._get_with_retry("/api/v2/emails", params=params)

    # ------------------------------------------------------------------
    # Campaign Analytics — aggregated per-campaign stats
    # ------------------------------------------------------------------

    def get_campaign_analytics(self, campaign_ids: List[str]) -> List[dict]:
        """Fetch aggregated analytics for one or more campaigns.

        POST /api/v2/campaigns/analytics

        ⚠️ CRITICAL: Returns a plain JSON array [...], NOT wrapped in {items: [...]}.
        Always check isinstance(data, list) before accessing.

        Rate limit: 1.1s delay between calls.

        Args:
            campaign_ids: List of campaign UUIDs.

        Returns:
            List of campaign analytics dicts with fields like:
            emails_sent_count, reply_count, reply_count_unique,
            reply_count_automatic, reply_count_automatic_unique,
            bounced_count, total_opportunities, etc.
        """
        time.sleep(1.1)
        data = self._get(
            "/api/v2/campaigns/analytics",
            params={"campaign_ids": ",".join(campaign_ids)},
        )
        if isinstance(data, list):
            return data
        # Fallback in case API changes shape
        return data.get("analytics", data.get("data", []))

    # ------------------------------------------------------------------
    # Campaign Detail — single campaign object
    # ------------------------------------------------------------------

    def get_campaign_detail(self, campaign_id: str) -> dict:
        """Fetch details for a single campaign.

        GET /api/v2/campaigns/{campaign_id}

        Returns a single campaign object (NOT wrapped in {items: [...]}).

        NOTE: email_list is ALWAYS empty — do not rely on it for
        account-campaign mapping. Derive mappings from email records.

        Args:
            campaign_id: The campaign UUID.

        Returns:
            Dict with campaign fields.
        """
        path = f"/api/v2/campaigns/{campaign_id}"
        data = self._get(path)
        if isinstance(data, dict) and "campaign" in data:
            return data["campaign"]
        return data
