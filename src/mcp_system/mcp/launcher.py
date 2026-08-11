"""Generate role-bound MCP client entries for one company environment."""

from __future__ import annotations

from pathlib import Path
import re
from typing import Any, Iterable


_ROLE_NAME = re.compile(r"^[a-z][a-z0-9_-]{0,62}$")


def build_client_config(
    *,
    environment_id: str,
    actors: Iterable[str],
    data_root: Path,
    python_executable: Path,
    server_script: Path,
    control_schema: str = "mcp_control",
    storage_namespace: str = "mcp",
) -> dict[str, Any]:
    """Return conventional ``mcpServers`` JSON without embedding credentials."""
    if not environment_id.strip():
        raise ValueError("environment_id must not be empty")
    actor_list = tuple(actors)
    if not actor_list:
        raise ValueError("at least one actor is required")
    if len(actor_list) != len(set(actor_list)):
        raise ValueError("actors must be unique")
    for actor in actor_list:
        if not _ROLE_NAME.fullmatch(actor):
            raise ValueError(f"invalid actor id: {actor!r}")

    servers: dict[str, Any] = {}
    for actor in actor_list:
        servers[f"mcp-system-{actor}"] = {
            # Preserve a virtualenv path even when it is a symlink to the base
            # interpreter; resolving it would silently drop the venv packages.
            "command": str(python_executable.absolute()),
            "args": [
                str(server_script.resolve()),
                "--data-root",
                str(data_root.resolve()),
                "--environment",
                environment_id,
                "--actor",
                actor,
                "--control-schema",
                control_schema,
                "--storage-namespace",
                storage_namespace,
            ],
        }
    return {"mcpServers": servers}
