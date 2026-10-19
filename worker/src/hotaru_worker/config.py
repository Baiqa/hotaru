import os
from dataclasses import dataclass
from pathlib import Path

import yaml
from dotenv import load_dotenv

PACKAGE_DIR = Path(__file__).resolve().parent


@dataclass
class Settings:
    database_url: str
    user_agent: str
    request_delay_seconds: float
    http_timeout_seconds: float
    sources_file: Path
    aliases_file: Path


def load() -> Settings:
    load_dotenv()

    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise RuntimeError(
            "DATABASE_URL не задан. Скопируйте worker/.env.example в worker/.env "
            "и впишите connection string из Supabase "
            "(Project Settings -> Database -> Connection string -> URI)."
        )

    contact_email = os.environ.get("CONTACT_EMAIL")
    if not contact_email:
        raise RuntimeError(
            "CONTACT_EMAIL не задан. Раздел 3.1 CLAUDE.md требует честный "
            "User-Agent с контактом — задайте CONTACT_EMAIL в worker/.env."
        )

    user_agent = f"HotaruNewsBot/0.1 (+mailto:{contact_email})"
    request_delay_seconds = float(os.environ.get("REQUEST_DELAY_SECONDS", "1.5"))
    http_timeout_seconds = float(os.environ.get("HTTP_TIMEOUT_SECONDS", "15"))
    sources_file = Path(os.environ.get("SOURCES_FILE", PACKAGE_DIR / "sources.yaml"))
    aliases_file = Path(os.environ.get("ALIASES_FILE", PACKAGE_DIR / "aliases_seed.yaml"))

    return Settings(
        database_url=database_url,
        user_agent=user_agent,
        request_delay_seconds=request_delay_seconds,
        http_timeout_seconds=http_timeout_seconds,
        sources_file=sources_file,
        aliases_file=aliases_file,
    )


def load_sources(path: Path) -> list[dict]:
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data["sources"]


def load_aliases(path: Path) -> list[dict]:
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data["entities"]
