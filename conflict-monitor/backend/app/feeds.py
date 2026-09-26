"""Feed health: what each collector last tried, what it last got, and how old that is.

The schema decisions are in docs/FINDINGS.md, Roadmap > Phase 2 > "feed_health —
schema decisions". In short:

- One vocabulary. `FeedState` is declared worst-first and its rank IS the
  declaration order, so a roll-up over a value it does not know raises instead
  of skipping it. `unconfigured` sits outside the roll-up.
- Facts are stored, state is derived. A tracker holds three clocks —
  `last_attempt_at`, `last_success_at` and `source_epoch` (the upstream's own
  time for its newest data) — and `state()` computes the label from them
  against the server clock every time it is read. Nothing stores "ok".
- A failure never advances `last_success_at` or `source_epoch`, and a missing
  upstream time stays None: receipt time is never substituted for it.

This module is pure: no I/O, no database, and every method takes `now`, so the
state machine is testable without a clock. Pollers call `succeeded` / `failed`
synchronously on every attempt. Persistence (transition and heartbeat rows) is
a later step and reads these trackers; it never feeds them.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from enum import StrEnum


class FeedState(StrEnum):
    """Worst first. The order is the severity ranking: reordering it changes
    every roll-up, so tests pin it."""

    DEAD = "dead"                  # the poller task has exited
    AUTH_FAILED = "auth_failed"    # the upstream refused our credentials
    UNAVAILABLE = "unavailable"    # no success for longer than max_stale
    RETRYING = "retrying"          # last attempt failed; last good still inside max_stale
    STALE = "stale"                # polls succeed, but the upstream's clock is old or absent
    PENDING = "pending"            # configured, nothing succeeded yet since boot
    LIVE = "live"                  # current by the upstream's own clock
    UNCONFIGURED = "unconfigured"  # a required key is not set; outside the roll-up


RANK: dict[FeedState, int] = {
    s: i for i, s in enumerate(FeedState) if s is not FeedState.UNCONFIGURED
}


def worst(states) -> FeedState | None:
    """The worst of `states`, ignoring unconfigured feeds. An unranked value
    raises KeyError rather than being skipped."""
    ranked = [s for s in states if s is not FeedState.UNCONFIGURED]
    if not ranked:
        return None
    return min(ranked, key=RANK.__getitem__)


class ErrorKind(StrEnum):
    """Closed. Adding a member is a recorded decision (FINDINGS.md), not an edit."""

    OK = "ok"
    EMPTY = "empty"                # a well-formed reply with nothing in it: a success
    HTTP_ERROR = "http_error"
    RATE_LIMITED = "rate_limited"
    AUTH_FAILED = "auth_failed"
    TIMEOUT = "timeout"
    FETCH_ERROR = "fetch_error"
    PARSE_ERROR = "parse_error"    # a malformed 200 is a failure, never "empty"


SUCCESS_KINDS = frozenset({ErrorKind.OK, ErrorKind.EMPTY})

# An upstream clock this far ahead of ours is not believed.
CLOCK_SKEW_S = 60
DETAIL_MAX = 300
_QUERY = re.compile(r"\?\S*")


def clean_detail(text: str | None) -> str | None:
    """Free text for humans: query strings stripped (a key must never ride a
    URL into the table), length capped."""
    if not text:
        return None
    return _QUERY.sub("?…", str(text))[:DETAIL_MAX]


@dataclass(frozen=True)
class FeedSpec:
    id: str
    cadence_s: int
    stale_after_s: int
    max_stale_s: int
    requires_env: tuple[str, ...]
    # One sentence on what silence from this feed means, so nobody reads it as calm.
    silence_means: str


# Thresholds are UNMEASURED starting points (FINDINGS.md decision 2), to be
# re-set from the first week of heartbeat rows.
REGISTRY: dict[str, FeedSpec] = {
    "aircraft": FeedSpec(
        id="aircraft",
        cadence_s=15,
        stale_after_s=45,     # three polls
        max_stale_s=300,
        requires_env=(),      # adsb.lol is keyless; OpenSky credentials are optional
        silence_means="zero aircraft is state 2 unless the feed is live: the configured "
                       "centre has thin ADS-B coverage (FINDINGS.md, Sensor coverage)",
    ),
    "vessels": FeedSpec(
        id="vessels",
        cadence_s=10,         # a stream; this is the client's poll, not the upstream's
        stale_after_s=120,    # no accepted position report for 2 min (unmeasured)
        max_stale_s=600,      # the existing 600 s vessel filter
        requires_env=("AISSTREAM_API_KEY",),
        silence_means="zero vessels in the Gulf is state 2: AISStream's free tier has no "
                      "receivers there (FINDINGS.md, Blocked: no AIS coverage in the Gulf)",
    ),
    "satellites": FeedSpec(
        id="satellites",
        cadence_s=6 * 3600,
        # The upstream clock here is the newest element-set EPOCH, not a fetch
        # time. Fourteen days is a choice, not a measurement (GEV uses the same
        # number): military element sets are refreshed irregularly and some
        # legitimately carry epochs days old, while two weeks of drift puts a LEO
        # position off by far more than the map can show honestly.
        stale_after_s=14 * 86400,
        # No successful fetch (network or verified disk cache) for 48 h.
        max_stale_s=48 * 3600,
        requires_env=(),
        silence_means="zero satellites is state 2 unless the feed is live: CelesTrak "
                      "IP-blocks clients that fetch too often (FINDINGS.md, C20)",
    ),
    "connectivity": FeedSpec(
        id="connectivity",
        cadence_s=300,
        # The upstream clock is the newest real IODA measurement across every
        # watched country and sensor. IODA publishes with a lag of its own; an
        # hour matches the per-sensor STALE_FLOOR in services/connectivity.py.
        stale_after_s=3600,
        max_stale_s=1800,     # six missed cycles
        requires_env=(),      # IODA is keyless; Cloudflare Radar is reported separately
        silence_means="no disruption shown is state 2 unless the feed is live: IODA "
                      "not answering looks exactly like a country with nothing wrong",
    ),
    "rss": FeedSpec(
        id="rss",
        # One cycle fetches every feed (2 s apart), THEN classifies, so health
        # reflects the fetch and not how long the classifier took.
        cadence_s=300,
        # The upstream clock is the newest dated article across all feeds. Nine
        # outlets with nothing new in six hours means the feeds are frozen, not
        # that the world is quiet. Unmeasured.
        stale_after_s=6 * 3600,
        max_stale_s=1800,
        requires_env=(),
        silence_means="no new RSS events is state 2 unless the feed is live: a feed URL "
                      "can die and keep answering nothing",
    ),
    "telegram": FeedSpec(
        id="telegram",
        # Event-driven: there is no poll. A watchdog reports the connection once
        # a minute, and the upstream clock is the newest message seen, so a
        # connected listener on quiet channels reads stale, not live.
        cadence_s=60,
        stale_after_s=3 * 3600,
        max_stale_s=600,
        requires_env=("TELEGRAM_API_ID", "TELEGRAM_API_HASH"),
        silence_means="no Telegram events is state 2 unless the feed is live: a dropped "
                      "session or unresolved channels read the same as a quiet war",
    ),
}


@dataclass
class FeedTracker:
    spec: FeedSpec
    configured: bool = True
    first_attempt_at: float | None = None
    last_attempt_at: float | None = None
    last_success_at: float | None = None
    source_epoch: float | None = None
    consecutive_failures: int = 0
    error_kind: ErrorKind | None = None
    error_detail: str | None = None
    source: str | None = None
    fallback_from: str | None = None
    synthetic: bool = False
    count: int | None = None
    # Why an unconfigured feed is off, when it is not a missing key (demo mode).
    off_reason: str | None = None
    # Per-source outcome of the last cycle, for feeds that fan out (RSS). A
    # cycle is one success if any source answered - there is no partial state -
    # so the sources that did not answer are named here instead of hidden.
    parts: dict | None = None
    task: asyncio.Task | None = field(default=None, repr=False)
    # What the collector did since the last heartbeat; drained by the flusher.
    window_attempts: int = 0
    window_successes: int = 0
    window_failures: dict = field(default_factory=dict)

    def _attempt(self, now: float) -> None:
        if self.first_attempt_at is None:
            self.first_attempt_at = now
        self.last_attempt_at = now
        self.window_attempts += 1

    def drain_window(self) -> tuple[int, int, dict]:
        """(attempts, successes, failures by kind) since the last drain, and reset."""
        out = (self.window_attempts, self.window_successes, dict(self.window_failures))
        self.window_attempts, self.window_successes, self.window_failures = 0, 0, {}
        return out

    def succeeded(self, now: float, *, count: int, source: str,
                  source_epoch: float | None, kind: ErrorKind = ErrorKind.OK,
                  fallback_from: str | None = None, synthetic: bool = False,
                  parts: dict | None = None) -> None:
        if kind not in SUCCESS_KINDS:
            raise ValueError(f"{kind} is not a success")
        self._attempt(now)
        self.window_successes += 1
        self.last_success_at = now
        # None stays None: an upstream that gave no time does not get ours.
        self.source_epoch = source_epoch
        self.consecutive_failures = 0
        self.error_kind = kind
        self.error_detail = None
        self.source = source
        self.fallback_from = fallback_from
        self.synthetic = synthetic
        self.count = count
        self.parts = parts

    def failed(self, now: float, kind: ErrorKind, detail: str | None = None,
               parts: dict | None = None) -> None:
        if kind in SUCCESS_KINDS:
            raise ValueError(f"{kind} is not a failure")
        self._attempt(now)
        self.window_failures[kind.value] = self.window_failures.get(kind.value, 0) + 1
        self.consecutive_failures += 1
        self.error_kind = kind
        self.error_detail = clean_detail(detail)
        self.parts = parts

    def freshness(self, now: float) -> str:
        """'current' / 'old' / 'unknown', by the upstream's clock only."""
        if self.source_epoch is None or self.source_epoch - now > CLOCK_SKEW_S:
            return "unknown"
        return "current" if now - self.source_epoch <= self.spec.stale_after_s else "old"

    def state(self, now: float) -> FeedState:
        if not self.configured:
            return FeedState.UNCONFIGURED
        if self.task is not None and self.task.done():
            return FeedState.DEAD
        if self.consecutive_failures and self.error_kind is ErrorKind.AUTH_FAILED:
            return FeedState.AUTH_FAILED
        if self.last_success_at is None:
            if (self.first_attempt_at is not None
                    and now - self.first_attempt_at > self.spec.max_stale_s):
                return FeedState.UNAVAILABLE
            return FeedState.PENDING
        if now - self.last_success_at > self.spec.max_stale_s:
            return FeedState.UNAVAILABLE
        if self.consecutive_failures:
            return FeedState.RETRYING
        if self.freshness(now) != "current":
            return FeedState.STALE
        return FeedState.LIVE

    def reason(self, now: float) -> str | None:
        """Why the state is not live, in one short phrase, or None."""
        st = self.state(now)
        if st is FeedState.LIVE:
            return None
        if st is FeedState.UNCONFIGURED:
            return self.off_reason or "set " + ", ".join(self.spec.requires_env)
        if st is FeedState.DEAD:
            if self.consecutive_failures and self.error_kind:
                # Why it exited, when the collector said so before it did.
                last = f"{self.error_kind.value}: {self.error_detail}" if self.error_detail else self.error_kind.value
                return f"collector task exited · {last}"
            return "collector task exited"
        if st is FeedState.PENDING:
            if self.last_attempt_at is None:
                return "not yet polled"
            if self.consecutive_failures and self.error_kind:
                # Polled and failing: say how, not "not yet polled".
                last = f"{self.error_kind.value}: {self.error_detail}" if self.error_detail else self.error_kind.value
                return f"no success yet · {last}"
            return "no successful poll yet"
        if st is FeedState.STALE:
            if self.source_epoch is None:
                return "upstream gave no timestamp"
            if self.source_epoch - now > CLOCK_SKEW_S:
                return "clock_skew"
            return "upstream data older than its stale bound"
        kind = self.error_kind.value if self.error_kind else "unknown"
        return f"{kind}: {self.error_detail}" if self.error_detail else kind

    def snapshot(self, now: float) -> dict:
        age = None if self.last_success_at is None else max(0.0, now - self.last_success_at)
        return {
            "feed": self.spec.id,
            "state": self.state(now).value,
            "reason": self.reason(now),
            "configured": self.configured,
            "source": self.source,
            "fallback_from": self.fallback_from,
            "synthetic": self.synthetic,
            "last_attempt_at": self.last_attempt_at,
            "last_success_at": self.last_success_at,
            "source_epoch": self.source_epoch,
            "freshness": self.freshness(now),
            "age_s": None if age is None else round(age, 1),
            "consecutive_failures": self.consecutive_failures,
            "error_kind": self.error_kind.value if self.error_kind else None,
            "error_detail": self.error_detail,
            # The count from the last SUCCESS, named so: /health reports what
            # happened, and a bare "count" here would read as a current reading.
            "last_success_count": self.count,
            "parts": self.parts,
        }


TRACKERS: dict[str, FeedTracker] = {fid: FeedTracker(spec) for fid, spec in REGISTRY.items()}


def tracker(feed_id: str) -> FeedTracker:
    return TRACKERS[feed_id]


# ── Per-region currency ──────────────────────────────────────────────────────
#
# A live feed says the collector is getting data; it does not say from WHERE.
# One AIS socket delivering the western Mediterranean read LIVE over a Gulf with
# no receivers (FINDINGS.md, Roadmap > Phase 2 > per-region currency). A region
# is a sub-tracker with the same clocks and the same vocabulary: it is told of
# a success only when an accepted position falls inside its box, its upstream
# clock is that position's own time, and it is never shown better than its feed.


@dataclass(frozen=True)
class RegionSpec(FeedSpec):
    feed: str = ""
    key: str = ""
    name: str = ""
    # lat_min, lat_max, lon_min, lon_max. First match wins, in registry order.
    bbox: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)
    # "ao": counted in the headline. "control": outside the AO, subscribed so a
    # silent AO region can be read against traffic in the same connection.
    # "outside": neither.
    role: str = "outside"
    # A measured reason this region cannot be seen, stated beside its silence.
    known_gap: str | None = None


def _region(key: str, name: str, bbox, role: str, known_gap: str | None = None) -> RegionSpec:
    feed = REGISTRY["vessels"]
    return RegionSpec(
        id=f"vessels:{key}", cadence_s=feed.cadence_s,
        # The feed's own bounds. Measured 2026-09-26 over 30 min: the slowest
        # region that delivered anything was silent for 21.4 s at most (Black
        # Sea), a 5.6x margin under 120 s. Re-set per region from a week of
        # per-region heartbeat rows.
        stale_after_s=feed.stale_after_s, max_stale_s=feed.max_stale_s, requires_env=(),
        silence_means="no vessels here is state 2 unless the region is live",
        feed="vessels", key=key, name=name, bbox=bbox, role=role, known_gap=known_gap,
    )


# Every sea the subscription reaches, first match wins. Known gaps are
# MEASURED (FINDINGS.md, Measured facts, "AIS by region"): zero positions in
# 30 min while the western Mediterranean delivered 466 a minute on the same socket.
REGIONS: dict[str, RegionSpec] = {s.id: s for s in (
    _region("gulf", "Persian Gulf & Hormuz", (23.5, 30.5, 47.5, 57.0), "ao",
            known_gap="no free-tier receivers (C30; re-measured 2026-09-26)"),
    _region("oman_arabia", "Gulf of Oman & Arabian Sea", (10.0, 26.0, 51.0, 65.0), "ao",
            known_gap="no free-tier receivers (measured 2026-09-26)"),
    _region("red_sea", "Red Sea & Gulf of Aden", (10.0, 30.0, 32.0, 51.0), "ao",
            known_gap="thin coverage: 1 position in 30 min, off Djibouti (2026-09-26)"),
    _region("levant", "Levant", (30.0, 37.0, 32.0, 37.0), "ao"),
    _region("e_med", "Aegean & E. Mediterranean", (30.0, 41.0, 25.0, 32.0), "outside"),
    _region("black_sea", "Black Sea", (40.5, 45.0, 27.0, 42.0), "outside"),
    _region("caspian", "Caspian Sea", (36.0, 45.0, 46.0, 55.0), "outside",
            known_gap="no free-tier receivers (measured 2026-09-26)"),
    _region("wc_med", "W. & C. Mediterranean", (25.0, 45.0, -5.0, 25.0), "control"),
)}


def region_of(feed_id: str, lat: float, lon: float) -> str | None:
    """The key of the first region of `feed_id` whose box holds the point, or None."""
    for spec in REGIONS.values():
        a, b, c, d = spec.bbox
        if spec.feed == feed_id and a <= lat <= b and c <= lon <= d:
            return spec.key
    return None


class RegionTracker(FeedTracker):
    """One region of a feed. Never better than its feed: if the feed is
    retrying, every region reads retrying, whatever its own clocks say."""

    def feed(self) -> FeedTracker:
        return TRACKERS[self.spec.feed]

    def arm(self, now: float) -> None:
        """The subscription covering this region is open: silence counts from here."""
        if self.first_attempt_at is None:
            self.first_attempt_at = now

    def saw(self, now: float, observed_at: float | None) -> None:
        """One accepted position inside this region. The upstream clock is the
        newest message's own time; a message without one leaves the last
        upstream time in place and never lends it the receipt time."""
        self._attempt(now)
        self.window_successes += 1
        self.last_success_at = now
        if observed_at is not None:
            self.source_epoch = observed_at
        self.error_kind = ErrorKind.OK

    def state(self, now: float) -> FeedState:
        parent = self.feed().state(now)
        if parent is FeedState.UNCONFIGURED:
            return parent
        return worst([parent, super().state(now)])

    def reason(self, now: float) -> str | None:
        st = self.state(now)
        if st is FeedState.LIVE:
            return None
        parent = self.feed()
        if st is parent.state(now):
            why = f"feed {st.value}: {parent.reason(now)}"
        elif st is FeedState.STALE:
            why = super().reason(now)
        elif self.last_success_at is None:
            armed = "" if self.first_attempt_at is None else f" in {now - self.first_attempt_at:.0f} s"
            why = f"no position here since the subscription opened{armed}"
        else:
            why = f"no position here for {now - self.last_success_at:.0f} s"
        return f"{why} · known gap: {self.spec.known_gap}" if self.spec.known_gap else why

    def region_snapshot(self, now: float, count: int) -> dict:
        st = self.state(now)
        age = None if self.last_success_at is None else max(0.0, now - self.last_success_at)
        return {
            "id": self.spec.id,
            "key": self.spec.key,
            "name": self.spec.name,
            "bbox": list(self.spec.bbox),
            "role": self.spec.role,
            "state": st.value,
            "reason": self.reason(now),
            "last_success_at": self.last_success_at,
            "source_epoch": self.source_epoch,
            "age_s": None if age is None else round(age, 1),
            "freshness": self.freshness(now),
            # Same rule as the envelope: no number the monitor cannot vouch for.
            "count": count if st in (FeedState.LIVE, FeedState.STALE) else None,
            "known_gap": self.spec.known_gap,
        }


REGION_TRACKERS: dict[str, RegionTracker] = {rid: RegionTracker(spec) for rid, spec in REGIONS.items()}


def regions(feed_id: str) -> list[RegionTracker]:
    """The region trackers of `feed_id`, in registry order."""
    return [t for t in REGION_TRACKERS.values() if t.spec.feed == feed_id]


def all_trackers() -> dict[str, FeedTracker]:
    """Every feed, plus the regions of feeds that are registered: what the
    flusher writes and what /health/history can be asked about."""
    out: dict[str, FeedTracker] = dict(TRACKERS)
    out.update({rid: t for rid, t in REGION_TRACKERS.items() if t.spec.feed in TRACKERS})
    return out


def envelope(feed_id: str, items: list, now: float) -> dict:
    """The one response shape for a tracking route (FINDINGS.md decision 4).

    `count` is None unless the feed is live or stale: a number the monitor
    cannot vouch for is not printed. Every zero is `unproven` — `absent` is
    reserved until the control ring can earn it.
    """
    t = TRACKERS[feed_id]
    snap = t.snapshot(now)
    state = FeedState(snap["state"])
    count = len(items) if state in (FeedState.LIVE, FeedState.STALE) else None
    out = {
        "schema_version": 1,
        "feed": feed_id,
        "state": snap["state"],
        "reason": snap["reason"],
        "source": snap["source"],
        "fallback_from": snap["fallback_from"],
        "synthetic": snap["synthetic"],
        "source_epoch": snap["source_epoch"],
        "last_success_at": snap["last_success_at"],
        "server_now": now,
        "age_s": snap["age_s"],
        "freshness": snap["freshness"],
        "count": count,
        "verdict": "present" if count else "unproven",
        "items": items,
    }
    regs = regions(feed_id)
    if regs:
        # Counted from the rows actually served, by the region each row was
        # assigned when its position was accepted.
        out["regions"] = [
            r.region_snapshot(now, sum(1 for i in items if i.get("region") == r.spec.key))
            for r in regs
        ]
    return out


def health(now: float, process_started_at: float) -> dict:
    """GET /health. Always answered from memory, so it works with the database down.
    Regions are listed under their feed and stay out of `worst`: a dark region
    is a coverage fact, not a failing collector."""
    feeds = {fid: t.snapshot(now) for fid, t in TRACKERS.items()}
    for fid, snap in feeds.items():
        regs = regions(fid)
        if regs:
            snap["regions"] = [
                {k: v for k, v in r.region_snapshot(now, 0).items() if k != "count"} for r in regs
            ]
    configured = [FeedState(f["state"]) for f in feeds.values() if f["configured"]]
    w = worst(configured)
    return {
        "checked_at": now,
        "process_started_at": process_started_at,
        "synthetic": any(t.synthetic for t in TRACKERS.values()),
        "worst": w.value if w else None,
        "feeds": feeds,
        "not_collected": [
            {"feed": t.spec.id, "missing_env": [] if t.off_reason else list(t.spec.requires_env),
             "reason": t.reason(now)}
            for t in TRACKERS.values() if not t.configured
        ],
    }
