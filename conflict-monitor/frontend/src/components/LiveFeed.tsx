/**
 * LiveFeed.
 *
 * Identity is shape + colour from lib/tokens.ts, never colour alone, and any
 * type outside the three validated hues folds to "Other" instead of inventing a
 * fourth hue. Severity is a small numeral: in the archive it is 86.3% constant,
 * so it must not outweigh identity.
 *
 * Provenance the UI used to discard is surfaced here (ledger finding C47). A
 * row whose classification FAILED must not look identical to one that
 * succeeded - the fallback path stamps event_type=military / severity=5 on
 * every failure, so an unmarked failed row reads as a confident military call.
 */
import { MapPin, MapPinOff } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { EVENT_TYPES, STATUS, eventVisual, formatAge } from "../lib/tokens";
import type { ConflictEvent } from "../types/event";

function timeAgo(ts: string): string {
  const seconds = (Date.now() - new Date(ts).getTime()) / 1000;
  return formatAge(seconds);
}

function formatTimestamp(ts: string): string {
  const d = new Date(ts);
  return d.toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

/** null = never recorded (unknown), not a failure. */
function extractionFailed(evt: ConflictEvent): boolean {
  return evt.extraction_status != null && evt.extraction_status !== "ok";
}

function geoMissing(evt: ConflictEvent): boolean {
  return evt.is_geolocated === false || evt.lat == null || evt.lon == null;
}

// Tiny blip sound via Web Audio API
function playBlip() {
  try {
    const ctx = new AudioContext();
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    osc.connect(gain);
    gain.connect(ctx.destination);
    osc.type = "sine";
    osc.frequency.setValueAtTime(880, ctx.currentTime);
    osc.frequency.exponentialRampToValueAtTime(440, ctx.currentTime + 0.08);
    gain.gain.setValueAtTime(0.08, ctx.currentTime);
    gain.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + 0.12);
    osc.start();
    osc.stop(ctx.currentTime + 0.12);
    setTimeout(() => ctx.close(), 200);
  } catch {
    // Audio not available
  }
}

interface LiveFeedProps {
  events: ConflictEvent[];
}

export function LiveFeed({ events }: LiveFeedProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const [autoScroll, setAutoScroll] = useState(true);
  const [newIds, setNewIds] = useState<Set<number>>(new Set());
  const prevCountRef = useRef(0);
  const [soundEnabled, setSoundEnabled] = useState(false);

  // Track newly arrived events for the "NEW" flash
  useEffect(() => {
    if (events.length > prevCountRef.current && prevCountRef.current > 0) {
      const incoming = new Set(
        events.slice(0, events.length - prevCountRef.current).map((e) => e.id),
      );
      setNewIds(incoming);
      if (soundEnabled) playBlip();
      const timer = setTimeout(() => setNewIds(new Set()), 3000);
      return () => clearTimeout(timer);
    }
    prevCountRef.current = events.length;
  }, [events, soundEnabled]);

  useEffect(() => {
    prevCountRef.current = events.length;
  }, [events.length]);

  // Auto-scroll to top on new events
  useEffect(() => {
    if (autoScroll && containerRef.current) {
      containerRef.current.scrollTop = 0;
    }
  }, [events.length, autoScroll]);

  const handleScroll = useCallback(() => {
    if (!containerRef.current) return;
    setAutoScroll(containerRef.current.scrollTop < 10);
  }, []);

  const lastUpdated = events.length > 0 ? timeAgo(events[0].timestamp) : "—";

  return (
    <div className="panel relative flex h-full flex-col">
      {/* Scan-line overlay (switched off under prefers-reduced-motion) */}
      <div className="pointer-events-none absolute inset-0 z-[2] overflow-hidden">
        <div className="scan-line" />
      </div>

      {/* Header bar */}
      <div className="flex items-center justify-between border-b border-[var(--border)] px-4 py-2.5">
        <span className="text-[11px] font-semibold tracking-[0.18em] text-[var(--text-primary)]">
          LIVE FEED
        </span>
        <div className="flex items-center gap-3">
          <button
            onClick={() => setSoundEnabled((p) => !p)}
            title={soundEnabled ? "Mute alerts" : "Enable alert sounds"}
            className="rounded-[3px] border px-1.5 py-[1px] text-[9px] font-semibold tracking-[0.1em]"
            style={{
              borderColor: soundEnabled ? "var(--text-secondary)" : "var(--border)",
              color: soundEnabled ? "var(--text-primary)" : "var(--text-muted)",
            }}
          >
            {soundEnabled ? "SND ON" : "SND OFF"}
          </button>
          {/* Both numbers carry their own unit: an unlabelled "4s  51" makes
              the reader guess which is age and which is a count. */}
          <span
            className="flex items-baseline gap-1"
            title="Age of the newest event in this window"
          >
            <span className="text-[9px] tracking-[0.1em] text-[var(--text-muted)]">NEWEST</span>
            <span
              className="text-[10px] tabular-nums text-[var(--text-secondary)]"
              style={{ fontFamily: "var(--font-mono)" }}
            >
              {lastUpdated}
            </span>
          </span>
          <span className="flex items-baseline gap-1" title="Events in the active window">
            <span
              className="text-[10px] tabular-nums text-[var(--text-secondary)]"
              style={{ fontFamily: "var(--font-mono)" }}
            >
              {events.length}
            </span>
            <span className="text-[9px] tracking-[0.1em] text-[var(--text-muted)]">IN WINDOW</span>
          </span>
        </div>
      </div>

      {/* Legend - identity is never colour alone */}
      <div className="flex items-center gap-3 border-b border-[var(--border)] px-4 py-[5px]">
        {EVENT_TYPES.map(({ key, label, color, Icon }) => (
          <span key={key} className="flex items-center gap-1">
            <Icon size={13} strokeWidth={2.25} style={{ color }} aria-hidden="true" />
            <span className="text-[9px] tracking-[0.08em] text-[var(--text-muted)]">
              {label.toUpperCase()}
            </span>
          </span>
        ))}
      </div>

      {/* Event list */}
      <div
        ref={containerRef}
        onScroll={handleScroll}
        className="relative z-[1] flex-1 overflow-y-auto py-1"
      >
        {events.length === 0 && (
          <div
            className="p-5 text-center text-[11px] tracking-[0.1em] text-[var(--text-muted)]"
            style={{ fontFamily: "var(--font-mono)" }}
          >
            AWAITING EVENTS...
          </div>
        )}
        {events.map((evt, idx) => {
          const isNew = newIds.has(evt.id);
          const visual = eventVisual(evt.event_type);
          const TypeIcon = visual.Icon;
          const failed = extractionFailed(evt);
          const noGeo = geoMissing(evt);
          const place = (evt.location_name ?? "").trim();
          const sources = evt.report_count ?? 1;

          return (
            <div
              key={evt.id}
              className="mb-px bg-[var(--bg-card)] py-[9px] pl-3 pr-3.5"
              style={{
                borderLeft: `2px solid ${visual.color}`,
                // Neutral lift, not the MILITARY blue: "new" is not a category,
                // so it must not borrow a series hue.
                background: isNew ? "rgba(142, 163, 187, 0.08)" : "var(--bg-card)",
                // Longhands only. Mixing the `animation` shorthand with
                // animationDelay/animationFillMode makes React warn on every
                // rerender ("Updating a style property during rerender...").
                animationName: isNew ? "slideIn" : undefined,
                animationDuration: isNew ? "0.4s" : undefined,
                animationTimingFunction: isNew ? "ease" : undefined,
                animationDelay: isNew ? `${idx * 30}ms` : undefined,
                animationFillMode: isNew ? "backwards" : undefined,
              }}
            >
              {/* Identity line */}
              <div className="flex items-start justify-between gap-2">
                <div className="flex min-w-0 items-center gap-1">
                  {/* The shape channel has to survive at true 1x: at 12px the
                      handshake was an orange smudge and identity collapsed back
                      onto colour alone. */}
                  <TypeIcon
                    size={14}
                    strokeWidth={2.25}
                    style={{ color: visual.color }}
                    className="shrink-0"
                    aria-hidden="true"
                  />
                  <span className="shrink-0 text-[9px] font-semibold tracking-[0.1em] text-[var(--text-secondary)]">
                    {visual.label.toUpperCase()}
                  </span>
                  {isNew && (
                    <span
                      className="shrink-0 rounded-[2px] px-1 text-[8px] font-bold tracking-[0.08em]"
                      style={{ background: "var(--text-secondary)", color: "#0a0e14" }}
                    >
                      NEW
                    </span>
                  )}
                  <span
                    className="truncate text-[9.5px] tabular-nums text-[var(--text-muted)]"
                    style={{ fontFamily: "var(--font-mono)" }}
                  >
                    {formatTimestamp(evt.timestamp)} ({timeAgo(evt.timestamp)}) &middot;{" "}
                    {evt.channel_name}
                  </span>
                </div>
                <span
                  className="shrink-0 text-[9.5px] tabular-nums text-[var(--text-muted)]"
                  style={{ fontFamily: "var(--font-mono)" }}
                  title="Severity (1-10). Near-constant in the archive - low information."
                >
                  SEV {evt.severity}
                </span>
              </div>

              {/* Summary */}
              <div className="mt-1 text-[12px] leading-[1.45] text-[var(--text-primary)]">
                {evt.summary}
              </div>

              {/* Provenance */}
              <div className="mt-[5px] flex flex-wrap items-center gap-x-2.5 gap-y-1">
                {place && (
                  <span className="flex items-center gap-1 text-[9.5px] text-[var(--text-secondary)]">
                    <MapPin size={10} strokeWidth={2} aria-hidden="true" />
                    {place}
                  </span>
                )}
                {sources > 1 && (
                  <span
                    className="text-[9.5px] tabular-nums text-[var(--text-secondary)]"
                    style={{ fontFamily: "var(--font-mono)" }}
                  >
                    {sources} sources
                  </span>
                )}
                {evt.source_reliability != null && (
                  <span
                    className="text-[9.5px] tabular-nums text-[var(--text-secondary)]"
                    style={{ fontFamily: "var(--font-mono)" }}
                    title="Source reliability score"
                  >
                    REL {evt.source_reliability}
                  </span>
                )}
                {/* Hue rides the border and the glyph; the words stay in ink.
                    Otherwise these chips put #c98500 and #d95926 on text right
                    beside an event whose identity hue is also #d95926. */}
                {noGeo && (
                  <span
                    className="flex items-center gap-1 rounded-[2px] px-1 text-[9px] font-semibold tracking-[0.06em] text-[var(--text-primary)]"
                    style={{ border: `1px solid ${STATUS.warning.color}` }}
                    title="No resolved coordinates - this event is not on the map."
                  >
                    <MapPinOff
                      size={10}
                      strokeWidth={2.5}
                      style={{ color: STATUS.warning.color }}
                      aria-hidden="true"
                    />
                    NOT GEOLOCATED
                  </span>
                )}
                {failed && (
                  <span
                    className="flex items-center gap-1 rounded-[2px] px-1 text-[9px] font-semibold tracking-[0.06em] text-[var(--text-primary)]"
                    style={{ border: `1px solid ${STATUS.serious.color}` }}
                    title="Classification failed; type and severity are fallback defaults, not findings."
                  >
                    <STATUS.serious.Icon
                      size={10}
                      strokeWidth={2.5}
                      style={{ color: STATUS.serious.color }}
                      aria-hidden="true"
                    />
                    CLASSIFY FAILED: {evt.extraction_status}
                  </span>
                )}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
