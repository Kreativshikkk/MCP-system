"""Extract MCPSystem's selected Bitbucket operations from a pinned Swagger."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


SOURCE_SHA256 = "dc11be99fe57eb991194de80cdfe75e425cd0f590e6cf83e9dc3d2a22d4943de"

# method, path, local operation, workflow
SELECTED = (
    ("get", "/user", "get_current_user", "identity"),
    ("get", "/workspaces/{workspace}", "get_workspace", "membership"),
    ("get", "/workspaces/{workspace}/members", "list_workspace_members", "membership"),
    ("get", "/repositories/{workspace}", "list_repositories", "repository"),
    ("get", "/repositories/{workspace}/{repo_slug}", "get_repository", "repository"),
    ("get", "/repositories/{workspace}/{repo_slug}/commits", "list_commits", "source"),
    ("get", "/repositories/{workspace}/{repo_slug}/commit/{commit}", "get_commit", "source"),
    ("post", "/repositories/{workspace}/{repo_slug}/src", "create_commit", "source"),
    ("get", "/repositories/{workspace}/{repo_slug}/src/{commit}/{path}", "get_file", "source"),
    ("get", "/repositories/{workspace}/{repo_slug}/diff/{spec}", "get_diff", "source"),
    ("get", "/repositories/{workspace}/{repo_slug}/refs/branches", "list_branches", "branch"),
    ("post", "/repositories/{workspace}/{repo_slug}/refs/branches", "create_branch", "branch"),
    ("get", "/repositories/{workspace}/{repo_slug}/refs/branches/{name}", "get_branch", "branch"),
    ("delete", "/repositories/{workspace}/{repo_slug}/refs/branches/{name}", "delete_branch", "branch"),
    ("get", "/repositories/{workspace}/{repo_slug}/issues", "list_issues", "ticket"),
    ("post", "/repositories/{workspace}/{repo_slug}/issues", "create_issue", "ticket"),
    ("get", "/repositories/{workspace}/{repo_slug}/issues/{issue_id}", "get_issue", "ticket"),
    ("put", "/repositories/{workspace}/{repo_slug}/issues/{issue_id}", "update_issue", "ticket"),
    ("get", "/repositories/{workspace}/{repo_slug}/issues/{issue_id}/comments", "list_issue_comments", "ticket"),
    ("post", "/repositories/{workspace}/{repo_slug}/issues/{issue_id}/comments", "create_issue_comment", "ticket"),
    ("get", "/repositories/{workspace}/{repo_slug}/pullrequests", "list_pull_requests", "review"),
    ("post", "/repositories/{workspace}/{repo_slug}/pullrequests", "create_pull_request", "review"),
    ("get", "/repositories/{workspace}/{repo_slug}/pullrequests/{pull_request_id}", "get_pull_request", "review"),
    ("get", "/repositories/{workspace}/{repo_slug}/pullrequests/{pull_request_id}/diff", "get_pull_request_diff", "review"),
    ("get", "/repositories/{workspace}/{repo_slug}/pullrequests/{pull_request_id}/comments", "list_pull_request_comments", "review"),
    ("post", "/repositories/{workspace}/{repo_slug}/pullrequests/{pull_request_id}/comments", "create_pull_request_comment", "review"),
    ("post", "/repositories/{workspace}/{repo_slug}/pullrequests/{pull_request_id}/approve", "approve_pull_request", "review"),
    ("post", "/repositories/{workspace}/{repo_slug}/pullrequests/{pull_request_id}/request-changes", "request_changes", "review"),
    ("post", "/repositories/{workspace}/{repo_slug}/pullrequests/{pull_request_id}/merge", "merge_pull_request", "review"),
    ("get", "/repositories/{workspace}/{repo_slug}/pipelines", "list_pipelines", "ci"),
    ("post", "/repositories/{workspace}/{repo_slug}/pipelines", "create_pipeline", "ci"),
    ("get", "/repositories/{workspace}/{repo_slug}/pipelines/{pipeline_uuid}", "get_pipeline", "ci"),
    ("get", "/repositories/{workspace}/{repo_slug}/pipelines/{pipeline_uuid}/steps", "list_pipeline_steps", "ci"),
    ("get", "/repositories/{workspace}/{repo_slug}/pipelines/{pipeline_uuid}/steps/{step_uuid}/log", "get_pipeline_step_log", "ci"),
    ("get", "/repositories/{workspace}/{repo_slug}/commit/{commit}/statuses", "list_commit_statuses", "ci"),
    ("post", "/repositories/{workspace}/{repo_slug}/commit/{commit}/statuses/build", "create_commit_status", "ci"),
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("contracts/bitbucket/selected-operations.json"),
    )
    args = parser.parse_args()
    raw = args.source.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if digest != SOURCE_SHA256:
        raise SystemExit(f"unexpected Bitbucket Swagger SHA-256: {digest}")
    source = json.loads(raw)
    operations = []
    for method, path, local, workflow in SELECTED:
        path_item = source["paths"][path]
        operation = path_item[method]
        parameters = [*path_item.get("parameters", []), *operation.get("parameters", [])]
        serialized_parameters = []
        for item in parameters:
            serialized_parameters.append(
                {
                    "name": item["name"],
                    "in": item["in"],
                    "required": bool(item.get("required")),
                    "type": item.get("type"),
                    "schemaRef": item.get("schema", {}).get("$ref"),
                }
            )
        responses = sorted(operation.get("responses", {}))
        operations.append(
            {
                "method": method.upper(),
                "path": path,
                "operationId": operation.get("operationId"),
                "workflow": workflow,
                "localOperation": local,
                "mcpTool": f"bitbucket_{local}",
                "requiredParameters": sorted(
                    item["name"] for item in parameters if item.get("required")
                ),
                "parameters": serialized_parameters,
                "responses": responses,
                "successResponses": [value for value in responses if value.startswith("2")],
                "errorResponses": [value for value in responses if value.startswith(("4", "5"))],
                "summary": operation.get("summary"),
            }
        )
    document = {
        "provider": "Bitbucket Cloud",
        "apiVersion": source["info"]["version"],
        "sourceSha256": digest,
        "operationCount": len(operations),
        "operations": operations,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


if __name__ == "__main__":
    main()
