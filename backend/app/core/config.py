"""Environment-based configuration. No secrets are hard-coded; SECRET_KEY must be provided."""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _list(name: str, default: str) -> list[str]:
    return [x.strip() for x in os.environ.get(name, default).split(",") if x.strip()]


@dataclass
class Settings:
    database_url: str = field(default_factory=lambda: os.environ.get("DATABASE_URL", "sqlite:///./stocksense.db"))
    secret_key: str = field(default_factory=lambda: os.environ.get("SECRET_KEY", ""))
    environment: str = field(default_factory=lambda: os.environ.get("ENVIRONMENT", "development"))
    access_token_minutes: int = field(default_factory=lambda: int(os.environ.get("ACCESS_TOKEN_MINUTES", "720")))
    cors_origins: list[str] = field(default_factory=lambda: _list("CORS_ORIGINS", "http://localhost:5173"))
    redis_url: str = field(default_factory=lambda: os.environ.get("REDIS_URL", "redis://localhost:6379/0"))
    llm_provider: str = field(default_factory=lambda: os.environ.get("LLM_PROVIDER", ""))
    llm_api_key: str = field(default_factory=lambda: os.environ.get("LLM_API_KEY", ""))

    def validate(self) -> None:
        if len(self.secret_key) < 32:
            raise RuntimeError("SECRET_KEY must be set to a random string of at least 32 characters "
                               "(e.g. `python -c \"import secrets;print(secrets.token_urlsafe(48))\"`).")


def get_settings() -> Settings:
    return Settings()
