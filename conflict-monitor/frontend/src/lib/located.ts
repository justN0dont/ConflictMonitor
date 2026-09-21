import type { ConflictEvent } from "../types/event";

/**
 * One definition of "located", for every renderer that draws a mark.
 *
 * MapPanel filtered on lat/lon AND is_geolocated; GlobeView filtered on lat/lon
 * alone. Nothing in the held window separates them - all 96 located rows carry
 * is_geolocated true, so zero rows disagree - and that is precisely why the
 * divergence survived review: it is latent, not absent. It goes live the first
 * time the geocoder writes a coordinate it distrusts, and from that moment the
 * globe draws a mark the 2D map refuses to draw, from the same query, in the
 * same second. A predicate that is only correct while the data happens to be
 * uniform is not correct; it is untested.
 *
 * is_geolocated is the row's own statement about whether it was ever placed, so
 * it decides. A coordinate can exist and still not be a location: every event
 * written before the sentinel was retired carries (-25.0, 80.0) - open ocean
 * south-west of Australia - with is_geolocated false. undefined and null mean
 * the column was never written, which is not a denial, so those rows stay
 * located.
 */
export function isLocated(evt: ConflictEvent): boolean {
  return evt.lat != null && evt.lon != null && evt.is_geolocated !== false;
}
