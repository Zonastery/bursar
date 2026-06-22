"""Seed default pricing config into credit_pricing_config table.

One-shot init container. Runs alongside agent-py (tables created by
setup_credit_schema). Retries until the get_active_pricing RPC is
available, then seeds default pricing if none exists.
"""

import json
import os
import sys
import time
from pathlib import Path

from supabase import create_client

from ducto.interface.models import PricingConfigData
from ducto.interface.supabase import SupabaseStore

RETRY_MAX = 30
RETRY_DELAY = 2

SUPABASE_URL = os.environ.get("SUPABASE_URL") or sys.exit("SUPABASE_URL required")
SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or sys.exit("SUPABASE_SERVICE_ROLE_KEY required")

_DEFAULTS_PATH = Path(__file__).parent / "pricing-defaults.json"
DEFAULT_CONFIG = PricingConfigData.model_validate(json.loads(_DEFAULTS_PATH.read_text()))


def _rpc_ready(supabase) -> bool:
    """Check if the get_active_pricing_config RPC exists."""
    try:
        supabase.rpc("get_active_pricing_config").execute()
        return True
    except Exception:
        return False


def main() -> None:
    supabase = create_client(SUPABASE_URL, SERVICE_KEY)
    rest = SupabaseStore(supabase)

    # Wait for the RPC to exist (created by agent-py's setup_credit_schema)
    for attempt in range(RETRY_MAX):
        if _rpc_ready(supabase):
            print(f"[seed-pricing] RPC ready after {attempt * RETRY_DELAY}s")
            break
        if attempt == RETRY_MAX - 1:
            print("[seed-pricing] RPC not available after max retries — exiting")
            sys.exit(1)
        time.sleep(RETRY_DELAY)

    existing = rest.get_active_pricing()
    if existing is not None:
        print(f"[seed-pricing] Active pricing already exists (id={existing.id}) — skipping")
        return

    rest.set_active_pricing(DEFAULT_CONFIG)
    print("[seed-pricing] Default pricing seeded successfully")


if __name__ == "__main__":
    main()
