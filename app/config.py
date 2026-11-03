from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    DATABASE_URL: str = "postgresql+psycopg://postgres:postgres@localhost:5432/property_assistant"
    TEST_DATABASE_URL: str = (
        "postgresql+psycopg://postgres:postgres@localhost:5432/property_assistant_test"
    )
    INVENTORY_PATH: str = "/app/data/property_inventory.csv"
    ADMIN_TOKEN: str | None = None
    MAX_REJECT_RATIO: float = 0.25
    PRICE_AROUND_PCT: float = 0.10
    FOLLOWUP_SKIP_THRESHOLD: int = 5
    FLOOR_BAND_LOW_MAX: int = 5
    FLOOR_BAND_MID_MIN: int = 6
    FLOOR_BAND_MID_MAX: int = 15
    FLOOR_BAND_HIGH_MIN: int = 16

    # LLM Settings
    GEMINI_API_KEY: str | None = None
    GEMINI_MODEL: str = "gemini-3.5-flash-lite"
    GEMINI_NLU_MODEL: str | None = None
    GEMINI_RESPONDER_MODEL: str | None = None
    LLM_TIMEOUT_SECONDS: int = 20
    LLM_MAX_RETRIES: int = 2
    NLU_THINKING_LEVEL: str = "minimal"
    RESPONDER_THINKING_LEVEL: str = "minimal"
    MAX_MESSAGE_CHARS: int = 1000


@lru_cache
def get_settings() -> Settings:
    return Settings()
