"""Materialize the built-in multi-service company environment."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from mcp_system import MCPSystem, PluginRegistry
from mcp_system.errors import TemplateNotFoundError
from mcp_system.service_plugins import BitbucketPlugin, GitHubPlugin, GitLabPlugin, JiraPlugin, LinearPlugin, YouTrackPlugin


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-root", type=Path, default=Path("data"))
    parser.add_argument("--name", default="local software company")
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
    system = (
        MCPSystem.with_postgres(
            args.data_root,
            registry,
            args.postgres_dsn,
            control_schema=args.control_schema,
            storage_namespace=args.storage_namespace,
        )
        if args.postgres_dsn
        else MCPSystem(args.data_root, registry)
    )
    template_id = "software_company_default"
    try:
        template = system.require_template(template_id).template
    except TemplateNotFoundError:
        template = system.create_template_from_toml(
            Path("configs/templates/software-company-default.toml")
        )
    environment = system.create_environment_from_template(
        template.id, name=args.name
    )
    print(f"template={template.id} status={template.status}")
    print(f"environment={environment.id} status={environment.status}")
    print(
        "config_command=PYTHONPATH=src .venv/bin/python "
        f"scripts/generate_mcp_config.py --data-root {args.data_root} "
        f"--environment {environment.id}"
    )


if __name__ == "__main__":
    main()
