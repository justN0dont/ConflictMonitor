# Conflict Monitor

Real-time geopolitical conflict monitoring dashboard. Ingests OSINT Telegram channels, classifies events using Claude AI, geocodes locations via Nominatim, and displays everything on an interactive map with live aircraft tracking, maritime vessel monitoring, military satellite orbits, and GPS jamming detection.

Built in response to [Bilawal Sidhu's WorldView](https://www.spatialintelligence.ai/p/i-built-a-spy-satellite-simulator) — this is the analysis layer that sits behind the visualization.

> **Known issues, measurements and roadmap:** [`docs/FINDINGS.md`](docs/FINDINGS.md) — a living
> register of verified defects, sensor-coverage measurements, and the phased plan. Updated in the
> same commit as the change it describes.

## Quick Start (Demo Mode)

No API keys required:

```bash
git clone https://github.com/troofevades-rgb/conflict-monitor.git
cd conflict-monitor/conflict-monitor
echo "DEMO_MODE=true" > .env
docker-compose up
```

Open **http://localhost:5173** — you'll see 300 pre-seeded historical events, synthetic aircraft flying routes across the Middle East, vessel tracks in the Persian Gulf and Red Sea, real military satellite orbits from CelesTrak, and new events generating every 15-45 seconds.

## Features

**Multi-source Telegram ingestion** — monitors 10+ OSINT channels simultaneously via Telethon with automatic backfill of recent messages.

**AI-powered event classification** — Claude Sonnet extracts event type (military/diplomatic/economic/cyber), severity (1-10), location name, and summary from raw Telegram messages. Pydantic validation ensures clean output.

**Two-stage geocoding** — Claude extracts the location name, then Nominatim/OpenStreetMap resolves precise coordinates. 70+ location fallback table covers military bases, straits, and colloquial names. LRU cache prevents repeated lookups.

**Three view modes** — 2D operational map (Mapbox), 3D globe (Three.js with satellite.js orbit propagation), and photorealistic terrain (CesiumJS with optional Google 3D Tiles).

**Live aircraft tracking** — real ADS-B data from adsb.lol (free, no auth), with OpenSky Network as fallback. 15-second polling. Callsign labels, altitude, speed.

**Maritime vessel tracking** — AIS data via AISStream.io WebSocket. Tankers, cargo, military vessels with heading, speed, destination.

**Military satellite orbits** — CelesTrak TLE data propagated in real-time with satellite.js. Ground track lines, orbital paths.

**GPS jamming detection** — infers jamming zones by clustering aircraft with degraded navigation accuracy (MLAT fallback, NIC/NAC_P degradation).

**Post-processing filters** — CRT scanlines, night vision (green monochrome + noise), FLIR thermal (white-hot palette). Real GLSL shaders.

**Timeline scrubber** — drag to select time range, play/pause with 1-30x speed, keyboard shortcuts (Space, arrow keys), LIVE mode snap-back.

**Event deduplication** — Jaccard similarity + PostGIS spatial proximity (50km) prevents duplicate markers when multiple channels report the same event.

**Source reliability scoring** — each Telegram channel has a 1-5 reliability rating. Filter events by minimum reliability.

**Escalation gauge** — rolling average severity of last 20 events with color-coded status: LOW ACTIVITY, ELEVATED, HIGH ALERT, CRITICAL.

**Demo mode** — full synthetic data generator with realistic events, aircraft routes, vessel tracks, and GPS jamming simulation. Zero API keys needed.

## Architecture

```
Telegram Channels --> Telethon Listener --> Claude Classifier --> Nominatim Geocoder
                                                                        |
                                                                  +-----v------+
                                                                  |  PostGIS   |
                                                                  |  (Events)  |
                                                                  +-----+------+
                                                                        |
adsb.lol ----------> Aircraft Poller -------> In-memory Cache --> FastAPI REST
AISStream ---------> Maritime Poller -------> In-memory Cache --> + WebSocket
CelesTrak ---------> TLE Fetcher ----------> In-memory Cache --> Broadcaster
                                                                        |
                                                                  +-----v------+
                                                                  |   React    |
                                                                  |  Frontend  |
                                                                  +------------+
```

## Full Setup

### 1. Telegram API credentials

Go to [my.telegram.org](https://my.telegram.org), create an app, get your `api_id` and `api_hash`.

### 2. Anthropic API key

Get one from [console.anthropic.com](https://console.anthropic.com). Used for event classification.

### 3. Mapbox token

Sign up at [mapbox.com](https://www.mapbox.com) for the 2D dark map view.

### 4. Optional API keys

- **AISStream** — free key from [aisstream.io](https://aisstream.io) for maritime vessel tracking
- **Google Maps** — for photorealistic 3D Tiles in terrain view (enable "Map Tiles API")
- **Cesium Ion** — for world terrain elevation (free tier available)
- **OpenSky Network** — increases aircraft polling rate limit

### 5. Configure and run

```bash
cp .env.example .env
# Fill in your API keys
docker-compose up
```

## Tests

```bash
docker compose up -d db
docker compose run --rm -T --no-deps -e LLM_BACKEND=none backend pytest
```

Runs in the backend container against a throwaway `conflict_monitor_test`
database, created and dropped by the suite; the dev database is never opened.
`-e LLM_BACKEND=none` and the network ban in `backend/tests/conftest.py` mean
no test can reach Ollama, Anthropic or Nominatim — an attempt is a failure with
a stack trace, not a skip. Seconds, not minutes. There is no CI: this runs when
someone types it.

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `TELEGRAM_API_ID` | For live data | Telegram API ID |
| `TELEGRAM_API_HASH` | For live data | Telegram API hash |
| `TELEGRAM_PHONE` | For live data | Phone number for Telegram auth |
| `ANTHROPIC_API_KEY` | For live data | Claude API key for classification |
| `MAPBOX_TOKEN` | For 2D view | Mapbox GL access token |
| `AISSTREAM_API_KEY` | Optional | AISStream.io key for vessel tracking |
| `GOOGLE_MAPS_KEY` | Optional | Enables Google 3D Tiles in terrain view |
| `CESIUM_ION_TOKEN` | Optional | Enables Cesium world terrain elevation |
| `OPENSKY_USERNAME` | Optional | OpenSky Network credentials |
| `OPENSKY_PASSWORD` | Optional | OpenSky Network credentials |
| `DEMO_MODE` | Optional | Set `true` for synthetic data (no keys needed) |
| `VITE_API_URL` | Optional | Frontend API URL (default: http://localhost:8000) |

## Data Sources

| Source | Data | Update Frequency | Auth Required |
|--------|------|-----------------|---------------|
| Telegram | Conflict events | Real-time | API ID + Hash |
| Claude (Anthropic) | Event classification | Per message | API key |
| Nominatim (OSM) | Geocoding | Per message (cached) | None |
| adsb.lol | Aircraft positions | 15 seconds | None |
| AISStream.io | Maritime vessels | Real-time (WebSocket) | Free API key |
| CelesTrak | Military satellite TLEs | 6 hours | None |

## Tech Stack

**Backend:** Python 3.12, FastAPI, SQLAlchemy + asyncpg, PostGIS, Telethon, Anthropic SDK, httpx, Alembic

**Frontend:** React 19, TypeScript, Vite, Three.js + React Three Fiber, CesiumJS + Resium, Mapbox GL, satellite.js

**Infrastructure:** Docker Compose, PostgreSQL + PostGIS

## Monitored Channels

| Channel | Reliability | Notes |
|---------|-------------|-------|
| Aurora Intel | 5/5 | Institutional-grade OSINT |
| OSINTdefender | 4/5 | Consistently accurate |
| Sentdefender | 4/5 | Fast, reliable |
| Iran International English | 4/5 | Iranian diaspora media |
| Conflict Intelligence Group | 4/5 | Analytical focus |
| Middle East Spectator | 4/5 | Broad coverage |
| Israel Radar | 3/5 | Israel-focused |
| Military OSINT | 3/5 | Military-focused aggregator |
| Intel Slava Z | 2/5 | High volume, pro-Russian bias |

## License

MIT

## Contributing

PRs welcome. If you're adding a new data source, follow the existing service pattern: create a module in `backend/app/services/`, add a cache + poller function, register it in `main.py` lifespan, and add a REST endpoint in `routes/tracking.py`.