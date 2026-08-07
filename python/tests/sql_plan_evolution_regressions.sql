-- Regression coverage for catalog plan defaults, release overrides, pins, and
-- same-plan accounting continuity. Run after bundled migrations.
BEGIN;

SELECT bursar.create_tenant(
    '00000000-0000-0000-0000-000000000777'::uuid,
    'plan-evolution-regression',
    'Plan evolution regression'
);
SELECT set_config(
    'bursar.tenant_id',
    '00000000-0000-0000-0000-000000000777',
    false
);

DO $$
DECLARE
    v_seeker_subject uuid := '00000000-0000-0000-0000-000000000778';
    v_new_seeker_subject uuid := '00000000-0000-0000-0000-000000000779';
    v_monk_subject uuid := '00000000-0000-0000-0000-000000000780';
    v_start timestamptz := now() - interval '5 days';
    v_renewal timestamptz := now() + interval '7 days';
    v_window_start timestamptz := date_trunc('month', now());
    v_window_end timestamptz := date_trunc('month', now()) + interval '1 month';
    v_doc jsonb := $json$
    {
      "version": 1,
      "pricing": {
        "operations": {
          "chat": {
            "measures": {"requests": {"unit": "request"}},
            "dimensions": {}
          }
        },
        "rate_cards": {}
      },
      "credits": {
        "buckets": {"included": {"priority": 10}},
        "default_bucket": "included"
      },
      "plans": {
        "seeker": {
          "display_name": "Seeker",
          "rank": 0,
          "evolution": {"default_rollout": "new_assignments_only"},
          "credit_allowance": {
            "amount": "100",
            "window": {
              "type": "calendar",
              "unit": "month",
              "count": 1,
              "timezone": "UTC"
            }
          },
          "quotas": {
            "requests": {
              "operation": "chat",
              "measure": "requests",
              "limit": "10",
              "window": {
                "type": "calendar",
                "unit": "month",
                "count": 1,
                "timezone": "UTC"
              },
              "enforcement": "block"
            }
          }
        },
        "monk": {
          "display_name": "Monk",
          "rank": 1,
          "evolution": {"default_rollout": "immediate"}
        }
      },
      "commerce": {
        "providers": {"custom": {"type": "custom", "adapter": "test"}},
        "offers": {
          "monk_monthly": {
            "type": "subscription",
            "display_name": "Monk monthly",
            "price": {
              "amount_minor": 1000,
              "currency": "USD",
              "tax_behavior": "exclusive"
            },
            "providers": {
              "custom": {
                "type": "custom_object",
                "object_kind": "subscription",
                "external_id": "monk-monthly"
              }
            },
            "plan": "monk",
            "billing_interval": {"unit": "month", "count": 1}
          }
        }
      }
    }
    $json$::jsonb;
    v_doc_2 jsonb;
    v_doc_3 jsonb;
    v_doc_4 jsonb;
    v_revision_1 uuid;
    v_revision_2 uuid;
    v_revision_3 uuid;
    v_revision_4 uuid;
    v_revision_no_3 bigint;
    v_revision_no_4 bigint;
    v_seeker_plan_1 uuid;
    v_seeker_plan_2 uuid;
    v_seeker_plan_3 uuid;
    v_seeker_plan_4 uuid;
    v_monk_plan_1 uuid;
    v_monk_plan_4 uuid;
    v_account uuid;
    v_assignment record;
BEGIN
    SELECT revision_id
    INTO v_revision_1
    FROM bursar.publish_and_activate_catalog(1, v_doc, 'evolution-v1');

    SELECT id INTO v_seeker_plan_1
    FROM bursar.catalog_plans
    WHERE catalog_revision_id = v_revision_1 AND plan_key = 'seeker';
    SELECT id INTO v_monk_plan_1
    FROM bursar.catalog_plans
    WHERE catalog_revision_id = v_revision_1 AND plan_key = 'monk';

    IF NOT bursar.assign_plan(v_seeker_subject, v_seeker_plan_1, v_start, NULL)
       OR NOT bursar.assign_plan(v_monk_subject, v_monk_plan_1, v_start, v_renewal)
    THEN
        RAISE EXCEPTION 'initial plan assignment failed';
    END IF;

    SELECT id INTO v_account
    FROM bursar.credit_accounts
    WHERE subject_id = v_seeker_subject AND account_kind = 'personal';

    INSERT INTO bursar.allowance_windows(
        account_id,
        plan_id,
        catalog_revision_id,
        allowance_key,
        window_start,
        window_end,
        period_unit,
        period_count,
        period_anchor,
        period_timezone,
        allowance,
        consumed,
        policy_snapshot
    ) VALUES (
        v_account,
        v_seeker_plan_1,
        v_revision_1,
        '__included_credits__',
        v_window_start,
        v_window_end,
        'month',
        1,
        'calendar',
        'UTC',
        100,
        70,
        (
            SELECT plan.definition->'credit_allowance'
            FROM bursar.catalog_plans AS plan
            WHERE plan.id = v_seeker_plan_1
              AND plan.catalog_revision_id = v_revision_1
        )
    );

    INSERT INTO bursar.quota_windows(
        account_id,
        plan_id,
        catalog_revision_id,
        quota_key,
        operation_key,
        measure_key,
        window_start,
        window_end,
        quota_limit,
        consumed,
        enforcement,
        policy_snapshot
    ) VALUES (
        v_account,
        v_seeker_plan_1,
        v_revision_1,
        'requests',
        'chat',
        'requests',
        v_window_start,
        v_window_end,
        10,
        7,
        'block',
        (
            SELECT quota.definition
            FROM bursar.catalog_plan_quotas AS quota
            WHERE quota.catalog_revision_id = v_revision_1
              AND quota.plan_key = 'seeker'
              AND quota.quota_key = 'requests'
        )
    );

    v_doc_2 := jsonb_set(
        jsonb_set(
            jsonb_set(
                jsonb_set(
                    v_doc,
                    '{plans,seeker,evolution,default_rollout}',
                    '"immediate"'::jsonb
                ),
                '{plans,monk,evolution,default_rollout}',
                '"next_renewal"'::jsonb
            ),
            '{plans,seeker,credit_allowance,amount}',
            '"60"'::jsonb
        ),
        '{plans,seeker,quotas,requests,limit}',
        '"6"'::jsonb
    );

    SELECT revision_id
    INTO v_revision_2
    FROM bursar.publish_and_activate_catalog(1, v_doc_2, 'evolution-v2');

    SELECT id INTO v_seeker_plan_2
    FROM bursar.catalog_plans
    WHERE catalog_revision_id = v_revision_2 AND plan_key = 'seeker';

    SELECT * INTO v_assignment
    FROM bursar.account_plan_assignments
    WHERE account_id = v_account;
    IF v_assignment.plan_id <> v_seeker_plan_2
       OR v_assignment.starts_at <> v_start
       OR v_assignment.source_type <> 'manual'
    THEN
        RAISE EXCEPTION 'target immediate policy did not preserve assignment identity';
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM bursar.allowance_windows
        WHERE account_id = v_account
          AND plan_id = v_seeker_plan_2
          AND catalog_revision_id = v_revision_2
          AND allowance = 60
          AND consumed = 70
    ) OR NOT EXISTS (
        SELECT 1
        FROM bursar.quota_windows
        WHERE account_id = v_account
          AND plan_id = v_seeker_plan_2
          AND catalog_revision_id = v_revision_2
          AND quota_limit = 6
          AND consumed = 7
    )
    THEN
        RAISE EXCEPTION 'same-plan accounting state was reset during immediate rollout';
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM bursar.plan_assignment_changes AS change
        JOIN bursar.credit_accounts AS account ON account.id = change.account_id
        WHERE account.subject_id = v_monk_subject
          AND change.change_kind = 'catalog_revision'
          AND change.state = 'scheduled'
          AND change.effective_at = v_renewal
    )
    THEN
        RAISE EXCEPTION 'target next-renewal policy was not scheduled';
    END IF;

    v_doc_3 := jsonb_set(
        jsonb_set(
            v_doc_2,
            '{plans,seeker,credit_allowance,amount}',
            '"70"'::jsonb
        ),
        '{plans,seeker,quotas,requests,limit}',
        '"7"'::jsonb
    );

    SELECT revision_id, revision_no
    INTO v_revision_3, v_revision_no_3
    FROM bursar.publish_and_activate_catalog(
        1,
        v_doc_3,
        'evolution-v3',
        true,
        '{"plans":{"seeker":{"effective":"new_assignments_only"}}}'::jsonb
    );

    SELECT id INTO v_seeker_plan_3
    FROM bursar.catalog_plans
    WHERE catalog_revision_id = v_revision_3 AND plan_key = 'seeker';

    IF (SELECT plan_id FROM bursar.account_plan_assignments WHERE account_id = v_account)
       <> v_seeker_plan_2
    THEN
        RAISE EXCEPTION 'new-assignments-only changed an existing assignment';
    END IF;

    IF NOT bursar.assign_plan(
        v_seeker_subject,
        v_seeker_plan_3
    ) OR (
        SELECT plan_id
        FROM bursar.account_plan_assignments
        WHERE account_id = v_account
    ) <> v_seeker_plan_2
    OR (
        SELECT starts_at
        FROM bursar.account_plan_assignments
        WHERE account_id = v_account
    ) <> v_start
    THEN
        RAISE EXCEPTION 'same-key reprovisioning bypassed rollout policy or reset its assignment epoch';
    END IF;

    IF NOT bursar.assign_plan(
        v_new_seeker_subject,
        v_seeker_plan_3,
        now(),
        NULL
    )
    THEN
        RAISE EXCEPTION 'new assignment to active target failed';
    END IF;

    PERFORM bursar.activate_catalog_revision(
        v_revision_no_3,
        '{"plans":{"seeker":{"effective":"immediate"}}}'::jsonb
    );
    IF (SELECT plan_id FROM bursar.account_plan_assignments WHERE account_id = v_account)
       <> v_seeker_plan_3
    THEN
        RAISE EXCEPTION 'same-revision catch-up rollout did not migrate existing assignment';
    END IF;

    IF NOT bursar.set_plan_revision_pin(v_seeker_subject, true) THEN
        RAISE EXCEPTION 'pinning current assignment failed';
    END IF;

    v_doc_4 := jsonb_set(
        jsonb_set(
            v_doc_3,
            '{plans,seeker,credit_allowance,amount}',
            '"80"'::jsonb
        ),
        '{plans,seeker,quotas,requests,limit}',
        '"8"'::jsonb
    );

    SELECT revision_id, revision_no
    INTO v_revision_4, v_revision_no_4
    FROM bursar.publish_and_activate_catalog(1, v_doc_4, 'evolution-v4');

    SELECT id INTO v_seeker_plan_4
    FROM bursar.catalog_plans
    WHERE catalog_revision_id = v_revision_4 AND plan_key = 'seeker';
    SELECT id INTO v_monk_plan_4
    FROM bursar.catalog_plans
    WHERE catalog_revision_id = v_revision_4 AND plan_key = 'monk';

    IF (SELECT plan_id FROM bursar.account_plan_assignments WHERE account_id = v_account)
       <> v_seeker_plan_3
    THEN
        RAISE EXCEPTION 'pinned assignment moved without an explicit override';
    END IF;

    PERFORM bursar.activate_catalog_revision(
        v_revision_no_4,
        '{"plans":{"seeker":{"effective":"immediate","include_pinned":true}}}'::jsonb
    );
    SELECT * INTO v_assignment
    FROM bursar.account_plan_assignments
    WHERE account_id = v_account;
    IF v_assignment.plan_id <> v_seeker_plan_4
       OR NOT v_assignment.catalog_revision_pinned
       OR v_assignment.starts_at <> v_start
    THEN
        RAISE EXCEPTION 'include-pinned rollout did not preserve pin and assignment epoch';
    END IF;

    UPDATE bursar.plan_assignment_changes AS change
    SET effective_at = now() - interval '1 second'
    FROM bursar.credit_accounts AS account
    WHERE account.id = change.account_id
      AND account.subject_id = v_monk_subject
      AND change.change_kind = 'catalog_revision'
      AND change.state = 'scheduled';

    PERFORM bursar.apply_due_plan_assignment_changes(100);
    SELECT assignment.* INTO v_assignment
    FROM bursar.account_plan_assignments AS assignment
    JOIN bursar.credit_accounts AS account ON account.id = assignment.account_id
    WHERE account.subject_id = v_monk_subject;
    IF v_assignment.plan_id <> v_monk_plan_4
       OR v_assignment.starts_at <> v_start
       OR v_assignment.source_type <> 'manual'
       OR v_assignment.ends_at <> v_renewal
    THEN
        RAISE EXCEPTION 'renewal rollout reset stable assignment state';
    END IF;

    BEGIN
        PERFORM bursar.publish_and_activate_catalog(
            1,
            jsonb_set(
                v_doc_4,
                '{plans,seeker,quotas,requests,window,unit}',
                '"day"'::jsonb
            ),
            'invalid-immediate-window-change'
        );
        RAISE EXCEPTION 'incompatible immediate quota window change was accepted';
    EXCEPTION
        WHEN SQLSTATE '22023' THEN
            IF SQLERRM NOT LIKE '%immediate rollout cannot change%quota%window%'
            THEN
                RAISE;
            END IF;
    END;
END
$$;

ROLLBACK;
