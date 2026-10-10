from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings. Every field can be overridden with a GEO_-prefixed env var."""

    model_config = SettingsConfigDict(env_prefix="GEO_", env_file=".env", extra="ignore")

    data_dir: Path = Path("data")
    database_url: str = ""

    max_upload_bytes: int = 100 * 1024 * 1024
    max_uncompressed_bytes: int = 500 * 1024 * 1024
    max_archive_members: int = 1000
    max_compression_ratio: float = 100.0

    @property
    def upload_dir(self) -> Path:
        return self.data_dir / "uploads"

    @property
    def tmp_dir(self) -> Path:
        return self.data_dir / "tmp"

    @property
    def db_url(self) -> str:
        return self.database_url or f"sqlite:///{(self.data_dir / 'app.db').as_posix()}"


@lru_cache
def get_settings() -> Settings:
    return Settings()
