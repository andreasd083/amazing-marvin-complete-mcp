"""Configuration and secret loading for the Amazing Marvin MCP server.

Tokens can be provided either as files (recommended: Docker secrets or any
0600 file, path given via *_FILE variables) or directly as environment
variables (convenient for local use). File paths take precedence. Token
values are never logged and never appear in repr() output.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


def _resolve_timezone():
    """Timezone for all date logic (Marvin 'days', rate-limit rollover).

    Set MARVIN_TIMEZONE to an IANA name (e.g. "Europe/Stockholm") to match
    the timezone your Marvin account lives in. Defaults to the system's
    local timezone — note that inside most containers that means UTC.
    """
    name = os.environ.get("MARVIN_TIMEZONE")
    if name:
        return ZoneInfo(name)
    return datetime.now().astimezone().tzinfo


TIMEZONE = _resolve_timezone()

MARVIN_API_BASE = "https://serv.amazingmarvin.com/api"


def _read_secret(name: str, required: bool = True) -> str | None:
    """Read a secret from <name>_FILE (preferred) or <name> directly."""
    path = os.environ.get(f"{name}_FILE")
    if path:
        value = Path(path).read_text(encoding="utf-8").strip()
        if not value:
            raise RuntimeError(f"Secret file for {name}_FILE is empty")
        return value
    value = os.environ.get(name, "").strip()
    if value:
        return value
    if required:
        raise RuntimeError(f"Neither {name}_FILE nor {name} is set")
    return None


@dataclass
class Settings:
    api_token: str = field(repr=False)
    full_access_token: str | None = field(repr=False)
    mcp_auth_token: str | None = field(repr=False)
    transport: str = "stdio"
    host: str = "127.0.0.1"
    port: int = 8787
    state_dir: Path = Path.home() / ".marvin-mcp"
    # Explicit opt-out from the fail-closed HTTP check (MCP_ALLOW_UNAUTHENTICATED).
    allow_unauthenticated: bool = False

    def __repr__(self) -> str:  # mask all token fields
        return (
            f"Settings(transport={self.transport!r}, host={self.host!r}, "
            f"port={self.port}, state_dir={self.state_dir!r}, "
            f"api_token=[HIDDEN], "
            f"full_access_token={'[HIDDEN]' if self.full_access_token else None}, "
            f"mcp_auth_token={'[HIDDEN]' if self.mcp_auth_token else None})"
        )


def load_settings() -> Settings:
    return Settings(
        api_token=_read_secret("MARVIN_API_TOKEN"),
        full_access_token=_read_secret("MARVIN_FULL_ACCESS_TOKEN", required=False),
        mcp_auth_token=_read_secret("MCP_AUTH_TOKEN", required=False),
        transport=os.environ.get("MCP_TRANSPORT", "stdio").lower(),
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "8787")),
        state_dir=Path(
            os.environ.get("STATE_DIR", str(Path.home() / ".marvin-mcp"))
        ),
        allow_unauthenticated=os.environ.get("MCP_ALLOW_UNAUTHENTICATED", "")
        .strip()
        .lower()
        in ("1", "true", "yes"),
    )
