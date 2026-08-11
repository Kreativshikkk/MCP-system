"""Bootstrap the pinned local GitLab CE target for differential scenarios."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
from urllib.error import HTTPError
from urllib.request import Request, urlopen


def request(base_url: str, method: str, path: str, *, token: str | None = None, body: object | None = None) -> object:
    payload = json.dumps(body).encode() if body is not None else None
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if payload is not None:
        headers["Content-Type"] = "application/json"
    req = Request(f"{base_url.rstrip('/')}{path}", data=payload, headers=headers, method=method)
    try:
        response = urlopen(req, timeout=60)
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"GitLab {method} {path} failed: {exc.code} {detail}") from exc
    raw = response.read()
    return json.loads(raw) if raw else None


def create_local_token(compose_file: Path, service: str) -> str:
    """Create a short-lived PAT in the pinned local GitLab container."""
    ruby = (
        "user = User.find_by_username!('root'); "
        "organization = Organizations::Organization.first; "
        "result = PersonalAccessTokens::CreateService.new("
        "current_user: user, target_user: user, organization_id: organization.id, "
        "params: {name: 'mcp-differential', scopes: ['api'], "
        "expires_at: 7.days.from_now.to_date}).execute; "
        "abort(result.message) unless result.success?; "
        "puts result.payload[:personal_access_token].token"
    )
    completed = subprocess.run(
        [
            "docker", "compose", "-f", str(compose_file), "exec", "-T",
            service, "gitlab-rails", "runner", ruby,
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    token = completed.stdout.strip().splitlines()[-1]
    if not token.startswith("glpat-"):
        raise RuntimeError("local GitLab did not return a personal access token")
    return token


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8929")
    parser.add_argument("--token", default=os.getenv("GITLAB_DIFFERENTIAL_TOKEN"))
    parser.add_argument("--compose-file", type=Path, default=Path("configs/differential/gitlab-compose.yaml"))
    parser.add_argument("--compose-service", default="gitlab")
    parser.add_argument("--token-file", type=Path, default=Path(".gitlab-differential-token"))
    args = parser.parse_args()

    token = args.token or create_local_token(args.compose_file, args.compose_service)
    api = f"{args.url.rstrip('/')}/api/v4"

    groups = request(api, "GET", "/groups?search=acme", token=token)
    group = next((item for item in groups if item["path"] == "acme"), None)
    if group is None:
        group = request(api, "POST", "/groups", token=token, body={"name": "Acme Software", "path": "acme", "visibility": "private"})
    projects = request(api, "GET", f"/groups/{group['id']}/projects?search=product", token=token)
    project = next((item for item in projects if item["path"] == "product"), None)
    if project is None:
        project = request(api, "POST", "/projects", token=token, body={"name": "Product", "path": "product", "namespace_id": group["id"], "visibility": "private", "initialize_with_readme": True, "description": "Primary product project"})

    descriptor = args.token_file.open("w", encoding="utf-8")
    try:
        os.chmod(args.token_file, 0o600)
        descriptor.write(token + "\n")
    finally:
        descriptor.close()
    print(json.dumps({"gitlab_version": request(api, "GET", "/version", token=token), "group_id": group["id"], "project_id": project["id"], "token_file": str(args.token_file)}, indent=2))


if __name__ == "__main__":
    main()
