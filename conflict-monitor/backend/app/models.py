import datetime

from geoalchemy2 import Geometry
from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Event(Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String(50), default="telegram")
    channel_name: Mapped[str] = mapped_column(String(255), default="")
    raw_text: Mapped[str] = mapped_column(Text, default="")
    summary: Mapped[str] = mapped_column(Text, default="")
    event_type: Mapped[str] = mapped_column(String(50), default="military")
    # Nullable with no default: "we did not measure severity" has to be
    # representable. A default of 5 wrote the classifier's failure fallback to
    # disk as though it were a classification (86.3% of the archive is exactly
    # 5). NULL means unmeasured; it is not a low score and not a high one.
    severity: Mapped[int | None] = mapped_column(Integer, nullable=True)
    lat: Mapped[float | None] = mapped_column(Float, nullable=True)
    lon: Mapped[float | None] = mapped_column(Float, nullable=True)
    geometry: Mapped[str | None] = mapped_column(
        Geometry(geometry_type="POINT", srid=4326), nullable=True
    )
    timestamp: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    report_count: Mapped[int] = mapped_column(Integer, default=1)
    reporting_channels: Mapped[str] = mapped_column(Text, default="")
    source_reliability: Mapped[int | None] = mapped_column(Integer, nullable=True)
    location_name: Mapped[str] = mapped_column(Text, default="")
    telegram_message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_url: Mapped[str] = mapped_column(Text, default="")
    # People the classified message SAID were killed, copied from its text.
    # NULL on every row written before this column existed AND on every message
    # that states no count — "not stated" is the normal case and is not zero.
    # 0 means the message said nobody was killed. Unlike severity this is a
    # quantity a reader can check against raw_text in one second.
    # Only ever written from THIS row's own raw_text; a duplicate report's
    # count goes on that report's own EventReport row, never here (see
    # merge_duplicate).
    killed_reported: Mapped[int | None] = mapped_column(Integer, nullable=True)
    extraction_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # Which model produced this row ("qwen3:8b", "claude-haiku-4-5-20251001").
    # NULL on rows written before this column existed and on fallbacks that
    # never reached a model — without it an archive mixing backends cannot be
    # read, and a classifier evaluation cannot tell whose output it is scoring.
    extraction_model: Mapped[str | None] = mapped_column(String(64), nullable=True)
    is_geolocated: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    # How precisely the location resolved, and how. NULL on every row written
    # before these columns existed, so the archive stays distinguishable from
    # a row that was actually measured.
    geo_precision: Mapped[str | None] = mapped_column(String(32), nullable=True)
    geo_uncertainty_m: Mapped[int | None] = mapped_column(Integer, nullable=True)
    geo_method: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # The words in THIS row's raw_text that spell THIS row's location_name,
    # copied out of it verbatim. What killed_reported did for a death toll, this
    # does for a place: it makes the claim checkable in one second, because the
    # value is a substring of a column sitting next to it.
    #
    # THREE VALUES, AND ALL THREE ARE STATEMENTS:
    #   a non-empty string  the quote. `evidence_span IN raw_text` is true of
    #                       every row that has one, by construction.
    #   ''                  something looked, and this row carries no quotable
    #                       location claim.
    #   NULL                nothing has looked yet.
    # '' is load-bearing and is not "no value" — it is the string column's
    # version of `is_geolocated = false`, which this table already uses to mean
    # "we tried to place this and could not" as distinct from its own NULL. A
    # zero-length quote cannot occur, so '' is never a quote.
    #
    # '' COVERS TWO CASES, AND location_name SEPARATES THEM. Either a place was
    # named and this text does not spell it, or no place was named at all —
    # location_name is one of geocoder._NOT_A_PLACE, the classifier's own
    # "Unknown"/"various"/"IDF". evidence_span() applies that same test before
    # it searches, so a row whose could-not-tell sentinel happens to be a word
    # in its own message gets '' and not a span reading "unknown". The
    # discriminator is therefore a column-to-column check, the way a NULL
    # killed_reported is read against extraction_status.
    #
    # WRITE THAT CHECK AS `coalesce(location_name, '') IN (...)`, NOT
    # `location_name IN (...)`. A row can carry location_name SQL NULL — demo.py
    # never sets it — and in SQL `NULL IN (...)` evaluates to NULL, not false,
    # so the bare form silently drops every such row out of BOTH sides of the
    # split instead of filing it under "no place was named". A discriminator
    # that quietly answers neither is the same failure as a field that quietly
    # answers wrongly. The counts are
    # over two different populations: of the 2026-08-18 archive's 83,938 rows,
    # 38,319 name no place and the remaining 45,619 split 77.8% quote /
    # 22.2% ''.
    #
    # After the repair pass in run_startup_migrations there are no NULLs left.
    # That makes NULL a DETECTOR for a writer that forgot to call
    # classifier.evidence_span() — but only within the window between that
    # write and the next boot, because the pass itself fills the row in. It is
    # a signal to look at a running database with, not a guarantee, and no test
    # asserts on it. What actually keeps the writers honest is that every one
    # of them calls the one function — telegram, news_feeds, the events route,
    # demo and the OSINT importer, the last two wired in *this commit* after
    # they were found writing NULLs the pass then quietly absorbed. The pass is
    # not one-off: it re-runs for any row that reaches disk without a span.
    #
    # WHAT '' DOES NOT MEAN. It does not mean the model invented the place.
    # Measured on the 2026-08-18 archive, over the 45,619 place-naming rows
    # (NOT all 83,938 — the column is written on every row, the split is not):
    # 77.8% carry a quote and 22.2% carry ''. Of that 22.2%, a tenth
    # (2.3% of the place-naming rows) names the place perfectly well and fails only
    # because location_name carries a qualifier the text does not — "Qasamia
    # Bridge, southern Lebanon" against a message reading "the Qasamia Bridge in
    # southern Lebanon". The rest is real derivation: a flag emoji, an adjective
    # ("Israelis" -> Israel), another language ("صفد" -> Safed), or an inference
    # ("Beirut's southern suburb" -> Dahieh). '' says the location is not
    # QUOTABLE from this text. Which of those it is, this column does not know
    # and does not guess.
    #
    # It also does not certify that the classifier READ the quote: ~969 archive
    # quotes sit beyond offset 2000, past the window classify_message sends.
    # The claim is about the text, not about the model.
    #
    # DELIBERATELY NOT A PROVENANCE TAG. geo_method already records which branch
    # produced the COORDINATE (table-exact / directional-exact / partial /
    # nominatim); duplicating that here would give one row two columns that can
    # disagree about one fact. The link neither column covered is the one
    # upstream of both — whether location_name is grounded in the message at
    # all — and that is this one. The provenances the roadmap listed differ in
    # how they score against it rather than being told apart by it, which is the
    # stronger test: _regex_location_fallback finds its answer IN raw_text, so
    # it always produces a quote; the flag fallback reads an emoji, so it never
    # does; the LLM may do either. The column measures that difference instead
    # of accepting a self-declaration.
    #
    # NOT A BOOLEAN, though under a case-insensitive match the quote can differ
    # from location_name only in capitalisation (~3,864 archive rows, 10.9% of
    # quotes, do). The reason is the same one that put raw_text on EventReport:
    # a boolean is an assertion ABOUT evidence, and the evidence itself costs
    # one TEXT column and cannot be wrong about itself.
    #
    # NOT WIDENED TO A SENTENCE either. A window of surrounding words would read
    # better and would still be verbatim, but its width would be a constant
    # nobody measured, and it would not separate a quoted place from an invented
    # one any better than the bare occurrence does.
    evidence_span: Mapped[str | None] = mapped_column(Text, nullable=True)


class EventReport(Base):
    """One incoming report about an event, kept with the text it was written from.

    An `events` row is the monitor's account of what happened; a report row is
    one source's account of it. Before this table a merge kept the channel
    name and the higher severity and nothing else (`C10`), so the second
    report's wording, its permalink, its message id and its stated death toll
    were destroyed at the moment they arrived — 19,027 of them on the archive,
    and that text is not recoverable.

    `events.report_count` and `events.reporting_channels` are NOT derived from
    this table and are still written exactly as they were. They are not a
    competing account of the same fact: they are the only surviving record of
    those 19,027 reports, so `report_count - count(reports)` is the per-row
    size of what merge destroyed — 0 for anything ingested after this table
    existed, 213 on the one archive row that claims 214.
    """
    __tablename__ = "event_reports"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # ON DELETE CASCADE because two admin routes delete events outside the ORM
    # (events.py purge-old, and the raw-SQL dedup), and without it both would
    # fail on the FK the first time any report row existed. The cost is named
    # in FINDINGS rather than hidden: /admin/dedup keeps MIN(id) per
    # (source, raw_text) and the cascade then destroys the deleted twin's
    # report rows, which is C10 reappearing in a new place.
    event_id: Mapped[int] = mapped_column(
        ForeignKey("events.id", ondelete="CASCADE"), index=True
    )
    source: Mapped[str] = mapped_column(String(50), default="telegram")
    # "" is reachable: telegram.py writes it when a chat has neither username
    # nor title. It means Telegram named no channel — which is why the
    # distinct-channel count in merge_duplicate excludes it. Two unnamed
    # channels cannot be shown to be distinct from each other.
    channel: Mapped[str] = mapped_column(String(255), default="")
    raw_text: Mapped[str] = mapped_column(Text, default="")
    # Kept because check_duplicate scores Jaccard over `summary`, so without
    # the absorbed report's summary the merge decision is unreproducible.
    summary: Mapped[str] = mapped_column(Text, default="")
    source_url: Mapped[str] = mapped_column(Text, default="")
    # NULL means this report did not come from Telegram — never "the id was
    # lost". Every Telegram writer passes an int from Telethon, so
    # `telegram_message_id IS NULL` is exactly `source <> 'telegram'` and the
    # two are checkable against each other in one query.
    telegram_message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # People THIS report's text said were killed, copied from it. The column is
    # legal here in a way it is not on a merged event row, and for the reason
    # merge_duplicate gives at length: the count and the text that stated it
    # are in the same tuple, so the one-second check against raw_text is not
    # just true, it is local.
    #
    # It inherits ALL THREE of events.killed_reported's values, not two. 0
    # means the report said nobody was killed; NULL means it stated no count;
    # NULL also means "nothing ever extracted a count from this text". On
    # events that third value is a legacy corner — rows written before the
    # column existed. Here it is the common case: classifier._build_fallback
    # returns a dict with no killed_reported key at all, so `.get()` is None on
    # every failed classification, and with no working Anthropic key that was
    # every classification. The startup backfill copies a NULL out of every row
    # written before events.killed_reported existed too — 83,938 of them if the
    # 2026-08-18 archive is restored, which carries no such column.
    killed_reported: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Which of those three NULL means, for this report. Same values as
    # events.extraction_status ("ok", "parse_failed", "no_backend", "api_400",
    # "ollama_timeout", ...), written from the same classifier result as the
    # count beside it.
    #
    # It is one column and it is the reason the ambiguity above is bearable
    # rather than permanent. On an events row a NULL count can be read against
    # that row's own extraction_status. On a MERGED report row it cannot be
    # read against anything, even by joining back, because the event's status
    # describes a DIFFERENT report: measured on the dev DB before this column
    # existed, 98 of 159 report rows (61.6%) were killed_reported IS NULL on an
    # event whose extraction_status was not "ok", and not one of them could say
    # whether the source stated no toll or the classifier never ran.
    #
    # NULL here means no classifier status was recorded for this text at all:
    # the OSINT wave import in events.py classifies nothing, and the startup
    # backfill copies the event's own value, which is itself NULL on every row
    # written before events.extraction_status existed. NULL is therefore "I
    # cannot tell you why the count is missing" — still not "the source stated
    # no toll", which is the confusion that matters.
    extraction_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # When the source said it. Kept for one reason: a merged report's own
    # report-time is destroyed by the merge and is recoverable from nowhere
    # else — the event keeps the FIRST report's timestamp, and neither row
    # carries the second one's. This table exists to stop discarding facts
    # about incoming reports, and when a report was made is one of them. That
    # is the whole justification; "a future reader may want it" is not part of
    # it, and would not be enough.
    #
    # `ingested_at` is the other half of the pair — the same pair as
    # events.timestamp / events.created_at: when they said it against when we
    # wrote it, which for the startup backfill are months apart.
    #
    # Inherited caveat, not created here: news_feeds substitutes
    # datetime.now() for a missing or unparseable pubDate, so an RSS
    # reported_at is only as true as events.timestamp already is.
    reported_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    ingested_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ChannelCheckpoint(Base):
    """Stores the highest processed Telegram message_id per channel.
    On restart, backfill only fetches messages NEWER than this ID,
    so we never re-process old data or waste API credits.
    """
    __tablename__ = "channel_checkpoints"

    channel_name: Mapped[str] = mapped_column(String(255), primary_key=True)
    last_message_id: Mapped[int] = mapped_column(Integer, default=0)
    last_processed_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


# ── feed_health (docs/FINDINGS.md, Roadmap > Phase 2 > feed_health decisions) ──
# Facts are stored; state is derived at read time from the timestamps. The
# `state` columns below are a cache of the last computation, written so a human
# reading the table can see it, and no reader trusts them over the clocks.


class FeedHealth(Base):
    """One row per feed: the tracker's facts at the last flush."""

    __tablename__ = "feed_health"

    feed: Mapped[str] = mapped_column(String(64), primary_key=True)
    configured: Mapped[bool] = mapped_column(Boolean, default=True)
    state: Mapped[str] = mapped_column(String(32))
    source: Mapped[str | None] = mapped_column(Text, nullable=True)
    fallback_from: Mapped[str | None] = mapped_column(Text, nullable=True)
    synthetic: Mapped[bool] = mapped_column(Boolean, default=False)
    last_attempt_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_success_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    source_epoch: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    error_kind: Mapped[str | None] = mapped_column(String(32), nullable=True)
    error_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True))


class FeedTransition(Base):
    """A change of derived state, and one `process_start` row per feed at boot.

    `process_start` is what lets a reader see the monitor's OWN downtime: the
    last state before it is not extended across the gap.
    """

    __tablename__ = "feed_transition"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    feed: Mapped[str] = mapped_column(String(64))
    at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True))
    from_state: Mapped[str | None] = mapped_column(String(32), nullable=True)
    to_state: Mapped[str] = mapped_column(String(32))
    error_kind: Mapped[str | None] = mapped_column(String(32), nullable=True)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (Index("ix_feed_transition_feed_at", "feed", "at"),)


class FeedHeartbeat(Base):
    """One row per feed per flush (every 60 s): the state then, and what the
    collector did in that minute. A gap of more than 120 s between two rows
    reads as UNKNOWN - the monitor was not recording, so nothing may be drawn
    across it."""

    __tablename__ = "feed_heartbeat"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    feed: Mapped[str] = mapped_column(String(64))
    at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True))
    state: Mapped[str] = mapped_column(String(32))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    successes: Mapped[int] = mapped_column(Integer, default=0)
    # {"timeout": 2, "fetch_error": 1}: failures in this minute by error_kind.
    failures: Mapped[dict] = mapped_column(JSONB, default=dict)

    __table_args__ = (Index("ix_feed_heartbeat_feed_at", "feed", "at"),)

