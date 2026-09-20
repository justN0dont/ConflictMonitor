from datetime import datetime

from pydantic import BaseModel


class EventCreate(BaseModel):
    source: str = "telegram"
    channel_name: str = ""
    raw_text: str = ""
    summary: str = ""
    event_type: str = "military"
    severity: int | None = None
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
    # null when severity was never measured — not 0, not 5. Consumers must
    # render the absence rather than substitute a number.
    severity: int | None
    # null when the message stated no death toll — the normal case, and not 0.
    # 0 means the message said nobody was killed.
    killed_reported: int | None = None
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
    extraction_status: str | None = None
    # null on rows classified before this column existed, and on fallbacks that
    # never reached a model — not an empty string.
    extraction_model: str | None = None
    is_geolocated: bool | None = None
    geo_precision: str | None = None
    geo_uncertainty_m: int | None = None
    geo_method: str | None = None

    model_config = {"from_attributes": True}


class EventWS(BaseModel):
    """Payload pushed over WebSocket."""
    type: str = "new_event"
    event: EventRead
