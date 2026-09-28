from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parent


def _load_dotenv() -> None:
    path = ROOT / ".env"
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'\"")
        if key:
            os.environ.setdefault(key, value)


def _positive_int(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc
    if value <= 0:
        raise RuntimeError(f"{name} must be greater than zero")
    return value


_load_dotenv()


@dataclass(frozen=True)
class Settings:
    api_key: str = os.getenv("DSH2API_KEY", "")
    host: str = os.getenv("DSH2API_HOST", "127.0.0.1")
    port: int = _positive_int("DSH2API_PORT", 18611)
    dsh_home: str = os.getenv("DSH_HOME", "")
    profile: str = os.getenv("DSH_PROFILE", "sdk")
    provider: str = os.getenv("DSH_PROVIDER", "deepseek-official")
    model: str = os.getenv("DSH_MODEL", "deepseek-v4-flash")
    reasoning_effort: str = os.getenv("DSH_REASONING_EFFORT", "")
    max_tokens: int = _positive_int("DSH_MAX_TOKENS", 8192)
    workspace: str = os.getenv("DSH_WORKSPACE", "")
    remote_url: str = os.getenv("DSH_REMOTE_URL", "").rstrip("/")
    remote_timeout: int = _positive_int("DSH_REMOTE_TIMEOUT", 600)
    auth_file: str = os.getenv("DSH_REMOTE_AUTH_FILE", str(ROOT / "data" / "remote_auth.json"))


SETTINGS = Settings()
