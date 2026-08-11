"""Print a ready-to-use, role-bound MCP client configuration."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from mcp_system.mcp.launcher import build_client_config


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--environment", required=True)
    parser.add_argument("--data-root", type=Path, default=Path("data"))
    parser.add_argument(
        "--actors", nargs="+", default=("director", "lead", "engineer", "qa")
    )
    parser.add_argument("--python", type=Path, default=Path(sys.executable))
    parser.add_argument(
        "--server-script", type=Path, default=Path("scripts/mcp_server.py")
    )
    parser.add_argument("--control-schema", default="mcp_control")
    parser.add_argument("--storage-namespace", default="mcp")
    args = parser.parse_args()
    config = build_client_config(
        environment_id=args.environment,
        actors=args.actors,
        data_root=args.data_root,
        python_executable=args.python,
        server_script=args.server_script,
        control_schema=args.control_schema,
        storage_namespace=args.storage_namespace,
    )
    print(json.dumps(config, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
