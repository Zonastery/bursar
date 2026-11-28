"""Catalog offer availability shared by commerce operations."""

from datetime import UTC, datetime

from bursar.config.types import Availability


def offer_is_available(
    availability: Availability | None, region: str | None = None, at: datetime | None = None
) -> bool:
    if availability is None:
        return True
    now = at if at is not None else datetime.now(UTC)
    return not (
        (availability.starts_at is not None and availability.starts_at > now)
        or (availability.ends_at is not None and availability.ends_at <= now)
        or (availability.regions and region not in availability.regions)
    )
