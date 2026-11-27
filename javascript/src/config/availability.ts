import type { Availability } from "./types.js";

/** Evaluate catalog availability against a trusted region and current time. */
export function offerIsAvailable(availability: Availability | undefined, region?: string): boolean {
  if (!availability) return true;
  const now = Date.now();
  return !(
    (availability.startsAt && Date.parse(availability.startsAt) > now) ||
    (availability.endsAt && Date.parse(availability.endsAt) <= now) ||
    (availability.regions.length > 0 && (!region || !availability.regions.includes(region)))
  );
}
