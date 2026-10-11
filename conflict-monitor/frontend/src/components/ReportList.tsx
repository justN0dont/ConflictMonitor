/**
 * The reports under one event (C84): every row the corroboration chip counts,
 * with the text it was written from, its byline, its forward origin and its
 * own stated death toll. Fetched when the event is opened, never held for the
 * whole store.
 *
 * raw_text was in the type declaration and nowhere on screen, so the
 * one-second check killed_reported promises ("copied from its text") could not
 * be run from the UI. Here the number and the text sit in the same row.
 *
 * Under the reports, the linked events: separate rows the link rule reads as
 * this incident, each with the score and what the rule saw, so the reader can
 * disagree with the bar. Nothing was merged.
 */
import { useEffect, useState } from "react";
import { LINK_TITLE, linkLine, reportByline, unrecordedNote } from "../lib/corroboration";
import type { Corroboration, EventLinkRow, EventReportRow } from "../types/event";

const API_BASE = (import.meta as any).env?.VITE_API_URL || "http://localhost:8000";

type State = { kind: "loading" } | { kind: "error" } | { kind: "ready"; rows: EventReportRow[] };
type Links = { kind: "loading" } | { kind: "error" } | { kind: "ready"; rows: EventLinkRow[] };

const MONO = { fontFamily: "var(--font-mono)" } as const;

function when(iso: string): string {
  return new Date(iso).toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

interface ReportListProps {
  eventId: number;
  corroboration?: Corroboration | null;
}

export function ReportList({ eventId, corroboration }: ReportListProps) {
  const [state, setState] = useState<State>({ kind: "loading" });
  const [links, setLinks] = useState<Links>({ kind: "loading" });

  useEffect(() => {
    const ctrl = new AbortController();
    setState({ kind: "loading" });
    setLinks({ kind: "loading" });
    fetch(`${API_BASE}/events/${eventId}/reports`, { signal: ctrl.signal })
      .then((r) => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        return r.json();
      })
      .then((rows: EventReportRow[]) => setState({ kind: "ready", rows }))
      .catch((e: unknown) => {
        if ((e as { name?: string }).name !== "AbortError") setState({ kind: "error" });
      });
    fetch(`${API_BASE}/events/${eventId}/links`, { signal: ctrl.signal })
      .then((r) => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        return r.json();
      })
      .then((rows: EventLinkRow[]) => setLinks({ kind: "ready", rows }))
      .catch((e: unknown) => {
        if ((e as { name?: string }).name !== "AbortError") setLinks({ kind: "error" });
      });
    return () => ctrl.abort();
  }, [eventId]);

  const note = unrecordedNote(corroboration);
  const muted = "text-[9px] tracking-[0.1em] text-[var(--text-muted)]";

  return (
    <div className="mt-2 flex flex-col gap-1.5 border-t border-[var(--border)] pt-2">
      {state.kind === "loading" && (
        <div className={muted} style={MONO}>
          READING REPORTS…
        </div>
      )}
      {/* An unanswered fetch is not an empty list: the two say different things. */}
      {state.kind === "error" && (
        <div className={muted} style={MONO}>
          REPORTS UNAVAILABLE · the API did not answer
        </div>
      )}
      {state.kind === "ready" && state.rows.length === 0 && (
        <div className={muted} style={MONO}>
          NO REPORT ROW UNDER THIS EVENT · its count has no text behind it
        </div>
      )}
      {state.kind === "ready" &&
        state.rows.map((r) => (
          <div key={r.id} className="border-l border-[var(--border)] pl-2">
            <div
              className="flex flex-wrap items-baseline gap-x-2 text-[9px] tracking-[0.06em] text-[var(--text-muted)]"
              style={MONO}
            >
              <span className="text-[var(--text-secondary)]">{reportByline(r)}</span>
              <span className="tabular-nums" title="When the source said it">
                {when(r.reported_at)}
              </span>
              {/* The number beside the text it was copied from: the check is local. */}
              {r.killed_reported != null && (
                <span
                  className="tabular-nums text-[var(--text-secondary)]"
                  title="People this report stated were killed, copied from the text below."
                >
                  {r.killed_reported} KILLED
                </span>
              )}
              {r.source_url && (
                <a
                  href={r.source_url}
                  target="_blank"
                  rel="noreferrer"
                  className="text-[var(--text-secondary)] underline decoration-dotted"
                >
                  SOURCE ↗
                </a>
              )}
            </div>
            <div className="mt-0.5 max-h-[7.5em] overflow-y-auto whitespace-pre-wrap text-[11px] leading-[1.45] text-[var(--text-primary)]">
              {r.raw_text}
            </div>
          </div>
        ))}
      {state.kind === "ready" && note && (
        <div className={muted} style={MONO}>
          {note}
        </div>
      )}
      {/* The linked events: rows the rule reads as this incident, never merged.
          An unanswered fetch and an empty list say different things here too. */}
      {links.kind === "error" && (
        <div className={muted} style={MONO}>
          LINKS UNAVAILABLE · the API did not answer
        </div>
      )}
      {links.kind === "ready" && links.rows.length > 0 && (
        <div className="mt-1 flex flex-col gap-1.5">
          <div className={muted} style={MONO} title={LINK_TITLE}>
            LINKED EVENTS · {links.rows.length} · SEPARATE ROWS THE LINK RULE READS AS THIS INCIDENT
          </div>
          {links.rows.map((l) => (
            <div key={l.event.id} className="border-l border-dashed border-[var(--border)] pl-2">
              <div
                className="flex flex-wrap items-baseline gap-x-2 text-[9px] tracking-[0.06em] text-[var(--text-muted)]"
                style={MONO}
              >
                <span className="text-[var(--text-secondary)]">{l.event.channel_name || "unnamed channel"}</span>
                <span className="tabular-nums" title="When the source said it">
                  {when(l.event.timestamp)}
                </span>
                <span className="tabular-nums" title={LINK_TITLE}>
                  {linkLine(l)}
                </span>
              </div>
              <div className="mt-0.5 text-[11px] leading-[1.45] text-[var(--text-primary)]">{l.event.summary}</div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
