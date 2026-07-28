"""Credits config parser — mirrors JS SDK's ``config/parse-credits.ts``."""

from __future__ import annotations

from bursar.config.types import (
    CreditsConfig,
    SubscriptionEndExpiry,
    _validate_map_keys,
)


def _validate_credits(credits: CreditsConfig) -> CreditsConfig:
    _validate_map_keys(credits.buckets, "credits.buckets")
    _validate_map_keys(credits.policies, "credits.policies")
    _validate_map_keys(credits.grant_programs, "credits.grant_programs")
    priorities = [bucket.priority for bucket in credits.buckets.values()]
    if len(priorities) != len(set(priorities)):
        raise ValueError("credits bucket priorities must be unique")
    if credits.buckets and credits.default_bucket not in credits.buckets:
        raise ValueError("credits.default_bucket must reference a configured bucket")
    if not credits.buckets and credits.default_bucket is not None:
        raise ValueError("credits.default_bucket requires credits.buckets")
    for bucket_key, bucket in credits.buckets.items():
        if isinstance(bucket.expiry, SubscriptionEndExpiry):
            raise ValueError(
                f"credits.buckets.{bucket_key}.expiry subscription_end is only valid for subscription cycle grants"
            )
    for program_key, program in credits.grant_programs.items():
        for award in program.awards:
            if award.bucket not in credits.buckets:
                raise ValueError(
                    f"credits.grant_programs.{program_key} award references unknown bucket '{award.bucket}'"
                )
            if isinstance(award.expiry, SubscriptionEndExpiry):
                raise ValueError(f"credits.grant_programs.{program_key} cannot use subscription_end expiry")
    return credits
