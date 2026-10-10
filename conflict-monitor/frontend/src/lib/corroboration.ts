/**
 * The corroboration chip and the report list under an event (C84).
 *
 * The unit is named every time: "3 REPORTS · 2 OUTLETS", never "3 sources".
 * `reports` is rows kept with their own text; `outlets` is who said it once a
 * Telegram forward collapses onto its origin and `x_name` onto `name` (one
 * outlet, two transports). Both come from the backend's one rule
 * (services/corroboration.py) and both are floors: a pasted repost carries no
 * forward header and still counts as an outlet. `unrecorded` is the archive's
 * destroyed reports, shown as what they are.
 */
import type { ConflictEvent, Corroboration, EventReportRow } from "../types/event";

export function plural(n: number, word: string): string {
  return `${n} ${word}${n === 1 ? "" : "S"}`;
}

/** The chip text, or null when there is nothing to say beyond "one report". */
export function corroborationChip(evt: ConflictEvent): string | null {
  const c = evt.corroboration;
  if (!c) {
    // A payload without the rows: the stored count is right, the outlet
    // breakdown is unknown, so the chip claims nothing about outlets.
    const n = evt.report_count ?? 1;
    return n > 1 ? plural(n, "REPORT") : null;
  }
  if (c.reports <= 1 && c.unrecorded === 0) return null;
  const parts = [plural(c.reports, "REPORT"), plural(c.outlets, "OUTLET")];
  if (c.unrecorded > 0) parts.push(`+${c.unrecorded} UNRECORDED`);
  return parts.join(" · ");
}

export const CHIP_TITLE =
  "Reports are rows kept with their own text. Outlets are who said it: a forward counts as its origin's, " +
  "and one outlet on two transports counts once. A pasted repost still counts, so outlets is a floor. " +
  "Unrecorded reports were merged before their text was kept. Click to read the reports.";

/** How a report row introduces itself: the channel, and the origin when it is a forward. */
export function reportByline(r: EventReportRow): string {
  const channel = r.channel || "unnamed channel";
  if (!r.forwarded_from) return channel;
  if (r.forwarded_from === "user") return `${channel} · FWD FROM a person`;
  if (r.forwarded_from === "?") return `${channel} · FWD FROM an unnamed origin`;
  return `${channel} · FWD FROM ${r.forwarded_from}`;
}

/** The line under the list when the archive destroyed reports; null when it did not. */
export function unrecordedNote(c: Corroboration | null | undefined): string | null {
  if (!c || c.unrecorded === 0) return null;
  return `+${plural(c.unrecorded, "REPORT")} merged before their text was kept. Not recoverable.`;
}
