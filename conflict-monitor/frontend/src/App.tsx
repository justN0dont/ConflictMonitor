import { useCallback, useEffect, useMemo, useState } from "react";
import { EscalationGauge } from "./components/EscalationGauge";
import { Header } from "./components/Header";
import { LiveFeed } from "./components/LiveFeed";
import { MapPanel } from "./components/MapPanel";
import { TimelineScrubber } from "./components/TimelineScrubber";
import { useEventStream } from "./hooks/useEventStream";
import { useTracking } from "./hooks/useTracking";

const API_BASE = (import.meta as any).env?.VITE_API_URL || "http://localhost:8000";

export default function App() {
  const { events, isConnected } = useEventStream();
  const { aircraft, tleData, jammingZones, vessels, aircraftTracks, vesselTracks } = useTracking();
  const [demoMode, setDemoMode] = useState(false);
  const [timeRange, setTimeRange] = useState<{ earliest: Date; latest: Date } | null>(null);
  const [activeRange, setActiveRange] = useState<{ start: Date; end: Date } | null>(null);

  // Fetch config
  useEffect(() => {
    fetch(`${API_BASE}/config`)
      .then((r) => r.json())
      .then((data) => setDemoMode(!!data.demo_mode))
      .catch(() => {});
  }, []);

  // Fetch time range on mount and periodically
  useEffect(() => {
    const fetchRange = () => {
      fetch(`${API_BASE}/events/time-range`)
        .then((r) => r.json())
        .then((data) => {
          if (data.earliest && data.latest) {
            setTimeRange({
              earliest: new Date(data.earliest),
              latest: new Date(data.latest),
            });
          }
        })
        .catch(() => {});
    };
    fetchRange();
    const interval = setInterval(fetchRange, 30000);
    return () => clearInterval(interval);
  }, []);

  // Filter events by active time range
  const filteredEvents = useMemo(() => {
    if (!activeRange) return events;
    const start = activeRange.start.getTime();
    const end = activeRange.end.getTime();
    return events.filter((e) => {
      const t = new Date(e.timestamp).getTime();
      return t >= start && t <= end;
    });
  }, [events, activeRange]);

  const onRangeChange = useCallback((range: { start: Date; end: Date } | null) => {
    setActiveRange(range);
  }, []);

  return (
    <div className="app-grid">
      <Header
        isConnected={isConnected}
        eventCount={filteredEvents.length}
        aircraftCount={aircraft.filter((a) => !a.on_ground).length}
        vesselCount={vessels.length}
        satelliteCount={tleData.length}
        demoMode={demoMode}
      />
      <MapPanel events={filteredEvents} aircraft={aircraft} vessels={vessels} tleData={tleData} jammingZones={jammingZones} aircraftTracks={aircraftTracks} vesselTracks={vesselTracks} />
      <div className="sidebar">
        <div className="sidebar-feed"><LiveFeed events={filteredEvents} /></div>
        <div className="sidebar-gauge"><EscalationGauge events={filteredEvents} /></div>
      </div>
      <TimelineScrubber
        allEvents={events}
        timeRange={timeRange}
        activeRange={activeRange}
        onRangeChange={onRangeChange}
      />
    </div>
  );
}
