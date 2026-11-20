"""Batch campaign-mapping script — processes accounts concurrently.

The per-account campaign mapping endpoint is slow when called 1,184 times
sequentially.  This script uses a thread pool to parallelise the work in
small batches, with a configurable delay between batches to avoid
triggering API rate limits.
"""

import concurrent.futures
import time
from typing import List, Optional

from .api import InstantlyClient, InstantlyAPIError
from .config import settings
from .db import close_connection, get_connection, init_db, upsert_account_campaign


def _fetch_and_store(email: str, client: InstantlyClient) -> int:
    """Fetch campaign mappings for one email and store them.

    Returns the number of mappings stored (0 on error).
    """
    try:
        mappings = client.get_account_campaign_mappings(email)
    except InstantlyAPIError:
        return 0

    if not mappings:
        return 0

    conn = get_connection()
    count = 0
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
    return count


def refresh_campaign_mappings_batched(
    batch_size: int = 50,
    max_workers: int = 10,
    delay_s: float = 1.0,
    progress: bool = True,
) -> int:
    """Fetch campaign mappings for all accounts in parallel batches.

    Args:
        batch_size: Accounts per batch (default: 50).
        max_workers: Thread pool size (default: 10).
        delay_s: Seconds to wait between batches (default: 1.0).
        progress: Print progress dots and summary.

    Returns:
        Total number of campaign mappings upserted.
    """
    init_db()
    conn = get_connection()
    emails = [
        r["email"]
        for r in conn.execute("SELECT email FROM accounts").fetchall()
    ]
    del conn  # don't keep the connection open during API calls

    if not settings.is_configured:
        print("ERROR: INSTANTLY_API_KEY not set.")
        return 0

    client = InstantlyClient()
    total_mappings = 0
    total_accounts_with_data = 0

    if progress:
        print(
            f"🔄 Fetching campaign mappings for {len(emails)} accounts "
            f"({batch_size}/batch, {max_workers} workers)…"
        )

    for batch_start in range(0, len(emails), batch_size):
        batch = emails[batch_start : batch_start + batch_size]

        with concurrent.futures.ThreadPoolExecutor(
            max_workers=max_workers
        ) as pool:
            futures = {
                pool.submit(_fetch_and_store, email, client): email
                for email in batch
            }
            for future in concurrent.futures.as_completed(futures):
                try:
                    n = future.result()
                    if n > 0:
                        total_accounts_with_data += 1
                    total_mappings += n
                except Exception:
                    pass

        if progress:
            done = min(batch_start + batch_size, len(emails))
            pct = done * 100 // len(emails)
            print(
                f"  [{pct:3d}%] {done}/{len(emails)} — "
                f"{total_mappings} mappings so far"
            )

        if batch_start + batch_size < len(emails):
            time.sleep(delay_s)

    if progress:
        print(f"✅ Done — {total_mappings} campaign mappings from "
              f"{total_accounts_with_data} accounts")

    return total_mappings


if __name__ == "__main__":
    refresh_campaign_mappings_batched()
