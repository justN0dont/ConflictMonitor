from datetime import datetime

from pydantic import BaseModel


class EventCreate(BaseModel):
    source: str = "telegram"
    channel_name: str = ""
    raw_text: str = ""
    summary: str = ""
    event_type: str = "military"
    severity: int = 5
    lat: float | None = None
    lon: float | None = None
    timestamp: datetime | None = None


class EventRead(BaseModel):
    id: int
    source: str
    channel_name: str
    raw_text: str
    summary: str
    event_type: str
    severity: int
    lat: float | None
    lon: float | None
    timestamp: datetime
    created_at: datetime
    report_count: int = 1
    reporting_channels: str = ""
    source_reliability: int | None = None
    location_name: str = ""
    telegram_message_id: int | None = None
    source_url: str = ""

    model_config = {"from_attributes": True}


class EventWS(BaseModel):
    """Payload pushed over WebSocket."""
    type: str = "new_event"
    event: EventRead
