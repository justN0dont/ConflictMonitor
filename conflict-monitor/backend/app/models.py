import datetime

from geoalchemy2 import Geometry
from sqlalchemy import Boolean, DateTime, Float, Integer, String, Text, func
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
