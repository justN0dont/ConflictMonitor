"""Demo mode must not pass off invented events as real reporting (C69).

demo.py generates strike, diplomatic, economic and cyber rows at real places.
Until C69 was fixed it signed them with the names of real OSINT outlets, most
of them channels this monitor actually reads, and put confirmations in the
mouths of real militaries. One row cut out of a screenshot was
indistinguishable from real reporting. These tests pin the two properties that
make it safe: the bylines are fictional, and every row says what it is in its
own text.

The README's channel table cannot be checked from here: the documented test
command mounts only backend/ into the container. The registry below is the list
that table documents, and the one the live monitor reads.
"""

import re

from app.seed_channels import CHANNEL_REGISTRY
from app.services import demo


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


_REAL = {_norm(k) for k in CHANNEL_REGISTRY} | {
    _norm(meta["display_name"]) for meta in CHANNEL_REGISTRY.values()
}


def test_every_demo_byline_is_visibly_fictional():
    """C69: the form alone says "not a real outlet", including outlets no registry lists."""
    assert demo.CHANNELS, "demo mode needs at least one byline"
    assert [c for c in demo.CHANNELS if not re.fullmatch(r"DEMO-CH-\d{2}", c)] == []


def test_no_demo_byline_matches_a_channel_the_monitor_reads():
    """C69: the byline on a fabricated row is never a name in CHANNEL_REGISTRY.

    Ignores case and punctuation and matches in both directions, so "IranIntl"
    collides with "IranIntl_En" and "CIG" with "CIG_telegram". Checked against
    keys and display names both, since the UI shows either.
    """
    clashes = [
        c for c in demo.CHANNELS
        if any(_norm(c) in real or real in _norm(c) for real in _REAL)
    ]
    assert clashes == []


def test_every_generated_row_says_it_is_synthetic():
    """C69: the marker is in the row's own summary AND raw_text, on every draw.

    All three demo writers (history seed, live generator, burst) build their
    rows with _gen_event, so this covers each of them. raw_text matters as much
    as summary: it is what a report row and any export carry.
    """
    for _ in range(2000):
        row = demo._gen_event()
        assert row["source"] == "demo"
        assert row["summary"].startswith(demo.SYNTHETIC_MARKER), row["summary"]
        assert row["raw_text"].startswith(demo.SYNTHETIC_MARKER), row["raw_text"]
