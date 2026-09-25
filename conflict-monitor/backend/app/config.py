from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str = "postgresql+asyncpg://postgres:postgres@db:5432/conflict_monitor"
    telegram_api_id: int = 0
    telegram_api_hash: str = ""
    telegram_phone: str = ""
    anthropic_api_key: str = ""
    # Which LLM classifies messages: "ollama" | "anthropic" | "none".
    # Never implicit: if the selected backend is down the classification fails
    # visibly rather than switching to the other one, because an archive whose
    # rows came from an unrecorded model cannot be interpreted afterwards.
    llm_backend: str = "ollama"
    # host.docker.internal is REQUIRED from inside the container: the backend
    # runs in Docker, Ollama runs on the Windows host. Measured from the
    # container — neither 172.17.0.1 nor localhost reaches it.
    ollama_url: str = "http://host.docker.internal:11434"
    ollama_model: str = "qwen3:8b"
    telegram_session: str = ""   # StringSession token written by auth.py (preferred over file session)
    telegram_channels: str = ""  # comma-separated channel usernames
    opensky_username: str = ""
    opensky_password: str = ""
    aisstream_api_key: str = ""
    cloudflare_radar_token: str = ""  # optional; empty means Radar is skipped, not failed
    demo_mode: bool = False
    # Shared secret for /events/admin/* (sent as X-Admin-Token). Empty means
    # the admin routes answer 503: unset closes them, it never opens them.
    admin_token: str = ""
    # Comma-separated browser origins allowed to read the API and open the
    # websocket. Add the public frontend URL here when deploying.
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"
    # Only ingest events on or after this date (ISO YYYY-MM-DD)
    conflict_start_date: str = "2026-02-28"

    model_config = {"env_file": ".env", "extra": "ignore"}


settings = Settings()
