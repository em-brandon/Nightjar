from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://hub:hub@localhost:5432/hub"
    verify_signatures: bool = False
    spike_threshold: int = 50
    spike_window_min: int = 10
    probe_enabled: bool = True
    probe_interval_s: int = 60
    probe_timeout_s: float = 10.0


settings = Settings()
