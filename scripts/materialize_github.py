"""Materialize the built-in GitHub template and one isolated environment."""

from __future__ import annotations

import os
import argparse
from pathlib import Path

from mcp_system import MCPSystem, PluginRegistry
from mcp_system.errors import TemplateNotFoundError
from mcp_system.service_plugins import GitHubPlugin


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-root", type=Path, default=Path("data"))
    parser.add_argument("--name", default="github local instance")
    parser.add_argument("--postgres-dsn", default=os.getenv("MCP_SYSTEM_POSTGRES_DSN"))
    args = parser.parse_args()
    registry = PluginRegistry()
    registry.register(GitHubPlugin())
    system = (
        MCPSystem.with_postgres(args.data_root, registry, args.postgres_dsn)
        if args.postgres_dsn
        else MCPSystem(args.data_root, registry)
    )

    template_id = "github_default"
    try:
        template = system.require_template(template_id).template
    except TemplateNotFoundError:
        template = system.create_template_from_toml(
            Path("configs/templates/github-default.toml")
        )

    environment = system.create_environment_from_template(
        template.id, name=args.name
    )
    print(f"template={template.id} status={template.status}")
    print(f"environment={environment.id} status={environment.status}")


if __name__ == "__main__":
    main()
