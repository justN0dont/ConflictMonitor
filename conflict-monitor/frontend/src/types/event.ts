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
  severity: number;
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
   * fallback (which stamps event_type=military, severity=5 regardless). null
   * when the field was never written - unknown, not a failure.
   */
  extraction_status?: string | null;
  /** false when lat/lon are a guess rather than a resolved location. */
  is_geolocated?: boolean | null;
}

export interface EventWSMessage {
  type: "new_event";
  event: ConflictEvent;
}
