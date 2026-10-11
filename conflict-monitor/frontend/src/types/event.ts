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
  /**
   * People the source message stated were killed, copied out of its text by
   * the classifier rather than graded. null means the message gave no count -
   * the normal case, and NOT zero. 0 means it said nobody was killed. Render
   * the absence by showing nothing; never print 0 for null.
   */
  killed_reported?: number | null;
  lat: number | null;
  lon: number | null;
  timestamp: string;
  created_at: string;
  report_count?: number;
  reporting_channels?: string;
  /**
   * Who said it, counted from the report rows (C84). Every route and every
   * WebSocket push carries it; null or absent means a payload that did not
   * read the rows, and lib/corroboration.ts then falls back to report_count.
   */
  corroboration?: Corroboration | null;

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
   * "llm_failed" / "api_<code>" / "no_backend" / "bad_backend" /
   * "ollama_unreachable" / "ollama_timeout" / "ollama_model_missing" /
   * "ollama_http_<code>" when it did NOT and the row is a regex-only fallback
   * (which stamps event_type=military and leaves severity null). null when the
   * field was never written - unknown, not a failure.
   */
  extraction_status?: string | null;
  /**
   * Which model classified this row ("qwen3:8b", "claude-haiku-4-5-20251001").
   * null on rows written before the column existed, and on fallbacks that
   * never reached a model.
   */
  extraction_model?: string | null;
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

/**
 * Who said it, counted once per outlet (backend services/corroboration.py,
 * the one rule). Every count is a floor bounded by the rows the table holds.
 */
export interface Corroboration {
  /** Rows under the event in event_reports. */
  reports: number;
  /** Distinct non-empty channel names among them. */
  channels: number;
  /**
   * Distinct outlets once a Telegram forward collapses onto the channel it
   * was forwarded from and `x_name` onto `name` (one outlet, two transports).
   * A pasted repost carries no forward header and still counts as one.
   */
  outlets: number;
  /** Reports the merge destroyed before the report table existed; 0 since. */
  unrecorded: number;
  /**
   * Other events the link rule reads as this incident (backend
   * services/linkrule.py): separate rows with their own text and counts,
   * never merged, at the rule's measured precision. Absent on a payload
   * from before the rule existed; read that as 0.
   */
  linked?: number;
}

/** One event linked to another, from GET /events/{id}/links. */
export interface EventLinkRow {
  event: ConflictEvent;
  rule: string;
  /** Containment over the two cleaned texts, the number the bar was applied to. */
  score: number;
  /** "same" | "unknown": the stored place names. "different" never links. */
  place: string;
  /** "same" | "unknown": the towns the two texts name. */
  towns: string;
  /** Minutes from the event asked about to this one; negative when it came first. */
  dt_min: number;
  km: number | null;
  created_at: string;
}

/** One report under an event, from GET /events/{id}/reports. */
export interface EventReportRow {
  id: number;
  source: string;
  channel: string;
  /** The outlet whose words these are: the key the chip counts by. */
  outlet: string;
  /**
   * null: this row never recorded Telegram's forward header (RSS, the
   * archive, demo). "": not a forward. Otherwise the origin's name; "user"
   * for a forward from a person, who is not named; "?" when the header
   * named nothing.
   */
  forwarded_from: string | null;
  raw_text: string;
  summary: string;
  source_url: string;
  telegram_message_id: number | null;
  /** This report's own stated toll, beside the text that stated it. */
  killed_reported: number | null;
  extraction_status: string | null;
  reported_at: string;
  ingested_at: string;
}

export interface EventWSMessage {
  type: "new_event";
  event: ConflictEvent;
}
