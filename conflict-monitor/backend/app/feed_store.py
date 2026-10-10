"""feed_health persistence: the trackers' facts, written to Postgres once a minute.

Decision 3 (docs/FINDINGS.md, Roadmap > Phase 2 > feed_health decisions):
history is TRANSITION rows plus per-minute HEARTBEAT rows, not one row per poll.

- At boot, one `process_start` transition per feed. Between the backend dying
  and this row, nothing was recording; a reader must not extend the last state
  across that gap.
- Every FLUSH_INTERVAL_S: a heartbeat per feed (the state, and what the
  collector did in that minute), a transition when the derived state changed,
  and an upsert of the feed's current facts into `feed_health`.
- Reading: `liveness_timeline` turns heartbeats into segments, and any gap
  longer than GAP_UNKNOWN_S between rows becomes an explicit `unknown` segment.

The flusher is the only writer and the trackers are its only input. A database
error is logged and the next flush tries again; it is never raised into a
poller, and /health keeps answering from memory throughout.
"""

import asyncio
import datetime
import logging
import math
import time

from sqlalchemy import delete, extract, func, select
from sqlalchemy.dialects.postgresql import insert

from app import feeds
from app.models import FeedHealth, FeedHeartbeat, FeedTransition

logger = logging.getLogger(__name__)

FLUSH_INTERVAL_S = 60
# A heartbeat gap longer than this reads as unknown: two missed flushes.
GAP_UNKNOWN_S = 120
# Heartbeat retention. Unmeasured; the first week of rows will say what it costs.
RETENTION_DAYS = 30
PRUNE_EVERY_FLUSHES = 60

PROCESS_START = "process_start"
UNKNOWN = "unknown"


def _ts(epoch: float | None) -> datetime.datetime | None:
    return None if epoch is None else datetime.datetime.fromtimestamp(epoch, tz=datetime.timezone.utc)


async def record_boot(session_factory, now: float) -> None:
    """One process_start transition per registered feed and region."""
    async with session_factory() as session:
        for fid in feeds.all_trackers():
            session.add(FeedTransition(feed=fid, at=_ts(now), from_state=None, to_state=PROCESS_START))
        await session.commit()


async def flush(session_factory, now: float, last_states: dict[str, str]) -> dict[str, str]:
    """Write one heartbeat per feed, any transitions, and the current facts.

    `last_states` is the state each feed had at the previous flush (or the
    boot marker); returns the new map. Window counters are drained only after
    the commit succeeds, so a failed flush loses nothing: the next heartbeat
    carries both minutes.
    """
    at = _ts(now)
    new_states: dict[str, str] = {}
    drained: list[feeds.FeedTracker] = []
    async with session_factory() as session:
        for fid, t in feeds.all_trackers().items():
            state = t.state(now).value
            new_states[fid] = state
            prev = last_states.get(fid)
            if state != prev:
                session.add(FeedTransition(
                    feed=fid, at=at, from_state=prev, to_state=state,
                    error_kind=t.error_kind.value if t.error_kind else None,
                    detail=t.error_detail,
                ))
            session.add(FeedHeartbeat(
                feed=fid, at=at, state=state,
                attempts=t.window_attempts, successes=t.window_successes,
                failures=dict(t.window_failures),
            ))
            row = {
                "feed": fid,
                # From the state, not the field: a region is unconfigured when its feed is.
                "configured": state != feeds.FeedState.UNCONFIGURED.value,
                "state": state,
                "source": t.source,
                "fallback_from": t.fallback_from,
                "synthetic": t.synthetic,
                "last_attempt_at": _ts(t.last_attempt_at),
                "last_success_at": _ts(t.last_success_at),
                "source_epoch": _ts(t.source_epoch),
                "consecutive_failures": t.consecutive_failures,
                "error_kind": t.error_kind.value if t.error_kind else None,
                "error_detail": t.error_detail,
                "updated_at": at,
            }
            stmt = insert(FeedHealth).values(**row)
            await session.execute(stmt.on_conflict_do_update(
                index_elements=[FeedHealth.feed],
                set_={k: stmt.excluded[k] for k in row if k != "feed"},
            ))
            drained.append(t)
        await session.commit()
    for t in drained:
        t.drain_window()
    return new_states


async def prune(session_factory, now: float) -> int:
    """Delete heartbeat rows older than RETENTION_DAYS. Transitions are kept."""
    cutoff = _ts(now - RETENTION_DAYS * 86400)
    async with session_factory() as session:
        result = await session.execute(delete(FeedHeartbeat).where(FeedHeartbeat.at < cutoff))
        await session.commit()
        return result.rowcount or 0


async def start_feed_flusher(session_factory, sleep=asyncio.sleep, clock=time.time):
    """The only writer. Boot marker, then a flush every FLUSH_INTERVAL_S."""
    last_states = {fid: PROCESS_START for fid in feeds.all_trackers()}
    try:
        await record_boot(session_factory, clock())
    except Exception as e:
        logger.error("feed_health: could not record process start: %s", e)
    flushes = 0
    while True:
        await sleep(FLUSH_INTERVAL_S)
        try:
            last_states = await flush(session_factory, clock(), last_states)
            flushes += 1
            if flushes % PRUNE_EVERY_FLUSHES == 0:
                await prune(session_factory, clock())
        except asyncio.CancelledError:
            raise
        except Exception as e:
            # Never raised into a poller. The counters were not drained, so the
            # next flush carries this minute too.
            logger.error("feed_health: flush failed, will retry next minute: %s", e)


async def liveness_timeline(session, feed: str, since: float, until: float) -> list[dict]:
    """Segments [{start, end, state}] covering [since, until].

    A heartbeat at time t vouches for the state over the interval ending at t
    and starting at the previous heartbeat, provided the two are at most
    GAP_UNKNOWN_S apart. A longer gap - including before the first row and
    after the last - is an `unknown` segment: the monitor was not recording.
    Adjacent segments with the same state are merged.
    """
    rows = (await session.execute(
        select(FeedHeartbeat.at, FeedHeartbeat.state)
        .where(FeedHeartbeat.feed == feed, FeedHeartbeat.at >= _ts(since - GAP_UNKNOWN_S),
               FeedHeartbeat.at <= _ts(until))
        .order_by(FeedHeartbeat.at)
    )).all()

    segments: list[dict] = []

    def add(start: float, end: float, state: str) -> None:
        start, end = max(start, since), min(end, until)
        if end <= start:
            return
        if segments and segments[-1]["state"] == state and segments[-1]["end"] >= start:
            segments[-1]["end"] = end
        else:
            segments.append({"start": start, "end": end, "state": state})

    prev_t = None
    for at, state in rows:
        t = at.timestamp()
        if prev_t is None:
            # Before the first heartbeat in range nothing vouches for anything,
            # except the flush interval the first row itself covers.
            add(since, t - FLUSH_INTERVAL_S, UNKNOWN)
            add(t - FLUSH_INTERVAL_S, t, state)
        elif t - prev_t > GAP_UNKNOWN_S:
            add(prev_t, t - FLUSH_INTERVAL_S, UNKNOWN)
            add(t - FLUSH_INTERVAL_S, t, state)
        else:
            add(prev_t, t, state)
        prev_t = t
    if prev_t is None:
        add(since, until, UNKNOWN)
    elif until - prev_t > GAP_UNKNOWN_S:
        add(prev_t, until, UNKNOWN)
    else:
        # The last row vouches until the next flush is due; not beyond it.
        add(prev_t, until, segments[-1]["state"] if segments else UNKNOWN)
    return segments


# A lane bucket's state when heartbeat rows there were pruned (RETENTION_DAYS):
# this monitor forgetting, which is not the same as this monitor not looking.
NOT_RETAINED = "not_retained"


def lane_buckets(
    agg,
    after: float,
    before: float,
    bucket_s: int,
    retained_since: float | None,
    first_recorded: float | None,
) -> list[dict]:
    """Dense liveness buckets over [after, before): one per `bucket_s`.

    `agg` is `(bucket_start_epoch, states, attempts, successes)` per bucket,
    from heartbeat rows grouped by bucket (liveness_lane does the SQL). Within
    a bucket the state is the WORST its heartbeats recorded (feeds.worst), so
    one failing minute inside a six-hour bucket shows; a bucket whose only
    state is `unconfigured` stays `unconfigured`.

    A bucket with no heartbeat row is one of two things, told apart by two
    clocks the caller supplies:
      - `not_retained`: it ends after the feed's first transition row
        (`first_recorded`, kept forever) and before `retained_since`, so rows
        were written there and have since been pruned;
      - `unknown` otherwise: nothing ever vouched for it - before this feed
        existed, or a gap in which the monitor was not recording.
    Neither is ever written as a feed state.

    The first bucket is aligned down to a multiple of `bucket_s`, so `at` can
    precede `after`; a caller drawing the lane clips it.
    """
    by_start = {int(b): (states, att, suc) for b, states, att, suc in agg}
    out: list[dict] = []
    start = math.floor(after / bucket_s) * bucket_s
    while start < before:
        end = start + bucket_s
        row = by_start.get(start)
        if row is None:
            pruned = (
                retained_since is not None and first_recorded is not None
                and end <= retained_since and end > first_recorded
            )
            out.append({"at": start, "state": NOT_RETAINED if pruned else UNKNOWN,
                        "attempts": 0, "successes": 0})
        else:
            states, att, suc = row
            w = feeds.worst(feeds.FeedState(s) for s in states)
            state = w.value if w is not None else feeds.FeedState.UNCONFIGURED.value
            out.append({"at": start, "state": state,
                        "attempts": int(att or 0), "successes": int(suc or 0)})
        start = end
    return out


async def liveness_lane(session, feed: str, after: float, before: float,
                        bucket_s: int, now: float) -> list[dict]:
    """`lane_buckets` over this feed's heartbeat rows, aggregated in SQL so a
    five-month axis costs one grouped scan and returns at most a few thousand
    rows, never a row per minute."""
    first_tr = (await session.execute(
        select(func.min(FeedTransition.at)).where(FeedTransition.feed == feed)
    )).scalar_one_or_none()
    first_recorded = first_tr.timestamp() if first_tr is not None else None
    bucket = (func.floor(extract("epoch", FeedHeartbeat.at) / bucket_s) * bucket_s).label("bucket")
    aligned = math.floor(after / bucket_s) * bucket_s
    rows = (await session.execute(
        select(
            bucket,
            func.array_agg(FeedHeartbeat.state.distinct()),
            func.sum(FeedHeartbeat.attempts),
            func.sum(FeedHeartbeat.successes),
        )
        .where(FeedHeartbeat.feed == feed,
               FeedHeartbeat.at >= _ts(aligned),
               FeedHeartbeat.at < _ts(before))
        .group_by(bucket)
        .order_by(bucket)
    )).all()
    return lane_buckets(rows, after, before, bucket_s,
                        retained_since=now - RETENTION_DAYS * 86400,
                        first_recorded=first_recorded)
