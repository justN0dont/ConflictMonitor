/**
 * C84: the chip read "{report_count} sources" with no unit and no evidence
 * under it. These pin what the chip says, what a report row's byline says,
 * and the note about reports the archive destroyed.
 */
import { describe, expect, it } from "vitest";
import type { ConflictEvent, Corroboration, EventLinkRow, EventReportRow } from "../types/event";
import { corroborationChip, linkLine, plural, reportByline, unrecordedNote } from "./corroboration";

const C = (reports: number, channels: number, outlets: number, unrecorded = 0, linked?: number): Corroboration => ({
  reports,
  channels,
  outlets,
  unrecorded,
  ...(linked === undefined ? {} : { linked }),
});

function evt(over: Partial<ConflictEvent> = {}): ConflictEvent {
  return {
    id: 1,
    source: "telegram",
    channel_name: "chan",
    raw_text: "",
    summary: "",
    event_type: "military",
    severity: null,
    lat: null,
    lon: null,
    timestamp: "2026-10-10T00:00:00Z",
    created_at: "2026-10-10T00:00:00Z",
    ...over,
  };
}

function row(over: Partial<EventReportRow> = {}): EventReportRow {
  return {
    id: 1,
    source: "telegram",
    channel: "reposter",
    outlet: "osintwarfare",
    forwarded_from: null,
    raw_text: "",
    summary: "",
    source_url: "",
    telegram_message_id: null,
    killed_reported: null,
    extraction_status: null,
    reported_at: "2026-10-10T00:00:00Z",
    ingested_at: "2026-10-10T00:00:00Z",
    ...over,
  };
}

describe("corroborationChip", () => {
  it("says nothing about a single report with nothing destroyed under it", () => {
    expect(corroborationChip(evt({ corroboration: C(1, 1, 1) }))).toBeNull();
  });

  it("names the unit: reports and outlets, never sources", () => {
    expect(corroborationChip(evt({ corroboration: C(3, 3, 2) }))).toBe("3 REPORTS · 2 OUTLETS");
  });

  it("shows one outlet repeating itself as exactly that", () => {
    // Three rows, three channel names (a post, its X mirror, a forward), one outlet.
    expect(corroborationChip(evt({ corroboration: C(3, 3, 1) }))).toBe("3 REPORTS · 1 OUTLET");
  });

  it("states what the archive destroyed", () => {
    expect(corroborationChip(evt({ corroboration: C(1, 1, 1, 213) }))).toBe("1 REPORT · 1 OUTLET · +213 UNRECORDED");
  });

  it("shows a count with no rows under it as unrecorded, never as a report", () => {
    expect(corroborationChip(evt({ corroboration: C(0, 0, 0, 1) }))).toBe("0 REPORTS · 0 OUTLETS · +1 UNRECORDED");
  });

  it("names a linked event beside one report, and after what the archive destroyed", () => {
    expect(corroborationChip(evt({ corroboration: C(1, 1, 1, 0, 1) }))).toBe("1 REPORT · 1 OUTLET · 1 LINKED");
    expect(corroborationChip(evt({ corroboration: C(2, 2, 2, 1, 3) }))).toBe(
      "2 REPORTS · 2 OUTLETS · +1 UNRECORDED · 3 LINKED",
    );
  });
  it("reads a payload from before the rule as zero links, never as unknown", () => {
    expect(corroborationChip(evt({ corroboration: C(1, 1, 1) }))).toBeNull();
  });
  it("falls back to the stored count when the payload carries no rows, and claims no outlets", () => {
    expect(corroborationChip(evt({ report_count: 4 }))).toBe("4 REPORTS");
    expect(corroborationChip(evt({ report_count: 1 }))).toBeNull();
    expect(corroborationChip(evt())).toBeNull();
  });
});

describe("reportByline", () => {
  it("names the origin of a forward", () => {
    expect(reportByline(row({ forwarded_from: "OSINTWarfare" }))).toBe("reposter · FWD FROM OSINTWarfare");
  });

  it("treats not-a-forward and never-recorded alike: the channel's own words", () => {
    expect(reportByline(row({ forwarded_from: "" }))).toBe("reposter");
    expect(reportByline(row({ forwarded_from: null }))).toBe("reposter");
  });

  it("does not name a person, and says when the header named nothing", () => {
    expect(reportByline(row({ forwarded_from: "user" }))).toBe("reposter · FWD FROM a person");
    expect(reportByline(row({ forwarded_from: "?" }))).toBe("reposter · FWD FROM an unnamed origin");
  });

  it("calls an unnamed channel that", () => {
    expect(reportByline(row({ channel: "" }))).toBe("unnamed channel");
  });
});

describe("unrecordedNote and plural", () => {
  it("is silent when nothing was destroyed", () => {
    expect(unrecordedNote(C(2, 2, 2))).toBeNull();
    expect(unrecordedNote(undefined)).toBeNull();
  });

  it("counts the destroyed reports", () => {
    expect(unrecordedNote(C(1, 1, 1, 213))).toBe("+213 REPORTS merged before their text was kept. Not recoverable.");
  });

  it("pluralises on exactly one", () => {
    expect(plural(1, "REPORT")).toBe("1 REPORT");
    expect(plural(0, "OUTLET")).toBe("0 OUTLETS");
  });
});

describe("linkLine", () => {
  const link = (over: Partial<EventLinkRow> = {}): EventLinkRow => ({
    event: evt({ id: 2 }),
    rule: "v1",
    score: 0.6154,
    place: "same",
    towns: "unknown",
    dt_min: 40,
    km: 27.9,
    created_at: "2026-10-11T00:00:00Z",
    ...over,
  });
  it("says the score, both place checks, the gap and the distance", () => {
    expect(linkLine(link())).toBe("LINK 0.62 · PLACES AGREE · TOWN UNREAD · 40 MIN AFTER · 28 KM");
  });
  it("signs the gap from the event asked about and drops a distance it does not have", () => {
    expect(linkLine(link({ dt_min: -0.4, km: null, place: "unknown", towns: "same" }))).toBe(
      "LINK 0.62 · A PLACE UNNAMED · SAME TOWN · SAME MINUTE",
    );
    expect(linkLine(link({ dt_min: -90, km: null }))).toBe("LINK 0.62 · PLACES AGREE · TOWN UNREAD · 90 MIN BEFORE");
  });
});
