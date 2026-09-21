import datetime

from geoalchemy2 import Geometry
from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text, func
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
