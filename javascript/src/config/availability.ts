import type { Availability } from "./types.js";

/** Evaluate catalog availability against a trusted region and current time. */
export function offerIsAvailable(
  availability: Availability | undefined,
  region?: string,
  now: Date = new Date(),
): boolean {
  if (!availability) return true;
  const timestamp = now.getTime();
  return !(
    (availability.startsAt && Date.parse(availability.startsAt) > timestamp) ||
    (availability.endsAt && Date.parse(availability.endsAt) <= timestamp) ||
    (availability.regions.length > 0 && (!region || !availability.regions.includes(region)))
  );
}
