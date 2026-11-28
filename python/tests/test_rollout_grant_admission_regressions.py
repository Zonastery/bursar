"""PostgreSQL regressions for grant replay and account admission."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Barrier

import psycopg2
from psycopg2.extras import Json

from tests.conftest import TEST_TENANT_ID

SUBJECT = "00000000-0000-0000-0000-000000009901"


def _connection(dsn):
    conn = psycopg2.connect(dsn)
    with conn.cursor() as cur:
        cur.execute("SELECT set_config('bursar.tenant_id', %s, false)", (TEST_TENANT_ID,))
        cur.execute("SELECT set_config('bursar.provider_environment', 'test', false)")
    return conn


def _config():
    return {
        "version": 1,
        "credits": {
            "buckets": {"default": {"priority": 10, "expiry": {"type": "never"}}},
            "default_bucket": "default",
            "grant_programs": {
                "promo": {
                    "trigger": "manual",
                    "awards": [{"recipient": "subject", "amount": "1", "bucket": "default"}],
                    "idempotency_scope": "subject",
                    "max_awards_per_subject": 10,
                }
            },
        },
        "pricing": {"operations": {"a": {"measures": {}}, "b": {"measures": {}}}},
        "admission": {"policies": {"cap": {"max_in_flight": 1}}},
        "plans": {"pro": {"display_name": "Pro", "allowed_operations": "all", "admission_policy": "cap"}},
    }


def _publish(cur, config, label):
    cur.execute("SELECT revision_id FROM bursar.publish_and_activate_catalog(1,%s,%s)", (Json(config), label))
    return cur.fetchone()[0]


def _grant(cur):
    cur.execute(
        "SELECT ledger_entry_id,replayed,error_code FROM "
        "bursar.execute_grant_program('manual','promo',%s,'event-1',NULL,NULL,'{}')",
        (SUBJECT,),
    )
    return cur.fetchone()


def test_grant_replay_survives_scope_and_catalog_removal(pg_database_url):
    config = _config()
    with _connection(pg_database_url) as conn, conn.cursor() as cur:
        _publish(cur, config, "initial")
        first = _grant(cur)
        assert first[1:] == (False, None)
        changed = deepcopy(config)
        changed["credits"]["grant_programs"]["promo"]["idempotency_scope"] = "event"
        _publish(cur, changed, "scope-change")
        assert _grant(cur) == (first[0], True, None)
        cur.execute(
            "SELECT ledger_entry_id,replayed,error_code FROM "
            "bursar.execute_grant_program('manual','promo',%s,'event-2',NULL,NULL,'{}')",
            (SUBJECT,),
        )
        second = cur.fetchone()
        assert second[0] != first[0] and second[1:] == (False, None)
        cur.execute(
            "SELECT replayed,error_code FROM bursar.execute_grant_program('manual','promo',%s,%s,NULL,NULL,'{}')",
            (SUBJECT, SUBJECT),
        )
        assert cur.fetchone() == (False, None)
        changed["credits"]["grant_programs"]["promo"]["idempotency_scope"] = "subject"
        _publish(cur, changed, "subject-again")
        cur.execute(
            "SELECT ledger_entry_id,replayed,error_code FROM "
            "bursar.execute_grant_program('manual','promo',%s,'event-3',NULL,NULL,'{}')",
            (SUBJECT,),
        )
        assert cur.fetchone() == (first[0], True, None)
        del changed["credits"]["grant_programs"]
        _publish(cur, changed, "removed")
        assert _grant(cur) == (first[0], True, None)
        cur.execute("SELECT balance FROM bursar.credit_accounts WHERE subject_id=%s", (SUBJECT,))
        assert cur.fetchone()[0] == 3
        cur.execute("SELECT count(*) FROM bursar.grant_program_events WHERE subject_id=%s", (SUBJECT,))
        assert cur.fetchone()[0] == 3


def test_global_admission_counts_across_operations(pg_database_url):
    with _connection(pg_database_url) as conn, conn.cursor() as cur:
        revision = _publish(cur, _config(), "admission")
        cur.execute("SELECT id FROM bursar.catalog_plans WHERE catalog_revision_id=%s AND plan_key='pro'", (revision,))
        plan = cur.fetchone()[0]
        cur.execute("SELECT bursar.assign_plan(%s,%s,now(),NULL)", (SUBJECT, plan))
        for operation, expected in (("a", None), ("b", "max_concurrent_reached")):
            cur.execute(
                "SELECT error_code FROM bursar.create_lease_for_operation(%s,%s,0,%s)", (SUBJECT, operation, operation)
            )
            assert cur.fetchone()[0] == expected


def test_same_plan_future_request_preserves_current_assignment(pg_database_url):
    with _connection(pg_database_url) as conn, conn.cursor() as cur:
        _publish(cur, _config(), "plan")
        cur.execute("SELECT * FROM bursar.set_subject_plan(%s,'pro',NULL)", (SUBJECT,))
        original = cur.fetchone()
        cur.execute("SELECT * FROM bursar.set_subject_plan(%s,'pro',now() + interval '1 day')", (SUBJECT,))
        result = cur.fetchone()
        assert result[1] == original[1]
        assert result[3] == original[3]
        assert result[4] == "applied"
        cur.execute(
            "SELECT starts_at <= now() FROM bursar.account_plan_assignments a JOIN "
            "bursar.credit_accounts c ON c.id=a.account_id WHERE c.subject_id=%s",
            (SUBJECT,),
        )
        assert cur.fetchone()[0]


def test_unchanged_plan_stays_pinned_but_shared_policy_change_rolls_out(pg_database_url):
    config = _config()
    with _connection(pg_database_url) as conn, conn.cursor() as cur:
        first = _publish(cur, config, "first")
        cur.execute("SELECT * FROM bursar.set_subject_plan(%s,'pro',NULL)", (SUBJECT,))
        cur.fetchone()
        other = deepcopy(config)
        other["plans"]["other"] = {"display_name": "Other"}
        _publish(cur, other, "unrelated-plan")
        cur.execute(
            "SELECT catalog_revision_id FROM bursar.account_plan_assignments a JOIN "
            "bursar.credit_accounts c ON c.id=a.account_id WHERE c.subject_id=%s",
            (SUBJECT,),
        )
        assert cur.fetchone()[0] == first
        other["admission"]["policies"]["cap"]["max_in_flight"] = 2
        latest = _publish(cur, other, "shared-admission")
        cur.execute(
            "SELECT catalog_revision_id FROM bursar.account_plan_assignments a JOIN "
            "bursar.credit_accounts c ON c.id=a.account_id WHERE c.subject_id=%s",
            (SUBJECT,),
        )
        assert cur.fetchone()[0] == latest


def test_concurrent_first_grant_requests_share_one_award(pg_database_url):
    with _connection(pg_database_url) as conn, conn.cursor() as cur:
        _publish(cur, _config(), "concurrent-grant")
    barrier = Barrier(2)

    def run_grant():
        with _connection(pg_database_url) as conn, conn.cursor() as cur:
            barrier.wait(timeout=10)
            return _grant(cur)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: run_grant(), range(2)))
    assert results[0][0] == results[1][0]
    assert sorted(row[1] for row in results) == [False, True]
    assert all(row[2] is None for row in results)


def test_next_renewal_skips_open_ended_assignment_without_blocking_others(pg_database_url):
    config = _config()
    config["plans"]["pro"]["evolution"] = {"default_rollout": "next_renewal"}
    config["commerce"] = {
        "providers": {"custom": {"type": "custom", "adapter": "test"}},
        "offers": {
            "monthly": {
                "type": "subscription",
                "display_name": "Monthly",
                "plan": "pro",
                "price": {"amount_minor": 100, "currency": "USD", "tax_behavior": "exclusive"},
                "providers": {
                    "custom": {"type": "custom_object", "object_kind": "subscription", "external_id": "monthly"}
                },
                "billing_interval": {"unit": "month", "count": 1},
            }
        },
    }
    other_subject = "00000000-0000-0000-0000-000000009902"
    with _connection(pg_database_url) as conn, conn.cursor() as cur:
        first = _publish(cur, config, "renewal-first")
        cur.execute("SELECT id FROM bursar.catalog_plans WHERE catalog_revision_id=%s AND plan_key='pro'", (first,))
        plan = cur.fetchone()[0]
        cur.execute("SELECT bursar.assign_plan(%s,%s,now(),NULL)", (SUBJECT, plan))
        cur.execute("SELECT bursar.assign_plan(%s,%s,now(),now()+interval '1 day')", (other_subject, plan))
        changed = deepcopy(config)
        changed["admission"]["policies"]["cap"]["max_in_flight"] = 2
        _publish(cur, changed, "renewal-changed")
        cur.execute(
            "SELECT c.subject_id, a.catalog_revision_id, count(ch.id) FROM "
            "bursar.account_plan_assignments a JOIN bursar.credit_accounts c ON c.id=a.account_id "
            "LEFT JOIN bursar.plan_assignment_changes ch ON ch.account_id=a.account_id AND "
            "ch.state='scheduled' GROUP BY c.subject_id,a.catalog_revision_id ORDER BY "
            "c.subject_id"
        )
        rows = cur.fetchall()
        assert rows == [(SUBJECT, first, 0), (other_subject, first, 1)]


def test_only_referenced_rate_card_changes_roll_out(pg_database_url):
    config = _config()
    config["plans"]["pro"]["rate_card"] = "standard"
    config["pricing"]["rate_cards"] = {
        "standard": {
            "operations": {"a": {"unmatched": {"action": "charge", "charge": {"type": "flat", "amount": "1"}}}}
        },
    }
    with _connection(pg_database_url) as conn, conn.cursor() as cur:
        first = _publish(cur, config, "rates-first")
        cur.execute("SELECT * FROM bursar.set_subject_plan(%s,'pro',NULL)", (SUBJECT,))
        cur.fetchone()
        changed = deepcopy(config)
        changed["pricing"]["rate_cards"]["unused"] = {"operations": {}}
        changed["plans"]["pro"]["display_name"] = "New label"
        _publish(cur, changed, "unrelated-rate-and-label")
        cur.execute(
            "SELECT catalog_revision_id FROM bursar.account_plan_assignments a JOIN "
            "bursar.credit_accounts c ON c.id=a.account_id WHERE c.subject_id=%s",
            (SUBJECT,),
        )
        assert cur.fetchone()[0] == first
        changed["pricing"]["rate_cards"]["standard"]["operations"]["a"]["unmatched"]["charge"]["amount"] = "2"
        latest = _publish(cur, changed, "changed-rate")
        cur.execute(
            "SELECT catalog_revision_id FROM bursar.account_plan_assignments a JOIN "
            "bursar.credit_accounts c ON c.id=a.account_id WHERE c.subject_id=%s",
            (SUBJECT,),
        )
        assert cur.fetchone()[0] == latest


def test_tenant_environment_binding_rejects_wrong_namespace_and_operator_replay(pg_database_url):
    import pytest

    from bursar.credits.postgres.store import PostgresStore
    from bursar.errors import StoreError

    with _connection(pg_database_url) as conn, conn.cursor() as cur:
        _publish(cur, _config(), "environment")
        cur.execute("SELECT * FROM bursar.set_subject_plan(%s,'pro',NULL)", (SUBJECT,))
        cur.fetchone()
    wrong_store = PostgresStore(pg_database_url, tenant_id=TEST_TENANT_ID, provider_environment="live")
    try:
        with pytest.raises(StoreError, match="environment"):
            wrong_store.get_user_plan(SUBJECT)
    finally:
        wrong_store.close()

    with psycopg2.connect(pg_database_url) as conn, conn.cursor() as cur:
        cur.execute("SET LOCAL ROLE bursar_client")
        with pytest.raises(psycopg2.Error, match="environment"):
            cur.execute("SELECT bursar.bind_tenant_context(%s::uuid,'live')", (TEST_TENANT_ID,))
        conn.rollback()
        cur.execute("SET LOCAL ROLE bursar_client")
        cur.execute(
            "SELECT set_config('bursar.tenant_id',%s,true), set_config('bursar.provider_environment','live',true)",
            (TEST_TENANT_ID,),
        )
        with pytest.raises(psycopg2.Error, match="environment"):
            cur.execute("SELECT * FROM bursar.get_credit_state(%s)", (SUBJECT,))

    with psycopg2.connect(pg_database_url) as conn, conn.cursor() as cur:
        cur.execute("SET LOCAL ROLE bursar_operator")
        with pytest.raises(psycopg2.Error, match="environment"):
            cur.execute("SELECT bursar.create_tenant(%s,'bursar-tests','Bursar tests','live')", (TEST_TENANT_ID,))


def test_operator_context_binding_and_tenant_lifecycle(pg_database_url):
    import pytest

    with psycopg2.connect(pg_database_url) as conn, conn.cursor() as cur:
        cur.execute("SET LOCAL ROLE bursar_operator")
        cur.execute("SELECT * FROM bursar.get_tenant_context_by_slug('bursar-tests')")
        assert str(cur.fetchone()[0]) == TEST_TENANT_ID
        cur.execute("SELECT bursar.bind_tenant_context(%s::uuid,'test')", (TEST_TENANT_ID,))
        assert str(cur.fetchone()[0]) == TEST_TENANT_ID
        cur.execute("SELECT bursar.create_tenant(%s,'bursar-tests','Bursar tests','test')", (TEST_TENANT_ID,))
        assert str(cur.fetchone()[0]) == TEST_TENANT_ID
        cur.execute("SELECT bursar.set_tenant_status(%s,'suspended')", (TEST_TENANT_ID,))
        cur.execute("SELECT * FROM bursar.get_tenant_context_by_slug('bursar-tests')")
        assert cur.fetchone() is None
        with pytest.raises(psycopg2.Error, match="inactive"):
            cur.execute("SELECT bursar.bind_tenant_context(%s::uuid,'test')", (TEST_TENANT_ID,))
