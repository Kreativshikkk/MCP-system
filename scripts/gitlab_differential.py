"""Run GitLab core scenarios against real and/or replica HTTP targets."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from uuid import uuid4

from mcp_system.differential import HTTPDifferentialTarget, compare_runs, read_cassette, run_scenario, write_cassette


def _token(value: str | None, path: Path | None) -> str | None:
    if value and path:
        raise ValueError("provide a token or a token file, not both")
    if path:
        return path.read_text(encoding="utf-8").strip()
    return value


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("record", "replay", "differential"))
    parser.add_argument("--scenario", type=Path, required=True)
    parser.add_argument("--artifacts", type=Path, default=Path("artifacts/gitlab-differential"))
    parser.add_argument("--real-url", default="http://127.0.0.1:8929/api/v4")
    parser.add_argument("--real-token")
    parser.add_argument("--real-token-file", type=Path)
    parser.add_argument("--replica-url", default="http://127.0.0.1:8780/api/v4")
    parser.add_argument("--replica-token")
    parser.add_argument("--replica-token-file", type=Path)
    parser.add_argument("--run-id", default=uuid4().hex[:10])
    args = parser.parse_args()
    try:
        real_token = _token(args.real_token, args.real_token_file)
        replica_token = _token(args.replica_token, args.replica_token_file)
    except ValueError as exc:
        parser.error(str(exc))
    scenario = json.loads(args.scenario.read_text())
    output = args.artifacts / scenario["name"]
    variables = {"run_id": args.run_id}

    real_records = None
    if args.mode in {"record", "differential"}:
        if not real_token:
            parser.error("--real-token or --real-token-file is required")
        real_records = run_scenario(scenario, HTTPDifferentialTarget(args.real_url, token=real_token), variables=variables)
        write_cassette(output / "real.json", real_records, metadata={"target": args.real_url, "run_id": args.run_id})
    if args.mode == "record":
        return

    if not replica_token:
        parser.error("--replica-token or --replica-token-file is required")
    replica_records = run_scenario(scenario, HTTPDifferentialTarget(args.replica_url, token=replica_token), variables=variables)
    write_cassette(output / "replica.json", replica_records, metadata={"target": args.replica_url, "run_id": args.run_id})
    real_records = real_records or read_cassette(output / "real.json")
    report = compare_runs(real_records, replica_records)
    (output / "semantic.diff.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(json.dumps(report, indent=2))
    if not report["passed"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
