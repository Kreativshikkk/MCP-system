"""GitLab projection into the provider-neutral Inspector model."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from ...git_storage import GitServiceDataPlane, GitStorageError
from ...plugins import RelationalSession


@dataclass(frozen=True, slots=True)
class GitLabInspectorAdapter:
    plugin_id: str = "gitlab"
    plugin_version: str = "0.1.0"

    def project(self, session: RelationalSession, git_data_plane: GitServiceDataPlane | None) -> Mapping[str, Any]:
        projects = session.execute("SELECT * FROM gitlab_projects ORDER BY lower(path_with_namespace)").fetchall()
        return {"provider": {"id": "gitlab", "name": "GitLab"}, "repositories": [self._project(session, git_data_plane, row) for row in projects]}

    def _project(self, session: RelationalSession, git_data_plane: GitServiceDataPlane | None, project: Mapping[str, Any]) -> dict[str, Any]:
        project_id = project["id"]
        issues = session.execute("""SELECT i.*,u.username author_username FROM gitlab_issues i JOIN gitlab_users u ON u.id=i.author_id
            WHERE i.project_id=? ORDER BY i.iid DESC""", (project_id,)).fetchall()
        tickets = []
        for issue in issues:
            labels = session.execute("SELECT l.name FROM gitlab_issue_labels il JOIN gitlab_labels l ON l.id=il.label_id WHERE il.issue_id=? ORDER BY lower(l.name)", (issue["id"],)).fetchall()
            notes = self._notes(session, "Issue", issue["id"])
            assignee = session.execute("SELECT username FROM gitlab_users WHERE id=?", (issue["assignee_id"],)).fetchone() if issue["assignee_id"] else None
            tickets.append({"id": str(issue["id"]), "number": issue["iid"], "title": issue["title"], "description": issue["description"], "state": "open" if issue["state"] == "opened" else issue["state"], "stateReason": None, "author": issue["author_username"], "labels": [label["name"] for label in labels], "assignees": [assignee["username"]] if assignee else [], "comments": notes, "createdAt": _json_time(issue["created_at"]), "updatedAt": _json_time(issue["updated_at"])})
        merge_requests = session.execute("""SELECT mr.*,u.username author_username FROM gitlab_merge_requests mr JOIN gitlab_users u ON u.id=mr.author_id
            WHERE mr.project_id=? ORDER BY mr.iid DESC""", (project_id,)).fetchall()
        change_sets = []
        for mr in merge_requests:
            reviewer_rows = session.execute("SELECT u.username,r.approved FROM gitlab_merge_request_reviewers r JOIN gitlab_users u ON u.id=r.user_id WHERE r.merge_request_id=?", (mr["id"],)).fetchall()
            diff = {"patch": "", "truncated": False, "available": False}
            if git_data_plane and mr["target_sha"] and mr["source_sha"]:
                try:
                    diff = {**git_data_plane.repository(project_id).diff(mr["target_sha"], mr["source_sha"]), "available": True}
                except GitStorageError:
                    pass
            change_sets.append({"id": str(mr["id"]), "number": mr["iid"], "title": mr["title"], "description": mr["description"], "state": "open" if mr["state"] == "opened" else mr["state"], "author": mr["author_username"], "draft": bool(mr["draft"]), "merged": mr["state"] == "merged", "mergeableState": "unknown", "base": {"ref": mr["target_branch"], "sha": mr["target_sha"]}, "head": {"ref": mr["source_branch"], "sha": mr["source_sha"]}, "mergeCommitSha": mr["merge_commit_sha"], "reviews": [{"id": f"{mr['id']}:{reviewer['username']}", "reviewer": reviewer["username"], "state": "APPROVED" if reviewer["approved"] else "PENDING", "body": None, "commitSha": mr["source_sha"], "submittedAt": _json_time(mr["updated_at"])} for reviewer in reviewer_rows], "reviewComments": self._notes(session, "MergeRequest", mr["id"]), "diff": diff, "createdAt": _json_time(mr["created_at"]), "updatedAt": _json_time(mr["updated_at"])})
        pipelines = session.execute("SELECT p.*,u.username actor_username FROM gitlab_pipelines p LEFT JOIN gitlab_users u ON u.id=p.user_id WHERE p.project_id=? ORDER BY p.iid DESC", (project_id,)).fetchall()
        builds = [{"id": str(p["id"]), "name": f"Pipeline #{p['iid']}", "event": p["source"], "status": self._build_status(p["status"]), "conclusion": self._conclusion(p["status"]), "headBranch": p["ref"], "headSha": p["sha"], "runNumber": p["iid"], "runAttempt": 1, "actor": p["actor_username"], "createdAt": _json_time(p["created_at"]), "updatedAt": _json_time(p["updated_at"])} for p in pipelines]
        return {"id": str(project_id), "provider": "gitlab", "name": project["name"], "fullName": project["path_with_namespace"], "visibility": project["visibility"], "archived": bool(project["archived"]), "defaultBranch": project["default_branch"], "tickets": tickets, "changeSets": change_sets, "builds": builds}

    @staticmethod
    def _notes(session: RelationalSession, kind: str, noteable_id: int) -> list[dict[str, Any]]:
        rows = session.execute("SELECT n.*,u.username author_username FROM gitlab_notes n JOIN gitlab_users u ON u.id=n.author_id WHERE n.noteable_type=? AND n.noteable_id=? ORDER BY n.id", (kind, noteable_id)).fetchall()
        return [{"id": str(row["id"]), "author": row["author_username"], "body": row["body"], "createdAt": _json_time(row["created_at"])} for row in rows]

    @staticmethod
    def _build_status(status: str) -> str:
        return "completed" if status in {"success", "failed", "canceled", "skipped"} else ("in_progress" if status == "running" else "queued")

    @staticmethod
    def _conclusion(status: str) -> str | None:
        return {"success": "success", "failed": "failure", "canceled": "cancelled", "skipped": "skipped"}.get(status)


def _json_time(value: Any) -> Any:
    return value.isoformat() if hasattr(value, "isoformat") else value
