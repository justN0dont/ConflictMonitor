"""feed_store.lane_buckets: the timeline's liveness lane as a pure function.

Phase 3 (docs/FINDINGS.md, "Promote the timeline into a feed-liveness lane"):
on a months-long axis a bucket with no heartbeat row must read as a hole, not
as the last state stretched across it - and a hole where rows were pruned is
`not_retained`, which is this monitor forgetting, not this monitor not looking.
"""

from app.feed_store import NOT_RETAINED, UNKNOWN, lane_buckets

H = 3600
T0 = 1_800_000_000  # a multiple of 3600


def _states(buckets):
    return [b["state"] for b in buckets]


def test_no_rows_is_unknown_everywhere():
    out = lane_buckets([], T0, T0 + 3 * H, H, retained_since=None, first_recorded=None)
    assert _states(out) == [UNKNOWN] * 3
    assert [b["at"] for b in out] == [T0, T0 + H, T0 + 2 * H]
    assert all(b["attempts"] == 0 and b["successes"] == 0 for b in out)


def test_a_bucket_reports_the_worst_minute_it_holds():
    """One retrying minute inside an hour of live must show: a six-hour bucket
    that read "live" because most of its minutes were live would hide exactly
    the outage the lane exists to draw."""
    agg = [
        (T0, ["live"], 60, 60),
        (T0 + H, ["live", "retrying"], 60, 58),
        (T0 + 2 * H, ["unavailable", "retrying", "live"], 60, 10),
    ]
    out = lane_buckets(agg, T0, T0 + 3 * H, H, retained_since=None, first_recorded=None)
    assert _states(out) == ["live", "retrying", "unavailable"]
    assert [(b["attempts"], b["successes"]) for b in out] == [(60, 60), (60, 58), (60, 10)]


def test_unconfigured_alone_stays_unconfigured_and_never_hides_a_ranked_state():
    agg = [(T0, ["unconfigured"], 0, 0), (T0 + H, ["unconfigured", "live"], 5, 5)]
    out = lane_buckets(agg, T0, T0 + 2 * H, H, retained_since=None, first_recorded=None)
    assert _states(out) == ["unconfigured", "live"]


def test_pruned_rows_read_not_retained_only_between_first_record_and_retention():
    """Three empty stretches, three different truths: before the feed existed
    (unknown), inside retention but pruned (not_retained), and inside retention
    with nothing written (unknown - the monitor was down)."""
    first_recorded = T0 + H        # the feed's first process_start
    retained_since = T0 + 3 * H    # rows before this have been pruned
    out = lane_buckets([], T0, T0 + 5 * H, H, retained_since, first_recorded)
    assert _states(out) == [
        UNKNOWN,        # [T0, T0+H): ends at first_recorded, not after it
        NOT_RETAINED,   # [T0+H, T0+2H)
        NOT_RETAINED,   # [T0+2H, T0+3H): ends at retained_since, still pruned
        UNKNOWN,        # inside retention, no row: nobody was recording
        UNKNOWN,
    ]


def test_a_row_wins_over_the_not_retained_label():
    """The prune runs hourly, so rows older than the retention window can still
    exist; a bucket that has rows says what they say."""
    out = lane_buckets([(T0, ["live"], 60, 60)], T0, T0 + 2 * H, H,
                       retained_since=T0 + 5 * H, first_recorded=T0 - H)
    assert _states(out) == ["live", NOT_RETAINED]


def test_first_bucket_is_aligned_down_and_the_last_covers_before():
    out = lane_buckets([], T0 + 100, T0 + H + 1, H, None, None)
    assert [b["at"] for b in out] == [T0, T0 + H]
