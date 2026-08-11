"""Materialize the built-in GitLab template and one isolated environment."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from mcp_system import MCPSystem, PluginRegistry
from mcp_system.errors import TemplateNotFoundError
from mcp_system.service_plugins import GitLabPlugin


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-root", type=Path, default=Path("data"))
    parser.add_argument("--name", default="gitlab local instance")
    parser.add_argument("--postgres-dsn", default=os.getenv("MCP_SYSTEM_POSTGRES_DSN"))
    args = parser.parse_args()

    registry = PluginRegistry()
    registry.register(GitLabPlugin())
    system = (
        MCPSystem.with_postgres(args.data_root, registry, args.postgres_dsn)
        if args.postgres_dsn
        else MCPSystem(args.data_root, registry)
    )

    template_id = "gitlab_default"
    try:
        template = system.require_template(template_id).template
    except TemplateNotFoundError:
        template = system.create_template_from_toml(
            Path("configs/templates/gitlab-default.toml")
        )

    environment = system.create_environment_from_template(
        template.id, name=args.name
    )
    initial_commit = system.invoke_service_operation(
        environment.id,
        "gitlab",
        actor="director",
        transport="bootstrap",
        operation="create_commit",
        arguments={
            "project": "acme/product",
            "message": "Initial commit",
            "author": "director",
            "files": {"README.md": "# Product\n"},
        },
    )
    system.invoke_service_operation(
        environment.id,
        "gitlab",
        actor="director",
        transport="bootstrap",
        operation="create_branch",
        arguments={
            "project": "acme/product",
            "branch": "main",
            "ref": initial_commit["id"],
        },
    )
    print(f"template={template.id} status={template.status}")
    print(f"environment={environment.id} status={environment.status}")


if __name__ == "__main__":
    main()
