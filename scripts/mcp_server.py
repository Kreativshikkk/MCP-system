"""Run one isolated MCPSystem environment over the MCP stdio transport."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from mcp_system import MCPSystem, PluginRegistry
from mcp_system.mcp import MCPDispatcher, MCPJSONRPCServer, run_stdio
from mcp_system.service_plugins import BitbucketPlugin, GitHubPlugin, GitLabPlugin, JiraPlugin, LinearPlugin, YouTrackPlugin


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-root", type=Path, default=Path("data"))
    parser.add_argument("--environment", required=True)
    parser.add_argument("--actor", required=True)
    parser.add_argument("--github-instance")
    parser.add_argument("--gitlab-instance")
    parser.add_argument("--jira-instance")
    parser.add_argument("--postgres-dsn", default=os.getenv("MCP_SYSTEM_POSTGRES_DSN"))
    parser.add_argument("--control-schema", default="mcp_control")
    parser.add_argument("--storage-namespace", default="mcp")
    args = parser.parse_args()

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
    bindings = {
        surface: instance
        for surface, instance in (
            ("github_rest_v3", args.github_instance),
            ("gitlab_rest_v4", args.gitlab_instance),
            ("jira_rest_v3", args.jira_instance),
        )
        if instance is not None
    }
    dispatcher = MCPDispatcher(
        system,
        args.environment,
        actor=args.actor,
        bindings=bindings,
    )
    run_stdio(MCPJSONRPCServer(dispatcher))


if __name__ == "__main__":
    main()
