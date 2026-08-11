"""Compare canonical provider HTTP/GraphQL calls with local audited operations."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
from uuid import uuid4

from mcp_system import MCPSystem, PluginRegistry
from mcp_system.differential import HTTPDifferentialTarget, LocalOperationTarget, compare_runs, run_dual_scenario, write_cassette
from mcp_system.service_plugins import BitbucketPlugin, GitHubPlugin, GitLabPlugin, JiraPlugin, LinearPlugin, YouTrackPlugin


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scenario", type=Path, required=True)
    parser.add_argument("--real-url", required=True)
    parser.add_argument("--real-token")
    parser.add_argument("--real-token-file", type=Path)
    parser.add_argument("--token-header", default="Authorization")
    parser.add_argument("--token-prefix", default="Bearer ")
    parser.add_argument("--http-timeout", type=float, default=30.0)
    parser.add_argument("--retry-status", type=int, action="append", default=[])
    parser.add_argument("--retry-attempts", type=int, default=1)
    parser.add_argument("--retry-interval", type=float, default=0.0)
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--environment", required=True)
    parser.add_argument("--instance", required=True)
    parser.add_argument("--actor", required=True)
    parser.add_argument("--postgres-dsn", default=os.getenv("MCP_SYSTEM_POSTGRES_DSN"))
    parser.add_argument("--artifacts", type=Path, default=Path("artifacts/provider-differential"))
    parser.add_argument("--run-id", default=uuid4().hex[:10])
    parser.add_argument("--confirm-disposable", action="store_true")
    args = parser.parse_args()
    if bool(args.real_token) == bool(args.real_token_file):
        parser.error("provide exactly one of --real-token or --real-token-file")
    scenario = json.loads(args.scenario.read_text(encoding="utf-8"))
    mutating = any(
        step["real"]["method"] not in {"GET", "HEAD"}
        and step["real"].get("operationType") != "query"
        for step in scenario["steps"]
    )
    if mutating and not args.confirm_disposable:
        parser.error("mutating scenarios require --confirm-disposable")
    if mutating and not scenario.get("safety", {}).get("disposableOnly"):
        parser.error("mutating scenario must declare safety.disposableOnly=true")
    token = args.real_token or args.real_token_file.read_text(encoding="utf-8").strip()
    registry = PluginRegistry()
    for plugin in (GitHubPlugin(), GitLabPlugin(), JiraPlugin(), BitbucketPlugin(), LinearPlugin(), YouTrackPlugin()):
        registry.register(plugin)
    system = MCPSystem.with_postgres(args.data_root, registry, args.postgres_dsn) if args.postgres_dsn else MCPSystem(args.data_root, registry)
    real = HTTPDifferentialTarget(
        args.real_url, token=args.token_prefix + token, token_header=args.token_header,
        timeout=args.http_timeout, retry_statuses=args.retry_status,
        retry_attempts=args.retry_attempts, retry_interval=args.retry_interval,
    )
    replica = LocalOperationTarget(system, args.environment, args.instance, actor=args.actor)
    real_records, replica_records = run_dual_scenario(scenario, real, replica, variables={"run_id": args.run_id})
    output = args.artifacts / scenario["name"] / args.run_id
    write_cassette(output / "real.json", real_records, metadata={"target": args.real_url, "run_id": args.run_id})
    write_cassette(output / "replica.json", replica_records, metadata={"environment": args.environment, "instance": args.instance, "run_id": args.run_id})
    report = compare_runs(real_records, replica_records)
    (output / "semantic.diff.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))
    if not report["passed"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
