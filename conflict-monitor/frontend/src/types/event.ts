export interface ConflictEvent {
  id: number;
  source: string;
  channel_name: string;
  raw_text: string;
  summary: string;
  /**
   * The classifier emits military | diplomatic | economic | humanitarian | cyber,
   * and the fallback path can emit others, so this is a string rather than a
   * closed union. lib/tokens.ts folds anything outside the three validated hues
   * into "Other".
   */
  event_type: string;
  /**
   * 1-10, or null when severity was never measured. The classifier's failure
   * fallback used to write 5 here; it now writes nothing at all, so null means
   * "unknown", not "middling". Render it as a dash - never as 5, never as 0.
   */
  severity: number | null;
  lat: number | null;
  lon: number | null;
  timestamp: string;
  created_at: string;
  report_count?: number;
  reporting_channels?: string;

  /**
   * Provenance. The API has always returned these (backend schemas.py
   * EventOut); the UI used to drop them on the floor. Ledger finding C47.
   */
  /** Place the classifier named. Empty string when it named none. */
  location_name?: string;
  /** Channel reliability score, or null when unscored. */
  source_reliability?: number | null;
  telegram_message_id?: number | null;
  /** Empty string when the source carried no link. */
  source_url?: string;
  /**
   * "ok" when classification succeeded; "rate_limited" / "parse_failed" /
   * "llm_failed" / "api_<code>" when it did NOT and the row is a regex-only
   * fallback (which stamps event_type=military and leaves severity null). null
   * when the field was never written - unknown, not a failure.
   */
  extraction_status?: string | null;
  /** false when lat/lon are a guess rather than a resolved location. */
  is_geolocated?: boolean | null;

  /**
   * Geocoder provenance. Together these say how big the coordinate's claim
   * actually is, which is the difference between a named facility and a
   * country centroid that the old schema could not express.
   */
  /**
   * How precise the coordinate is: "facility" | "city" | "admin1" |
   * "region_named" | "country_centroid". null when it was never measured.
   * lib/tokens.ts GEO_PRECISION turns this into mark geometry.
   */
  geo_precision?: string | null;
  /** Radius of the claim in metres. A country centroid can be over 1 000 000. */
  geo_uncertainty_m?: number | null;
  /**
   * How it was resolved: "table-exact" | "directional-exact" | "partial" |
   * "nominatim". null when it was never recorded.
   */
  geo_method?: string | null;
}

export interface EventWSMessage {
  type: "new_event";
  event: ConflictEvent;
}
