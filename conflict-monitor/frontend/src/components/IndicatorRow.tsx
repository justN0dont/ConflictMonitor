/**
 * IndicatorRow - the one row every indicator section is built from.
 *
 * Extracted verbatim from IndicatorRail so the CONNECTIVITY section speaks the
 * same states in the same shape. A second row component would let two sections
 * quietly disagree about what DEGRADED means, and the whole point of these
 * states is that "we cannot see" must never render as a calm reading.
 *
 * The optional `spark` slot is a 24h trace drawn beside the value. IndicatorRail
 * passes none, so its rows are byte-for-byte what they were.
 */
import type { LucideIcon } from "lucide-react";
import type { ReactNode } from "react";
import { STATUS, type StatusKey, formatAge } from "../lib/tokens";

export type RailState =
  | "NOT-OBSERVED"
  | "WATCH"
  | "PARTIAL"
  | "TRIPPED"
  | "STALE"
  | "DEGRADED";

/**
 * PARTIAL shares amber with STALE rather than taking a sixth hue.
 *
 * It means "two independent sensors agree that this is depressed, which is not
 * enough to call it" - so it must not wear TRIPPED's red, and it must not wear
 * the nominal state's nothing. Amber is the palette's "not confirmed, look at
 * me" and STALE means the same kind of thing; the glyph (CircleAlert vs the
 * WARNING triangle and the CRITICAL octagon) and the state word keep them apart.
 */
export const STATE_STATUS: Record<RailState, StatusKey> = {
  "NOT-OBSERVED": "unknown",
  WATCH: "good",
  PARTIAL: "warning",
  TRIPPED: "critical",
  STALE: "warning",
  DEGRADED: "serious",
};

/**
 * WATCH is the nominal state and carries no hue at all.
 *
 * Two reasons. (1) status-good is #199e70, which is also the ECONOMIC series
 * hue: a green WATCH chip means the same green says "economic" beside an event
 * and "healthy" 300px below it. (2) Five rows of identical green ticks is a
 * wall, not a scan - nothing stands out because everything is lit. Colour is
 * reserved for the exception, so a TRIPPED, PARTIAL, STALE or DEGRADED row is
 * the only thing burning in the rail.
 *
 * And per the palette rules, the hue only ever lands on the MARK (the glyph and
 * the row's left edge). Text always wears a text token.
 */
export const NOMINAL: RailState = "WATCH";

export interface Row {
  name: string;
  value: string;
  state: RailState;
  note: string;
  age: number | null;
  detail?: string;
}

export function IndicatorRow({
  row,
  tall,
  spark,
}: {
  row: Row;
  tall?: boolean;
  /** Optional 24h trace. Occupies its own fixed column so values stay aligned. */
  spark?: ReactNode;
}) {
  const status = STATUS[STATE_STATUS[row.state]];
  const Icon: LucideIcon = status.Icon;
  const nominal = row.state === NOMINAL;
  // Hue lands on the mark and the row's left edge only, and only when the row
  // is an exception. Never on text: text wears text tokens.
  const mark = nominal ? "var(--text-muted)" : status.color;
  // With a trace in the middle, the value column is FIXED: an auto column sizes
  // to each row's own string, so a row reading "0%" would shove its sparkline
  // 18px right of the row above it. Without a trace there is no middle column
  // to shift, so IndicatorRail keeps the auto column it always had.
  const cols = spark
    ? "grid-cols-[14px_minmax(0,1fr)_64px_56px]"
    : "grid-cols-[14px_minmax(0,1fr)_auto]";
  return (
    <div
      className={`grid ${cols} items-start gap-x-2 border-t border-[var(--border)] pr-3 pl-[10px] ${
        tall ? "py-2" : "py-[7px]"
      }`}
      style={{ borderLeft: `2px solid ${nominal ? "transparent" : status.color}` }}
    >
      <Icon
        size={13}
        strokeWidth={nominal ? 1.75 : 2.25}
        style={{ color: mark }}
        className="mt-[2px]"
        aria-hidden="true"
      />
      <div className="min-w-0">
        <div className="truncate text-[11px] font-semibold tracking-[0.09em] text-[var(--text-primary)]">
          {row.name}
        </div>
        <div className="mt-[2px] flex min-w-0 items-baseline gap-1.5">
          <span
            className={`shrink-0 text-[9px] font-bold tracking-[0.08em] ${
              nominal ? "text-[var(--text-muted)]" : "text-[var(--text-primary)]"
            }`}
          >
            {row.state}
          </span>
          <span className="min-w-0 text-[10px] leading-[1.35] text-[var(--text-muted)]">
            {row.note}
          </span>
        </div>
        {row.detail && (
          <div
            className="mt-[3px] truncate text-[9.5px] tabular-nums tracking-[0.02em] text-[var(--text-muted)]"
            style={{ fontFamily: "var(--font-mono)" }}
          >
            {row.detail}
          </div>
        )}
      </div>
      {spark && <div className="mt-[3px] flex justify-end">{spark}</div>}
      <div className="text-right">
        <div className="text-[15px] font-semibold leading-[1.2] tabular-nums text-[var(--text-primary)]">
          {row.value}
        </div>
        <div
          className="mt-[3px] text-[10px] tabular-nums text-[var(--text-secondary)]"
          style={{ fontFamily: "var(--font-mono)" }}
          title="Age of the data behind this row"
        >
          {formatAge(row.age)}
        </div>
      </div>
    </div>
  );
}
