/**
 * The corroboration chip and the report list under an event (C84).
 *
 * The unit is named every time: "3 REPORTS · 2 OUTLETS", never "3 sources".
 * `reports` is rows kept with their own text; `outlets` is who said it once a
 * Telegram forward collapses onto its origin and `x_name` onto `name` (one
 * outlet, two transports). Both come from the backend's one rule
 * (services/corroboration.py) and both are floors: a pasted repost carries no
 * forward header and still counts as an outlet. `unrecorded` is the archive's
 * destroyed reports, shown as what they are. `linked` is other events the
 * link rule reads as this incident: rows kept apart, never merged, at the
 * rule's measured precision, which the title states.
 */
import type { ConflictEvent, Corroboration, EventLinkRow, EventReportRow } from "../types/event";

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
  const linked = c.linked ?? 0;
  if (c.reports <= 1 && c.unrecorded === 0 && linked === 0) return null;
  const parts = [plural(c.reports, "REPORT"), plural(c.outlets, "OUTLET")];
  if (c.unrecorded > 0) parts.push(`+${c.unrecorded} UNRECORDED`);
  if (linked > 0) parts.push(`${linked} LINKED`);
  return parts.join(" · ");
}

export const CHIP_TITLE =
  "Reports are rows kept with their own text. Outlets are who said it: a forward counts as its origin's, " +
  "and one outlet on two transports counts once. A pasted repost still counts, so outlets is a floor. " +
  "Unrecorded reports were merged before their text was kept. Linked are other events the link rule reads as this " +
  "incident: separate rows, never merged, at precision 0.86 and recall 0.66 on the gold pairs against one labeller. " +
  "Click to read the reports and the linked events.";

export const LINK_TITLE =
  "Separate events the link rule reads as this incident: their texts overlap by more than 0.4 (containment, tags " +
  "and URLs stripped) and neither the stored places nor the towns the texts name disagree. Measured on the gold " +
  "pairs at precision 0.86, recall 0.66, against one labeller and reviewed by nobody. Nothing was merged.";

/** What the rule saw, in words: the score, the two place checks, the gap. */
export function linkLine(l: EventLinkRow): string {
  const place = l.place === "same" ? "PLACES AGREE" : "A PLACE UNNAMED";
  const towns = l.towns === "same" ? "SAME TOWN" : "TOWN UNREAD";
  const m = Math.round(Math.abs(l.dt_min));
  const gap = m === 0 ? "SAME MINUTE" : `${m} MIN ${l.dt_min > 0 ? "AFTER" : "BEFORE"}`;
  const parts = [`LINK ${l.score.toFixed(2)}`, place, towns, gap];
  if (l.km != null) parts.push(`${Math.round(l.km)} KM`);
  return parts.join(" · ");
}

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
