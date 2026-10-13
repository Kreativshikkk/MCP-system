"""Optional evaluation restriction on branch writes, bound at server startup.

Provider APIs retain their ordinary behavior when no policy is supplied. The
policy acts on provider operation names after MCP argument translation.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Mapping

from ..errors import ConfigurationError, ServiceOperationError


class BranchPolicyViolation(ServiceOperationError):
    status_code = 403
    error = "branch_write_forbidden"


# Explicitly classified operations that change collaboration metadata, not Git
# branches. An unknown mutation on the repository surface is refused.
_METADATA = {
    "gitlab": frozenset({
        "create_label", "update_label", "delete_label", "create_issue", "update_issue",
        "delete_issue", "create_issue_note", "update_issue_note", "delete_issue_note",
        "create_merge_request", "update_merge_request", "approve_merge_request",
        "unapprove_merge_request", "create_merge_request_note", "update_merge_request_note",
        "delete_merge_request_note", "create_merge_request_discussion",
        "resolve_merge_request_discussion", "create_merge_request_discussion_note",
        "create_merge_request_pipeline", "create_pipeline", "retry_pipeline", "cancel_pipeline",
        "retry_job", "cancel_job", "play_job", "create_tag", "delete_tag",
        "create_release", "update_release", "delete_release",
    }),
    "github": frozenset({
        "create_label", "create_issue", "update_issue", "create_comment",
        "add_issue_labels", "set_issue_labels", "remove_issue_label", "add_assignees",
        "remove_assignees", "create_pull_request", "update_pull_request",
        "request_reviewers", "remove_requested_reviewers", "create_review",
        "create_review_comment", "dispatch_workflow", "create_release",
    }),
    "bitbucket": frozenset({
        "create_tag", "create_issue", "update_issue", "create_issue_comment",
        "create_pull_request", "create_pull_request_comment", "approve_pull_request",
        "request_changes", "create_pipeline",
    }),
}


@dataclass(frozen=True)
class BranchWritePolicy:
    instance_id: str
    plugin_id: str
    repository: str
    writable_branch: str | None

    @classmethod
    def load(cls, path: Path) -> "BranchWritePolicy":
        data = json.loads(path.read_text())
        expected = {"version", "instance_id", "plugin_id", "repository", "writable_branch"}
        if not isinstance(data, dict) or set(data) != expected or type(data["version"]) is not int or data["version"] != 1:
            raise ConfigurationError("unsupported branch policy document")
        for key in ("instance_id", "plugin_id", "repository"):
            if not isinstance(data[key], str) or not data[key]:
                raise ConfigurationError(f"branch policy {key} must be a nonempty string")
        branch = data["writable_branch"]
        if branch is not None and (not isinstance(branch, str) or not branch or branch.startswith("refs/")):
            raise ConfigurationError("writable_branch must be a branch name or null")
        if data["plugin_id"] not in _METADATA:
            raise ConfigurationError("unsupported branch policy repository provider")
        return cls(**{key: data[key] for key in expected - {"version"}})

    def _deny(self, operation: str) -> None:
        scope = f"only existing branch {self.writable_branch!r} is writable" if self.writable_branch else "all branches are read-only"
        raise BranchPolicyViolation(f"Cannot perform {operation}: {scope}; creating or deleting branches is forbidden.")

    def _branch(self, operation: str, branch: str | None) -> None:
        if self.writable_branch is None or branch != self.writable_branch:
            self._deny(operation)

    def check(self, system: Any, environment_id: str, actor: str,
              instance_id: str, plugin_id: str, operation: str,
              arguments: Mapping[str, Any], *, read_only: bool) -> None:
        if read_only or plugin_id not in _METADATA:
            return
        if instance_id != self.instance_id or plugin_id != self.plugin_id:
            self._deny(operation)
        with system.open_service_operations(environment_id, instance_id, actor=actor) as ops:
            if plugin_id == "gitlab":
                if not arguments.get("project"):
                    self._deny(operation)
                repo_args = {"project": arguments["project"]}
                identity = ops.get_project(**repo_args)["path_with_namespace"]
            elif plugin_id == "github":
                if "owner" not in arguments or "repository" not in arguments:
                    self._deny(operation)
                repo_args = {key: arguments[key] for key in ("owner", "repository")}
                identity = ops.get_repository(**repo_args)["full_name"]
            else:
                if "workspace" not in arguments or "repo_slug" not in arguments:
                    self._deny(operation)
                repo_args = {key: arguments[key] for key in ("workspace", "repo_slug")}
                identity = ops.get_repository(**repo_args)["full_name"]
            if identity != self.repository:
                self._deny(operation)

            if operation in _METADATA[plugin_id]:
                return
            if operation in {"create_branch", "delete_branch", "create_ref", "delete_ref"}:
                self._deny(operation)
            if plugin_id == "gitlab" and operation in {
                "create_repository_commit", "create_repository_file",
                "update_repository_file", "delete_repository_file",
            }:
                branch = arguments["branch"]
                self._branch(operation, branch)
                ops.get_branch(**repo_args, branch=branch)  # no implicit branch creation
                return
            if plugin_id == "github" and operation == "create_commit":
                # GitHub creates detached objects first, then publishes through
                # update_ref. No branch is moved here.
                if self.writable_branch is None:
                    self._deny(operation)
                return
            if plugin_id == "github" and operation == "update_ref":
                ref = arguments["ref"]
                name = ref.removeprefix("refs/")
                if not name.startswith("heads/"):
                    self._deny(operation)
                self._branch(operation, name.removeprefix("heads/"))
                ops.get_ref(**repo_args, ref=ref)
                return
            if plugin_id == "bitbucket" and operation == "create_commit":
                branch = arguments["branch"]
                self._branch(operation, branch)
                ops.get_branch(**repo_args, name=branch)
                return
            if plugin_id == "gitlab" and operation in {"merge_merge_request", "resolve_merge_request_conflicts"}:
                request = ops.get_merge_request(**repo_args, merge_request_iid=arguments["merge_request_iid"])
                key = "source_branch" if operation == "resolve_merge_request_conflicts" else "target_branch"
                self._branch(operation, request[key])
                ops.get_branch(**repo_args, branch=request[key])
                return
            if plugin_id == "github" and operation == "merge_pull_request_api":
                request = ops.get_pull_request(**repo_args, pull_number=arguments["pull_number"])
                self._branch(operation, request["base"]["ref"])
                ops.get_ref(**repo_args, ref="heads/" + request["base"]["ref"])
                return
            if plugin_id == "bitbucket" and operation == "merge_pull_request":
                request = ops.get_pull_request(**repo_args, pull_request_id=arguments["pull_request_id"])
                branch = request["destination"]["branch"]["name"]
                self._branch(operation, branch)
                ops.get_branch(**repo_args, name=branch)
                return
            self._deny(operation)
