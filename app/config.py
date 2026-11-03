from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    DATABASE_URL: str = (
        "postgresql+psycopg://postgres:postgres@localhost:5432/property_assistant"
    )
    TEST_DATABASE_URL: str = (
        "postgresql+psycopg://postgres:postgres@localhost:5432/property_assistant_test"
    )
    INVENTORY_PATH: str = "/app/data/property_inventory.csv"
    ADMIN_TOKEN: str | None = None
    MAX_REJECT_RATIO: float = 0.25


@lru_cache
def get_settings() -> Settings:
    return Settings()
