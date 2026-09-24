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
                  fallback_from: str | None = None, synthetic: bool = False) -> None:
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

    def failed(self, now: float, kind: ErrorKind, detail: str | None = None) -> None:
        if kind in SUCCESS_KINDS:
            raise ValueError(f"{kind} is not a failure")
        self._attempt(now)
        self.window_failures[kind.value] = self.window_failures.get(kind.value, 0) + 1
        self.consecutive_failures += 1
        self.error_kind = kind
        self.error_detail = clean_detail(detail)

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
            return "set " + ", ".join(self.spec.requires_env)
        if st is FeedState.DEAD:
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
        }


TRACKERS: dict[str, FeedTracker] = {fid: FeedTracker(spec) for fid, spec in REGISTRY.items()}


def tracker(feed_id: str) -> FeedTracker:
    return TRACKERS[feed_id]


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
    return {
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


def health(now: float, process_started_at: float) -> dict:
    """GET /health. Always answered from memory, so it works with the database down."""
    feeds = {fid: t.snapshot(now) for fid, t in TRACKERS.items()}
    configured = [FeedState(f["state"]) for f in feeds.values() if f["configured"]]
    w = worst(configured)
    return {
        "checked_at": now,
        "process_started_at": process_started_at,
        "synthetic": any(t.synthetic for t in TRACKERS.values()),
        "worst": w.value if w else None,
        "feeds": feeds,
        "not_collected": [
            {"feed": t.spec.id, "missing_env": list(t.spec.requires_env)}
            for t in TRACKERS.values() if not t.configured
        ],
    }
