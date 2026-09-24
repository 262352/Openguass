from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


@dataclass(frozen=True)
class Settings:
    api_key: str
    model: str = "deepseek-flash"
    base_url: str = "https://api.deepseek.com"
    timeout_seconds: float = 45.0
    max_retries: int = 3
    postgres_host: str = "127.0.0.1"
    postgres_port: int = 5432
    postgres_database: str = "tpcc"
    postgres_user: str = "andromeda_bench"
    postgres_password: str = ""

    @property
    def masked_key(self) -> str:
        return f"{self.api_key[:3]}…{self.api_key[-4:]}" if len(self.api_key) >= 8 else "***"


def load_settings(env_file: Path | None = None) -> Settings:
    _load_dotenv(env_file or ROOT / ".env")
    api_key = os.getenv("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError(
            "DEEPSEEK_API_KEY is missing. Copy .env.example to .env and set it, "
            "or export the variable before starting Andromeda."
        )
    return Settings(
        api_key=api_key,
        model=os.getenv("DEEPSEEK_MODEL", "deepseek-flash"),
        base_url=os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
        postgres_host=os.getenv("PGHOST", "127.0.0.1"),
        postgres_port=int(os.getenv("PGPORT", "5432")),
        postgres_database=os.getenv("PGDATABASE", "tpcc"),
        postgres_user=os.getenv("PGUSER", "andromeda_bench"),
        postgres_password=os.getenv("PGPASSWORD") or os.getenv("POSTGRES_BENCH_PASSWORD", ""),
    )
