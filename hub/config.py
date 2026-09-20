from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://hub:hub@localhost:5432/hub"
    verify_signatures: bool = False
    spike_threshold: int = 50
    spike_window_min: int = 10


settings = Settings()
