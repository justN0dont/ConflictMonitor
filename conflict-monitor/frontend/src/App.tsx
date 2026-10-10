import { useCallback, useEffect, useState } from "react";
import { ConnectivityRail } from "./components/ConnectivityRail";
import { Header } from "./components/Header";
import { IndicatorRail } from "./components/IndicatorRail";
import { LiveFeed } from "./components/LiveFeed";
import { MapPanel } from "./components/MapPanel";
import { TimelineScrubber } from "./components/TimelineScrubber";
import { useEventStream } from "./hooks/useEventStream";
import { useHistory, useTimelineAxis } from "./hooks/useHistory";
import { useTracking } from "./hooks/useTracking";

const API_BASE = (import.meta as any).env?.VITE_API_URL || "http://localhost:8000";

export default function App() {
  const { events, truncated, arrivals, isConnected } = useEventStream();
  const { aircraft, aircraftFeed, aircraftReceivedAt, vesselsFeed, vesselsReceivedAt, tleFeed, tleReceivedAt, tleData, jammingZones, jammingStatus, vessels, aircraftTracks, vesselTracks, connectivity, connectivityStatus } = useTracking();
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

  // A scrubbed window is fetched from the server, not filtered out of the
  // 200 newest events the live store holds (C81): LIVE shows the store, a
  // window shows its own answer. The axis under the scrubber is the whole
  // table's density plus a liveness lane per ingest feed.
  const history = useHistory(activeRange);
  const axis = useTimelineAxis(timeRange);
  const filteredEvents = activeRange ? history.events : events;

  // LIVE: the store holds the newest 200, so its count is a floor once it is
  // full. A window: a floor only when the server's cap cut it short.
  const eventCountIsFloor = activeRange ? history.floor : truncated;

  const onRangeChange = useCallback((range: { start: Date; end: Date } | null) => {
    setActiveRange(range);
  }, []);

  return (
    <div className="app-grid">
      <Header
        events={filteredEvents}
        allEvents={events}
        aircraft={aircraft}
        aircraftFeed={aircraftFeed}
        aircraftReceivedAt={aircraftReceivedAt}
        vesselsFeed={vesselsFeed}
        vesselsReceivedAt={vesselsReceivedAt}
        tleFeed={tleFeed}
        vessels={vessels}
        tleData={tleData}
        jammingStatus={jammingStatus}
        isConnected={isConnected}
        demoMode={demoMode}
        eventCountIsFloor={eventCountIsFloor}
      />
      <MapPanel events={filteredEvents} arrivals={arrivals} aircraft={aircraft} vessels={vessels} tleData={tleData} jammingZones={jammingZones} jammingStatus={jammingStatus} aircraftTracks={aircraftTracks} vesselTracks={vesselTracks} aircraftFeedState={aircraftFeed.state} vesselsFeedState={vesselsFeed.state} tleFeedState={tleFeed.state} />
      <div className="sidebar">
        <div className="sidebar-feed"><LiveFeed events={filteredEvents} arrivals={arrivals} /></div>
        <div className="sidebar-connectivity">
          <ConnectivityRail countries={connectivity} status={connectivityStatus} />
        </div>
        <div className="sidebar-rail">
          <IndicatorRail
            events={filteredEvents}
            allEvents={events}
            aircraft={aircraft}
            aircraftFeed={aircraftFeed}
            aircraftReceivedAt={aircraftReceivedAt}
            vesselsFeed={vesselsFeed}
            vesselsReceivedAt={vesselsReceivedAt}
            tleFeed={tleFeed}
            tleReceivedAt={tleReceivedAt}
            vessels={vessels}
            tleData={tleData}
            jammingStatus={jammingStatus}
            jammingZones={jammingZones}
            isConnected={isConnected}
            demoMode={demoMode}
            eventCountIsFloor={eventCountIsFloor}
          />
        </div>
      </div>
      <TimelineScrubber
        heldCount={events.length}
        timeRange={timeRange}
        activeRange={activeRange}
        onRangeChange={onRangeChange}
        truncated={truncated}
        density={axis.density}
        lanes={axis.lanes}
        window={{ count: filteredEvents.length, loaded: history.loaded, floor: history.floor }}
      />
    </div>
  );
}
