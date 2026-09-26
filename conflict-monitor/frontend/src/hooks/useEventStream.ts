import { useCallback, useEffect, useRef, useState } from "react";
import type { ConflictEvent, EventWSMessage } from "../types/event";
import { type ArrivalLog, logArrival, mergeEvent } from "../lib/events";

const MAX_EVENTS = 200;
const RECONNECT_DELAY = 3000;
const API_BASE = (import.meta as any).env?.VITE_API_URL || "http://localhost:8000";
const WS_URL = API_BASE.replace(/^http/, "ws") + "/ws/events";
// Fetch what this client is willing to hold. MAX_EVENTS has been 200 since
// this hook was written, but the initial fetch asked for 50, so the store was
// capped at a quarter of its own limit by a number nobody reconciled.
//
// It reads as the project's own bug class on the widest element on screen:
// TimelineScrubber draws its axis from /events/time-range, which spans the
// WHOLE table, and its ticks from this array. Fetching 50 of 200 therefore
// rendered most of that axis as empty space -- "nothing happened" standing in
// for "this client asked for 50". The route accepts up to 500
// (routes/events.py:31); 200 is MAX_EVENTS, so the two now agree.
//
// One more than it holds: if the 201st comes back, the store is truncated and
// every count drawn from it is a floor (lib/events.ts, countIsFloor).
const REST_URL = `${API_BASE}/events?limit=${MAX_EVENTS + 1}`;

export function useEventStream() {
  const [store, setStore] = useState<{ events: ConflictEvent[]; truncated: boolean }>({
    events: [],
    truncated: false,
  });
  // When each event arrived over the live stream (C50). Backfill never arrives.
  const [arrivals, setArrivals] = useState<ArrivalLog>(new Map());
  const knownIds = useRef<Set<number>>(new Set());
  const [isConnected, setIsConnected] = useState(false);
  const wsRef = useRef<WebSocket | null>(null);
  const reconnectTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);

  const connect = useCallback(() => {
    if (wsRef.current?.readyState === WebSocket.OPEN) return;

    const ws = new WebSocket(WS_URL);
    wsRef.current = ws;

    ws.onopen = () => {
      setIsConnected(true);
      // Send periodic pings to keep the connection alive
      const pingInterval = setInterval(() => {
        if (ws.readyState === WebSocket.OPEN) {
          ws.send("ping");
        } else {
          clearInterval(pingInterval);
        }
      }, 30000);
      (ws as any)._pingInterval = pingInterval;
    };

    ws.onmessage = (e) => {
      try {
        const msg = JSON.parse(e.data);
        if (msg.type === "ping") return; // ignore server pings
        if (msg.type === "new_event") {
          const event = msg.event as ConflictEvent;
          // A merge is re-broadcast under its existing id: replace it, and do
          // not flash it as an arrival.
          const known = knownIds.current.has(event.id);
          knownIds.current.add(event.id);
          setStore((s) => {
            const m = mergeEvent(s.events, event, MAX_EVENTS);
            return { events: m.events, truncated: s.truncated || m.evicted };
          });
          if (!known) setArrivals((log) => logArrival(log, event.id, Date.now()));
        }
      } catch {
        console.warn("WS: malformed message", e.data);
      }
    };

    ws.onclose = () => {
      setIsConnected(false);
      clearInterval((ws as any)._pingInterval);
      reconnectTimer.current = setTimeout(connect, RECONNECT_DELAY);
    };

    ws.onerror = () => ws.close();
  }, []);

  // Fetch historical events on mount
  useEffect(() => {
    fetch(REST_URL)
      .then((r) => r.json())
      .then((data: ConflictEvent[]) => {
        for (const e of data) knownIds.current.add(e.id);
        setStore({ events: data.slice(0, MAX_EVENTS), truncated: data.length > MAX_EVENTS });
      })
      .catch(() => {});
  }, []);

  useEffect(() => {
    connect();
    return () => {
      clearTimeout(reconnectTimer.current);
      wsRef.current?.close();
    };
  }, [connect]);

  const { events, truncated } = store;
  const latestEvent = events[0] ?? null;

  return { events, truncated, arrivals, isConnected, latestEvent };
}
