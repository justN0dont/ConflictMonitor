import datetime

from geoalchemy2 import Geometry
from sqlalchemy import DateTime, Float, Integer, String, Text, func
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
    severity: Mapped[int] = mapped_column(Integer, default=5)
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
