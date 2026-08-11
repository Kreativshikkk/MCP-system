"""Run the local read-only MCPSystem Inspector UI."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from mcp_system import MCPSystem, PluginRegistry
from mcp_system.http import InspectorHTTPRouter, serve_http
from mcp_system.service_plugins import BitbucketPlugin, GitHubPlugin, GitLabPlugin, JiraPlugin, LinearPlugin, YouTrackPlugin


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-root", type=Path, default=Path("data"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8777)
    parser.add_argument("--postgres-dsn", default=os.getenv("MCP_SYSTEM_POSTGRES_DSN"))
    parser.add_argument("--control-schema", default="mcp_control")
    parser.add_argument("--storage-namespace", default="mcp")
    args = parser.parse_args()
    if args.host not in ("127.0.0.1", "localhost"):
        parser.error("Inspector without authentication must bind to loopback")

    registry = PluginRegistry()
    registry.register(GitHubPlugin())
    registry.register(GitLabPlugin())
    registry.register(JiraPlugin())
    registry.register(BitbucketPlugin())
    registry.register(LinearPlugin())
    registry.register(YouTrackPlugin())
    if args.postgres_dsn:
        system = MCPSystem.with_postgres(
            args.data_root,
            registry,
            args.postgres_dsn,
            control_schema=args.control_schema,
            storage_namespace=args.storage_namespace,
        )
    else:
        system = MCPSystem(args.data_root, registry)
    print(f"Inspector: http://{args.host}:{args.port}")
    try:
        serve_http(InspectorHTTPRouter(system), host=args.host, port=args.port)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
