"""Bitbucket projection into the provider-neutral Inspector model."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from ...git_storage import GitServiceDataPlane
from ...plugins import RelationalSession


@dataclass(frozen=True, slots=True)
class BitbucketInspectorAdapter:
    plugin_id: str = "bitbucket"
    plugin_version: str = "0.1.0"

    def project(self, session: RelationalSession, git_data_plane: GitServiceDataPlane | None) -> Mapping[str, Any]:
        repositories = session.execute("SELECT r.*,w.slug workspace_slug FROM bitbucket_repositories r JOIN bitbucket_workspaces w ON w.id=r.workspace_id ORDER BY w.slug,r.slug").fetchall()
        return {"provider": {"id": "bitbucket", "name": "Bitbucket"}, "repositories": [self._repository(session, git_data_plane, row) for row in repositories]}

    def _repository(self, session: RelationalSession, git_data_plane: GitServiceDataPlane | None, repo: Mapping[str, Any]) -> dict[str, Any]:
        issues = session.execute("SELECT i.*,u.username reporter FROM bitbucket_issues i JOIN bitbucket_users u ON u.id=i.reporter_id WHERE i.repository_id=? ORDER BY i.local_id DESC", (repo["id"],)).fetchall()
        tickets = []
        for issue in issues:
            assignee = session.execute("SELECT username FROM bitbucket_users WHERE id=?", (issue["assignee_id"],)).fetchone() if issue["assignee_id"] else None
            comments = session.execute("SELECT c.*,u.username author FROM bitbucket_comments c JOIN bitbucket_users u ON u.id=c.author_id WHERE c.subject_type='issue' AND c.subject_id=? ORDER BY c.id", (issue["id"],)).fetchall()
            tickets.append({"id": str(issue["id"]), "number": issue["local_id"], "title": issue["title"], "description": issue["content"], "state": "closed" if issue["state"] in {"resolved", "closed"} else "open", "stateReason": issue["state"], "author": issue["reporter"], "labels": [issue["kind"], issue["priority"]], "assignees": [assignee["username"]] if assignee else [], "comments": [{"id": str(item["id"]), "author": item["author"], "body": item["content"], "createdAt": _time(item["created_at"])} for item in comments], "iterations": [], "links": [], "createdAt": _time(issue["created_at"]), "updatedAt": _time(issue["updated_at"])})
        prs = session.execute("SELECT p.*,u.username author FROM bitbucket_pull_requests p JOIN bitbucket_users u ON u.id=p.author_id WHERE p.repository_id=? ORDER BY p.local_id DESC", (repo["id"],)).fetchall()
        changes = []
        for pr in prs:
            reviews = session.execute("SELECT r.state,u.username reviewer FROM bitbucket_pull_request_reviewers r JOIN bitbucket_users u ON u.id=r.user_id WHERE r.pull_request_id=? ORDER BY u.id", (pr["id"],)).fetchall()
            comments = session.execute("SELECT c.content,u.username reviewer FROM bitbucket_comments c JOIN bitbucket_users u ON u.id=c.author_id WHERE c.subject_type='pullrequest' AND c.subject_id=? ORDER BY c.id", (pr["id"],)).fetchall()
            patch = {"patch": "", "truncated": False}
            if git_data_plane is not None:
                patch = git_data_plane.repository(repo["id"]).diff(pr["destination_hash"], pr["source_hash"])
            changes.append({"id": str(pr["id"]), "number": pr["local_id"], "title": pr["title"], "description": pr["description"], "state": pr["state"].lower(), "merged": pr["state"] == "MERGED", "draft": False, "author": pr["author"], "base": {"ref": pr["destination_branch"], "sha": pr["destination_hash"]}, "head": {"ref": pr["source_branch"], "sha": pr["source_hash"]}, "mergeableState": "mergeable" if pr["state"] == "OPEN" else "unknown", "reviews": [{"reviewer": item["reviewer"], "state": item["state"], "body": next((comment["content"] for comment in comments if comment["reviewer"] == item["reviewer"]), None)} for item in reviews], "diff": {"available": git_data_plane is not None, "patch": patch["patch"], "truncated": patch["truncated"]}, "createdAt": _time(pr["created_at"]), "updatedAt": _time(pr["updated_at"])})
        pipelines = session.execute("SELECT * FROM bitbucket_pipelines WHERE repository_id=? ORDER BY build_number DESC", (repo["id"],)).fetchall()
        builds = [{"id": item["uuid"], "runNumber": item["build_number"], "name": f"Pipeline {item['ref_name']}", "status": item["state"].lower(), "conclusion": "success" if item["state"] == "COMPLETED" else "failure" if item["state"] == "FAILED" else None, "headSha": item["commit_hash"], "createdAt": _time(item["created_at"]), "updatedAt": _time(item["completed_at"] or item["created_at"])} for item in pipelines]
        return {"id": str(repo["id"]), "provider": "bitbucket", "name": repo["name"], "fullName": f"{repo['workspace_slug']}/{repo['slug']}", "visibility": "private" if repo["is_private"] else "public", "archived": False, "defaultBranch": repo["mainbranch"], "tickets": tickets, "changeSets": changes, "builds": builds}


def _time(value: Any) -> Any:
    return value.isoformat() if hasattr(value, "isoformat") else value
