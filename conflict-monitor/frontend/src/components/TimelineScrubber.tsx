import { useCallback, useEffect, useRef, useState } from "react";
import type { ConflictEvent } from "../types/event";
import { eventVisual } from "../lib/tokens";
import { SPEEDS, windowAt } from "../lib/playback";

function formatTimeLabel(d: Date): string {
  const months = ["JAN","FEB","MAR","APR","MAY","JUN","JUL","AUG","SEP","OCT","NOV","DEC"];
  return `${String(d.getUTCDate()).padStart(2, "0")} ${months[d.getUTCMonth()]} ${String(d.getUTCHours()).padStart(2, "0")}:${String(d.getUTCMinutes()).padStart(2, "0")} UTC`;
}

function formatDuration(ms: number): string {
  const mins = Math.floor(ms / 60000);
  if (mins < 60) return `${mins}m window`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h window`;
  const days = Math.floor(hrs / 24);
  return `${days}d window`;
}

interface TimelineScrubberProps {
  /** All events (unfiltered) for drawing tick marks */
  allEvents: ConflictEvent[];
  /** The full time range from the API */
  timeRange: { earliest: Date; latest: Date } | null;
  /** Current filter — null means show all (LIVE mode) */
  activeRange: { start: Date; end: Date } | null;
  /** Called when the user changes the time range */
  onRangeChange: (range: { start: Date; end: Date } | null) => void;
}

export function TimelineScrubber({ allEvents, timeRange, activeRange, onRangeChange }: TimelineScrubberProps) {
  const trackRef = useRef<HTMLDivElement>(null);
  const [isDragging, setIsDragging] = useState<"left" | "right" | "window" | null>(null);
  const [isPlaying, setIsPlaying] = useState(false);
  // Data milliseconds per wall millisecond (lib/playback.ts, C44).
  const [speed, setSpeed] = useState(SPEEDS[0].rate);
  const dragStartRef = useRef({ x: 0, startMs: 0, endMs: 0 });

  // Derived range values
  const rangeStart = timeRange?.earliest?.getTime() ?? (Date.now() - 7 * 86400000);
  const rangeEnd = timeRange?.latest?.getTime() ?? Date.now();
  const fullSpan = Math.max(rangeEnd - rangeStart, 60000); // at least 1 minute

  // Default window: last 6 hours
  const defaultWindowMs = 6 * 3600000;

  // Active window in ms
  const winStart = activeRange?.start?.getTime() ?? rangeEnd - defaultWindowMs;
  const winEnd = activeRange?.end?.getTime() ?? rangeEnd;

  // Position as percentage
  const leftPct = ((winStart - rangeStart) / fullSpan) * 100;
  const widthPct = ((winEnd - winStart) / fullSpan) * 100;

  // ── Playback ──────────────────────────────────────────────
  // C44: where the window is follows from wall time elapsed since playback was
  // anchored, so a throttled or busy tab plays at the labelled rate, only less
  // smoothly. The tick only redraws. The effect restarts on play/pause and on a
  // speed change - not on every window it produces - and reads the window it
  // starts from through a ref.
  const windowRef = useRef({ start: winStart, end: winEnd });
  windowRef.current = { start: winStart, end: winEnd };
  const anchorRef = useRef<{ wall: number; start: number; size: number } | null>(null);

  /** Move the window; while playing, playback continues from the new place. */
  const seek = useCallback((range: { start: Date; end: Date } | null) => {
    if (range && anchorRef.current) {
      anchorRef.current = {
        wall: performance.now(),
        start: range.start.getTime(),
        size: range.end.getTime() - range.start.getTime(),
      };
    }
    onRangeChange(range);
  }, [onRangeChange]);

  useEffect(() => {
    if (!isPlaying) {
      anchorRef.current = null;
      return;
    }
    const w = windowRef.current;
    anchorRef.current = { wall: performance.now(), start: w.start, size: w.end - w.start };
    let cancelled = false;
    const id = window.setInterval(() => {
      const a = anchorRef.current;
      if (cancelled || !a) return;
      const next = windowAt(a.wall, a.start, a.size, speed, performance.now(), Date.now());
      if (next === null) {
        // Reached live - stop playing and go to LIVE mode
        setIsPlaying(false);
        onRangeChange(null);
        return;
      }
      onRangeChange({ start: new Date(next.start), end: new Date(next.end) });
    }, 100);
    // A hidden tab pauses rather than playing on unseen; press play to resume.
    const onVisibility = () => {
      if (document.hidden) setIsPlaying(false);
    };
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      cancelled = true;
      clearInterval(id);
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, [isPlaying, speed, onRangeChange]);

  // ── Keyboard shortcuts ────────────────────────────────────
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (e.target instanceof HTMLInputElement || e.target instanceof HTMLSelectElement) return;
      if (e.code === "Space") {
        e.preventDefault();
        if (!activeRange) {
          // Enter time filter mode with default window
          onRangeChange({ start: new Date(Date.now() - defaultWindowMs), end: new Date() });
        }
        setIsPlaying((p) => !p);
      } else if (e.code === "ArrowLeft" && activeRange) {
        e.preventDefault();
        const step = (winEnd - winStart) * 0.1;
        seek({ start: new Date(winStart - step), end: new Date(winEnd - step) });
      } else if (e.code === "ArrowRight" && activeRange) {
        e.preventDefault();
        const step = (winEnd - winStart) * 0.1;
        const newEnd = winEnd + step;
        if (newEnd >= Date.now()) {
          setIsPlaying(false);
          onRangeChange(null); // snap to live
        } else {
          seek({ start: new Date(winStart + step), end: new Date(newEnd) });
        }
      }
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [activeRange, winStart, winEnd, onRangeChange, seek]);

  // ── Mouse drag handling ───────────────────────────────────
  const getTimeFromX = useCallback((clientX: number): number => {
    if (!trackRef.current) return 0;
    const rect = trackRef.current.getBoundingClientRect();
    const pct = Math.max(0, Math.min(1, (clientX - rect.left) / rect.width));
    return rangeStart + pct * fullSpan;
  }, [rangeStart, fullSpan]);

  const onMouseDown = useCallback((e: React.MouseEvent, target: "left" | "right" | "window") => {
    e.preventDefault();
    e.stopPropagation();
    setIsDragging(target);
    setIsPlaying(false);
    dragStartRef.current = { x: e.clientX, startMs: winStart, endMs: winEnd };
  }, [winStart, winEnd]);

  useEffect(() => {
    if (!isDragging) return;

    const onMove = (e: MouseEvent) => {
      const { x: startX, startMs, endMs } = dragStartRef.current;
      const dx = e.clientX - startX;
      const trackWidth = trackRef.current?.getBoundingClientRect().width ?? 1;
      const dtMs = (dx / trackWidth) * fullSpan;

      if (isDragging === "window") {
        let newStart = startMs + dtMs;
        let newEnd = endMs + dtMs;
        // Clamp
        if (newStart < rangeStart) { newEnd += rangeStart - newStart; newStart = rangeStart; }
        if (newEnd > Date.now()) { newStart -= newEnd - Date.now(); newEnd = Date.now(); }
        onRangeChange({ start: new Date(newStart), end: new Date(newEnd) });
      } else if (isDragging === "left") {
        const newStart = Math.max(rangeStart, Math.min(endMs - 60000, startMs + dtMs));
        onRangeChange({ start: new Date(newStart), end: new Date(endMs) });
      } else if (isDragging === "right") {
        const newEnd = Math.min(Date.now(), Math.max(startMs + 60000, endMs + dtMs));
        onRangeChange({ start: new Date(startMs), end: new Date(newEnd) });
      }
    };

    const onUp = () => setIsDragging(null);
    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", onUp);
    return () => { window.removeEventListener("mousemove", onMove); window.removeEventListener("mouseup", onUp); };
  }, [isDragging, fullSpan, rangeStart, onRangeChange]);

  // ── Click on track to jump ────────────────────────────────
  const onTrackClick = useCallback((e: React.MouseEvent) => {
    if (isDragging) return;
    const clickTime = getTimeFromX(e.clientX);
    const halfWindow = (winEnd - winStart) / 2;
    const newStart = clickTime - halfWindow;
    const newEnd = clickTime + halfWindow;
    onRangeChange({ start: new Date(Math.max(rangeStart, newStart)), end: new Date(Math.min(Date.now(), newEnd)) });
  }, [getTimeFromX, winStart, winEnd, rangeStart, isDragging, onRangeChange]);

  // ── Event ticks ───────────────────────────────────────────
  const ticks = allEvents
    .filter((e) => e.lat != null && e.lon != null)
    .slice(0, 300)
    .map((e) => {
      const t = new Date(e.timestamp).getTime();
      const pct = ((t - rangeStart) / fullSpan) * 100;
      return { pct, severity: e.severity, type: e.event_type };
    })
    .filter((t) => t.pct >= 0 && t.pct <= 100);

  const isLive = activeRange === null;

  return (
    <div
      style={{
        gridArea: "timeline",
        background: "var(--bg-secondary, #0d1219)",
        borderTop: "1px solid var(--border, #1a2332)",
        display: "flex",
        alignItems: "center",
        padding: "0 16px",
        gap: 12,
        height: 72,
        boxShadow: "0 0 1px rgba(88,166,255,0.3)",
        userSelect: "none",
      }}
    >
      {/* ── Controls ── */}
      <div style={{ display: "flex", gap: 6, alignItems: "center", flexShrink: 0, width: 140 }}>
        {/* Play / Pause */}
        <button
          onClick={() => {
            if (isLive) {
              onRangeChange({ start: new Date(Date.now() - defaultWindowMs), end: new Date() });
            }
            setIsPlaying((p) => !p);
          }}
          style={{
            width: 28, height: 28, borderRadius: 4, border: "1px solid var(--border, #1a2332)",
            background: isPlaying ? "rgba(88,166,255,0.15)" : "rgba(10,14,20,0.9)",
            color: isPlaying ? "var(--accent-blue, #58a6ff)" : "var(--text-secondary, #5a6a7e)",
            cursor: "pointer", display: "flex", alignItems: "center", justifyContent: "center",
            fontSize: 14, fontFamily: "var(--font-mono, monospace)",
          }}
          title="Space to toggle"
        >
          {isPlaying ? "⏸" : "▶"}
        </button>

        {/* Speed */}
        <select
          value={speed}
          onChange={(e) => setSpeed(Number(e.target.value))}
          style={{
            padding: "3px 4px", fontFamily: "var(--font-mono, monospace)", fontSize: 9,
            background: "rgba(10,14,20,0.9)", border: "1px solid var(--border, #1a2332)",
            borderRadius: 3, color: "var(--text, #c8d6e5)", cursor: "pointer", outline: "none",
            width: 66,
          }}
          title="Playback rate: data time played per second"
        >
          {SPEEDS.map((s) => (
            <option key={s.rate} value={s.rate}>{s.label}</option>
          ))}
        </select>

        {/* LIVE button */}
        <button
          onClick={() => { setIsPlaying(false); onRangeChange(null); }}
          style={{
            padding: "4px 8px", fontFamily: "var(--font-mono, monospace)", fontSize: 9, fontWeight: 700,
            letterSpacing: 1, borderRadius: 3, cursor: "pointer",
            background: isLive ? "rgba(63,185,80,0.15)" : "rgba(10,14,20,0.9)",
            border: `1px solid ${isLive ? "var(--accent-green, #3fb950)" : "var(--border, #1a2332)"}`,
            color: isLive ? "var(--accent-green, #3fb950)" : "var(--text-secondary, #5a6a7e)",
          }}
        >
          LIVE
        </button>
      </div>

      {/* ── Timeline Track ── */}
      <div
        ref={trackRef}
        onClick={onTrackClick}
        style={{
          flex: 1, height: 40, position: "relative", cursor: "pointer",
          display: "flex", alignItems: "center",
        }}
      >
        {/* Background track */}
        <div style={{
          position: "absolute", left: 0, right: 0, top: "50%", transform: "translateY(-50%)",
          height: 4, background: "var(--border, #1a2332)", borderRadius: 2,
        }} />

        {/* Event tick marks */}
        {ticks.map((tick, i) => (
          <div
            key={i}
            style={{
              position: "absolute",
              left: `${tick.pct}%`,
              top: "50%",
              transform: "translate(-50%, -50%)",
              width: 1.5,
              // severity may be null ("never measured"). null * 1.5 is 0 in
              // JS, so these used to draw as the shortest tick - the look of
              // a measured, trivial event. Base height plus a lower opacity
              // reads as "no reading" instead.
              height: 4 + (tick.severity ?? 0) * 1.5,
              background: eventVisual(tick.type).color,
              opacity: tick.severity == null ? 0.28 : 0.6,
              borderRadius: 1,
              pointerEvents: "none",
            }}
          />
        ))}

        {/* Selection window (only shown when not LIVE) */}
        {!isLive && (
          <>
            {/* Dimmed areas outside the window */}
            <div style={{
              position: "absolute", left: 0, width: `${Math.max(0, leftPct)}%`,
              top: 0, bottom: 0, background: "rgba(0,0,0,0.4)", pointerEvents: "none",
            }} />
            <div style={{
              position: "absolute", left: `${Math.min(100, leftPct + widthPct)}%`, right: 0,
              top: 0, bottom: 0, background: "rgba(0,0,0,0.4)", pointerEvents: "none",
            }} />

            {/* Window overlay */}
            <div
              onMouseDown={(e) => onMouseDown(e, "window")}
              style={{
                position: "absolute",
                left: `${leftPct}%`,
                width: `${widthPct}%`,
                top: 4, bottom: 4,
                background: "rgba(88,166,255,0.08)",
                border: "1px solid rgba(88,166,255,0.3)",
                borderRadius: 3,
                cursor: "grab",
              }}
            />

            {/* Left handle */}
            <div
              onMouseDown={(e) => onMouseDown(e, "left")}
              style={{
                position: "absolute", left: `${leftPct}%`, top: 2, bottom: 2,
                width: 6, transform: "translateX(-50%)", cursor: "ew-resize",
                background: "var(--accent-blue, #58a6ff)", borderRadius: 2, opacity: 0.8,
              }}
            />

            {/* Right handle */}
            <div
              onMouseDown={(e) => onMouseDown(e, "right")}
              style={{
                position: "absolute", left: `${leftPct + widthPct}%`, top: 2, bottom: 2,
                width: 6, transform: "translateX(-50%)", cursor: "ew-resize",
                background: "var(--accent-blue, #58a6ff)", borderRadius: 2, opacity: 0.8,
              }}
            />
          </>
        )}

        {/* LIVE indicator line at right edge */}
        {isLive && (
          <div style={{
            position: "absolute", right: 0, top: 2, bottom: 2, width: 2,
            background: "var(--accent-green, #3fb950)", borderRadius: 1,
            boxShadow: "0 0 6px rgba(63,185,80,0.5)",
          }} />
        )}
      </div>

      {/* ── Time Display ── */}
      <div style={{ flexShrink: 0, width: 160, textAlign: "right" }}>
        {isLive ? (
          <div style={{ fontFamily: "var(--font-mono, monospace)", fontSize: 11, color: "var(--accent-green, #3fb950)", fontWeight: 600, letterSpacing: 1 }}>
            LIVE — ALL EVENTS
          </div>
        ) : (
          <>
            <div style={{ fontFamily: "var(--font-mono, monospace)", fontSize: 10, color: "var(--text, #c8d6e5)", lineHeight: 1.5 }}>
              {formatTimeLabel(new Date(winStart))}
            </div>
            <div style={{ fontFamily: "var(--font-mono, monospace)", fontSize: 10, color: "var(--text, #c8d6e5)", lineHeight: 1.5 }}>
              {formatTimeLabel(new Date(winEnd))}
            </div>
            <div style={{ fontFamily: "var(--font-mono, monospace)", fontSize: 9, color: "var(--text-secondary, #5a6a7e)", marginTop: 1 }}>
              {formatDuration(winEnd - winStart)}
            </div>
          </>
        )}
      </div>
    </div>
  );
}
