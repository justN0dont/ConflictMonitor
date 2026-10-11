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


class Corroboration(BaseModel):
    """Who said it, counted once per outlet (services/corroboration.py).

    Every count is a floor bounded by what event_reports holds. `outlets`
    collapses a Telegram forward onto its origin and `x_name` onto `name`;
    a pasted repost still counts as an outlet. `unrecorded` is the reports
    the merge destroyed before the table existed: 0 on anything since.
    """
    reports: int
    channels: int
    outlets: int
    unrecorded: int
    # Other events the link rule reads as this incident (services/
    # linkrule.py): separate rows, each with its own text and counts, never
    # merged. A floor like the rest, and a claim at the rule's measured
    # precision, which the chip's title states.
    linked: int = 0


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
    # The words in raw_text that spell location_name. "" means something looked
    # and found none — the place is not quotable from this text, which is not
    # the same as the place being invented. null means nothing has looked yet.
    # Consumers must not render "" as a quote, and must not read it as a
    # warning about the location either; models.py carries the full contract.
    evidence_span: str | None = None
    # null only when nothing computed it — the rows were not read. It is not
    # "no corroboration": that is reports=1, outlets=1. Every route and every
    # WebSocket push goes through corroboration.read_events, so null should
    # not leave this process; the UI falls back to report_count if it does.
    corroboration: Corroboration | None = None

    model_config = {"from_attributes": True}


class EventReportRead(BaseModel):
    """One report under an event: the row the corroboration counts, with the
    text it was written from (models.EventReport)."""
    id: int
    source: str
    channel: str
    # The outlet whose words these are (services/corroboration.outlet over the
    # origin): the key the chip counts by, served so the list groups the same
    # way and no second rule grows in the frontend.
    outlet: str
    # null: this row never recorded Telegram's forward header (RSS, the
    # archive, demo). "": Telegram said this message is not a forward.
    # Anything else: a forward, naming its origin as far as the header allowed.
    forwarded_from: str | None
    raw_text: str
    summary: str
    source_url: str
    telegram_message_id: int | None
    # This report's own stated toll, beside the text that stated it. A merged
    # report's count lives here and nowhere else (dedup.merge_duplicate).
    killed_reported: int | None
    extraction_status: str | None
    reported_at: datetime
    ingested_at: datetime


class EventLinkRead(BaseModel):
    """One event the link rule reads as the same incident as the one asked
    about, with what the rule saw (models.EventLink)."""
    event: EventRead
    rule: str
    score: float
    place: str
    towns: str
    # Minutes from the event asked about to this one: negative when it came
    # first.
    dt_min: float
    km: float | None
    created_at: datetime


class EventWS(BaseModel):
    """Payload pushed over WebSocket."""
    type: str = "new_event"
    event: EventRead
