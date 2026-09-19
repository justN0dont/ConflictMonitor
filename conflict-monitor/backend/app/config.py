from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str = "postgresql+asyncpg://postgres:postgres@db:5432/conflict_monitor"
    telegram_api_id: int = 0
    telegram_api_hash: str = ""
    telegram_phone: str = ""
    anthropic_api_key: str = ""
    telegram_session: str = ""   # StringSession token written by auth.py (preferred over file session)
    telegram_channels: str = ""  # comma-separated channel usernames
    opensky_username: str = ""
    opensky_password: str = ""
    aisstream_api_key: str = ""
    cloudflare_radar_token: str = ""  # optional; empty means Radar is skipped, not failed
    demo_mode: bool = False
    # Only ingest events on or after this date (ISO YYYY-MM-DD)
    conflict_start_date: str = "2026-02-28"

    model_config = {"env_file": ".env", "extra": "ignore"}


settings = Settings()
