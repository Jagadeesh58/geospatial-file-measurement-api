import os
from dataclasses import dataclass
from functools import lru_cache

MEBIBYTE = 1024 * 1024


@dataclass(frozen=True)
class Settings:
    database_url: str
    max_upload_bytes: int
    max_zip_uncompressed_bytes: int
    max_zip_members: int


@lru_cache
def get_settings() -> Settings:
    return Settings(
        database_url=os.environ.get("DATABASE_URL", "sqlite:///data/app.db"),
        max_upload_bytes=int(os.environ.get("MAX_UPLOAD_BYTES", 20 * MEBIBYTE)),
        max_zip_uncompressed_bytes=int(
            os.environ.get("MAX_ZIP_UNCOMPRESSED_BYTES", 100 * MEBIBYTE)
        ),
        max_zip_members=int(os.environ.get("MAX_ZIP_MEMBERS", 50)),
    )
