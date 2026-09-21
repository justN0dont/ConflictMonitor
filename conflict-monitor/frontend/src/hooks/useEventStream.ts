import { useCallback, useEffect, useRef, useState } from "react";
import type { ConflictEvent, EventWSMessage } from "../types/event";

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
const REST_URL = `${API_BASE}/events?limit=${MAX_EVENTS}`;

export function useEventStream() {
  const [events, setEvents] = useState<ConflictEvent[]>([]);
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
          setEvents((prev) =>
            [event, ...prev]
              .sort((a, b) => new Date(b.timestamp).getTime() - new Date(a.timestamp).getTime())
              .slice(0, MAX_EVENTS),
          );
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
      .then((data: ConflictEvent[]) => setEvents(data))
      .catch(() => {});
  }, []);

  useEffect(() => {
    connect();
    return () => {
      clearTimeout(reconnectTimer.current);
      wsRef.current?.close();
    };
  }, [connect]);

  const latestEvent = events[0] ?? null;

  return { events, isConnected, latestEvent };
}
