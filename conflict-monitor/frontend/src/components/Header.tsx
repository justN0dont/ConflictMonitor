/**
 * Header.
 *
 * Liveness is rendered as AGE, never as a green dot. A dot asserts health it
 * cannot know: it stays green while a feed quietly stops. An age is the honest
 * signal - it climbs on its own the moment we stop hearing anything. The status
 * palette only enters once an age crosses that feed's poll interval.
 */
import { Plane, Radio, Satellite, Ship, FlaskConical } from "lucide-react";
import { useEffect, useState } from "react";
import type { Aircraft, JammingStatus, TLERecord, Vessel } from "../hooks/useTracking";
import { STATUS, formatAge } from "../lib/tokens";
import { STALE_AFTER, useFeedAges } from "../lib/useFeedAges";
import type { ConflictEvent } from "../types/event";

interface HeaderProps {
  /** Events inside the active time window. */
  events: ConflictEvent[];
  /** The whole retained stream, for stream currency. */
  allEvents: ConflictEvent[];
  aircraft: Aircraft[];
  vessels: Vessel[];
  tleData: TLERecord[];
  jammingStatus: JammingStatus;
  isConnected: boolean;
  demoMode: boolean;
}

function formatUTCTime(): string {
  const now = new Date();
  const months = [
    "JAN", "FEB", "MAR", "APR", "MAY", "JUN",
    "JUL", "AUG", "SEP", "OCT", "NOV", "DEC",
  ];
  const day = String(now.getUTCDate()).padStart(2, "0");
  const month = months[now.getUTCMonth()];
  const year = now.getUTCFullYear();
  const hours = String(now.getUTCHours()).padStart(2, "0");
  const minutes = String(now.getUTCMinutes()).padStart(2, "0");
  const seconds = String(now.getUTCSeconds()).padStart(2, "0");
  return `${day} ${month} ${year} | ${hours}:${minutes}:${seconds} UTC`;
}

function Count({
  Icon,
  value,
  unit,
}: {
  Icon: typeof Plane;
  value: number;
  unit: string;
}) {
  return (
    <span className="flex items-baseline gap-1.5" title={`${value} ${unit}`}>
      <Icon
        size={12}
        strokeWidth={2}
        className="translate-y-[1px] text-[var(--text-muted)]"
        aria-hidden="true"
      />
      <span className="text-[13px] font-semibold tabular-nums text-[var(--text-primary)]">
        {value}
      </span>
      <span className="text-[9px] tracking-[0.12em] text-[var(--text-muted)]">{unit}</span>
    </span>
  );
}

/**
 * One feed's age.
 *
 * The numeral always wears a text token - a series hue on a number would mean
 * status-good (#199e70) and the ECONOMIC hue are the same green in one frame.
 * When a feed crosses its poll interval a status-coloured MARK appears beside
 * it; a healthy feed gets no mark and no hue at all, so the only lit thing in
 * the header is the one that needs reading.
 */
function FeedAge({
  label,
  age,
  staleAfter,
}: {
  label: string;
  age: number | null;
  staleAfter: number;
}) {
  const status =
    age == null ? STATUS.unknown : age > staleAfter ? STATUS.warning : null;
  const StatusIcon = status?.Icon;
  return (
    <span className="flex items-baseline gap-1" title={`${label}: last payload received`}>
      {StatusIcon && (
        <StatusIcon
          size={11}
          strokeWidth={2.25}
          style={{ color: status.color }}
          className="translate-y-[1.5px]"
          aria-hidden="true"
        />
      )}
      <span className="text-[9px] tracking-[0.1em] text-[var(--text-muted)]">{label}</span>
      <span
        className="text-[11px] tabular-nums"
        style={{
          fontFamily: "var(--font-mono)",
          color: status ? "var(--text-primary)" : "var(--text-secondary)",
        }}
      >
        {formatAge(age)}
      </span>
    </span>
  );
}

export function Header({
  events,
  allEvents,
  aircraft,
  vessels,
  tleData,
  jammingStatus,
  isConnected,
  demoMode,
}: HeaderProps) {
  const [utcTime, setUtcTime] = useState(formatUTCTime);
  const ages = useFeedAges({
    aircraft,
    vessels,
    tleData,
    jammingStatus,
    latestEventTs: allEvents[0]?.timestamp ?? null,
  });

  useEffect(() => {
    const interval = setInterval(() => setUtcTime(formatUTCTime()), 1000);
    return () => clearInterval(interval);
  }, []);

  // null = nominal, and nominal gets no mark. A green tick beside the age is a
  // green dot with a tick drawn in it: it asserts health that the age already
  // reports honestly, and it spends the ECONOMIC hue on a non-category.
  const stream = !isConnected
    ? STATUS.critical
    : ages.stream == null
      ? STATUS.unknown
      : ages.stream > STALE_AFTER.stream
        ? STATUS.warning
        : null;

  return (
    <header
      className="panel flex items-center justify-between gap-4 border-b border-[var(--border)] px-5"
      style={{ gridArea: "header" }}
    >
      {/* Identity */}
      <div className="flex min-w-0 items-baseline gap-3">
        <h1 className="whitespace-nowrap text-[15px] font-bold tracking-[0.26em] text-[var(--text-primary)]">
          CONFLICT MONITOR
        </h1>
        <span className="hidden whitespace-nowrap text-[10px] tracking-[0.16em] text-[var(--text-muted)] xl:inline">
          REAL-TIME OSINT INTELLIGENCE
        </span>
        {demoMode && (
          <span
            className="flex shrink-0 translate-y-[1px] items-center gap-1.5 rounded-[3px] px-2.5 py-[3px] text-[11px] font-bold tracking-[0.12em]"
            style={{ background: STATUS.warning.color, color: "#0a0e14" }}
            title="Demo mode: every event, track and position on this screen is fabricated."
          >
            <FlaskConical size={12} strokeWidth={2.5} aria-hidden="true" />
            DEMO DATA &mdash; NOT LIVE
          </span>
        )}
      </div>

      {/* UTC clock */}
      <div
        className="shrink-0 text-[13px] tabular-nums tracking-[0.06em] text-[var(--text-secondary)]"
        style={{ fontFamily: "var(--font-mono)" }}
      >
        {utcTime}
      </div>

      {/* Counts, feed ages, stream state */}
      <div className="flex shrink-0 items-center gap-4">
        <div className="flex items-baseline gap-3.5">
          <Count Icon={Radio} value={events.length} unit="EVT" />
          <Count Icon={Plane} value={aircraft.filter((a) => !a.on_ground).length} unit="AC" />
          <Count Icon={Ship} value={vessels.length} unit="VES" />
          <Count Icon={Satellite} value={tleData.length} unit="SAT" />
        </div>

        <span className="h-4 w-px bg-[var(--border)]" aria-hidden="true" />

        <div className="flex items-baseline gap-3">
          <FeedAge label="ADS-B" age={ages.adsb} staleAfter={STALE_AFTER.adsb} />
          <FeedAge label="AIS" age={ages.ais} staleAfter={STALE_AFTER.ais} />
          <FeedAge label="GNSS" age={ages.gnss} staleAfter={STALE_AFTER.gnss} />
        </div>

        <span className="h-4 w-px bg-[var(--border)]" aria-hidden="true" />

        <span className="flex items-center gap-1.5" title="Event stream">
          {stream && (
            <stream.Icon
              size={13}
              strokeWidth={2.25}
              style={{ color: stream.color }}
              aria-hidden="true"
            />
          )}
          <span className="text-[9px] tracking-[0.1em] text-[var(--text-muted)]">STREAM</span>
          <span
            className="text-[11px] font-semibold tabular-nums"
            style={{
              fontFamily: "var(--font-mono)",
              color: stream ? "var(--text-primary)" : "var(--text-secondary)",
            }}
          >
            {isConnected ? formatAge(ages.stream) : "OFFLINE"}
          </span>
        </span>
      </div>
    </header>
  );
}
