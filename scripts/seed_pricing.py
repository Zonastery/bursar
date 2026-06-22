"""Seed default pricing config into credit_pricing_config table.

One-shot init container. Runs migrations then seeds default pricing
if none exists.
"""

import os
import sys
import time
from pathlib import Path

import httpx
import yaml

from ducto.interface.models import PricingConfigData
from ducto.interface.supabase import HttpxSupabaseStore, run_migrations

RETRY_MAX = 30
RETRY_DELAY = 2

SUPABASE_URL = os.environ.get("SUPABASE_URL") or sys.exit("SUPABASE_URL required")
SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or sys.exit("SUPABASE_SERVICE_ROLE_KEY required")
DATABASE_URL = os.environ.get("SUPABASE_DB_URL")

_DEFAULTS_PATH = Path(__file__).parent / "pricing-defaults.yaml"
DEFAULT_CONFIG = PricingConfigData.model_validate(yaml.safe_load(_DEFAULTS_PATH.read_text()))


def main() -> None:
    rest = HttpxSupabaseStore(url=SUPABASE_URL, key=SERVICE_KEY)

    # Run schema migrations before seeding
    if DATABASE_URL:
        result = run_migrations(DATABASE_URL)
        if result.errors:
            print(f"[seed-pricing] Migration errors: {result.errors}")
        else:
            print(f"[seed-pricing] Schema ready: {result.tables_created}")
    else:
        print("[seed-pricing] No SUPABASE_DB_URL — skipping migrations")

    # Wait for the get_active_pricing_config RPC to be available
    existing = None
    for attempt in range(RETRY_MAX):
        try:
            existing = rest.get_active_pricing()
            print(f"[seed-pricing] RPC ready after {attempt * RETRY_DELAY}s")
            break
        except httpx.HTTPStatusError:
            if attempt == RETRY_MAX - 1:
                print("[seed-pricing] RPC not available after max retries — exiting")
                sys.exit(1)
            time.sleep(RETRY_DELAY)

    if existing is not None:
        print(f"[seed-pricing] Active pricing already exists (id={existing.id}) — skipping")
        return

    rest.set_active_pricing(DEFAULT_CONFIG)
    print("[seed-pricing] Default pricing seeded successfully")


if __name__ == "__main__":
    main()
