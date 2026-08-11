"""GitHub-to-universal Inspector projection adapter."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from ...git_storage import GitServiceDataPlane, GitStorageError
from ...plugins import RelationalSession


@dataclass(frozen=True, slots=True)
class GitHubInspectorAdapter:
    plugin_id: str = "github"
    plugin_version: str = "0.1.0"

    def project(
        self,
        session: RelationalSession,
        git_data_plane: GitServiceDataPlane | None,
    ) -> Mapping[str, Any]:
        repositories = session.execute(
            """
            SELECT repository.*, organization.login AS owner_login
              FROM github_repositories repository
              JOIN github_organizations organization
                ON organization.id = repository.owner_organization_id
             ORDER BY lower(organization.login), lower(repository.name)
            """
        ).fetchall()
        return {
            "provider": {"id": "github", "name": "GitHub"},
            "repositories": [
                self._repository(session, git_data_plane, row)
                for row in repositories
            ],
        }

    def _repository(
        self,
        session: RelationalSession,
        git_data_plane: GitServiceDataPlane | None,
        repository: Mapping[str, Any],
    ) -> dict[str, Any]:
        repository_id = repository["id"]
        return {
            "id": str(repository_id),
            "provider": "github",
            "name": repository["name"],
            "fullName": repository["full_name"],
            "visibility": "private" if repository["private"] else "public",
            "archived": bool(repository["archived"]),
            "defaultBranch": repository["default_branch"],
            "tickets": self._tickets(session, repository_id),
            "changeSets": self._change_sets(
                session, git_data_plane, repository_id
            ),
            "builds": self._builds(session, repository_id),
        }

    @staticmethod
    def _tickets(
        session: RelationalSession, repository_id: int
    ) -> list[dict[str, Any]]:
        rows = session.execute(
            """
            SELECT issue.*, author.login AS author_login
              FROM github_issues issue
              JOIN github_users author ON author.id = issue.author_id
             WHERE issue.repository_id = ? AND issue.is_pull_request = false
             ORDER BY issue.number DESC
            """,
            (repository_id,),
        ).fetchall()
        tickets: list[dict[str, Any]] = []
        for row in rows:
            labels = session.execute(
                """
                SELECT label.name FROM github_issue_labels linked
                  JOIN github_labels label ON label.id = linked.label_id
                 WHERE linked.issue_id = ? ORDER BY lower(label.name)
                """,
                (row["id"],),
            ).fetchall()
            assignees = session.execute(
                """
                SELECT user_row.login FROM github_issue_assignees linked
                  JOIN github_users user_row ON user_row.id = linked.user_id
                 WHERE linked.issue_id = ? ORDER BY lower(user_row.login)
                """,
                (row["id"],),
            ).fetchall()
            comments = session.execute(
                """
                SELECT comment.*, author.login AS author_login
                  FROM github_issue_comments comment
                  JOIN github_users author ON author.id = comment.author_id
                 WHERE comment.issue_id = ? ORDER BY comment.created_at, comment.id
                """,
                (row["id"],),
            ).fetchall()
            tickets.append(
                {
                    "id": str(row["id"]),
                    "number": row["number"],
                    "title": row["title"],
                    "description": row["body"],
                    "state": row["state"],
                    "stateReason": row["state_reason"],
                    "author": row["author_login"],
                    "labels": [label["name"] for label in labels],
                    "assignees": [assignee["login"] for assignee in assignees],
                    "comments": [
                        {
                            "id": str(comment["id"]),
                            "author": comment["author_login"],
                            "body": comment["body"],
                            "createdAt": comment["created_at"],
                        }
                        for comment in comments
                    ],
                    "createdAt": row["created_at"],
                    "updatedAt": row["updated_at"],
                }
            )
        return tickets

    def _change_sets(
        self,
        session: RelationalSession,
        git_data_plane: GitServiceDataPlane | None,
        repository_id: int,
    ) -> list[dict[str, Any]]:
        rows = session.execute(
            """
            SELECT issue.*, author.login AS author_login, pull.*
              FROM github_pull_requests pull
              JOIN github_issues issue ON issue.id = pull.issue_id
              JOIN github_users author ON author.id = issue.author_id
             WHERE issue.repository_id = ? ORDER BY issue.number DESC
            """,
            (repository_id,),
        ).fetchall()
        return [
            self._change_set(session, git_data_plane, repository_id, row)
            for row in rows
        ]

    def _change_set(
        self,
        session: RelationalSession,
        git_data_plane: GitServiceDataPlane | None,
        repository_id: int,
        row: Mapping[str, Any],
    ) -> dict[str, Any]:
        reviews = session.execute(
            """
            SELECT review.*, reviewer.login AS reviewer_login
              FROM github_pull_request_reviews review
              JOIN github_users reviewer ON reviewer.id = review.reviewer_id
             WHERE review.issue_id = ? ORDER BY review.id
            """,
            (row["issue_id"],),
        ).fetchall()
        comments = session.execute(
            """
            SELECT comment.*, author.login AS author_login
              FROM github_pull_request_review_comments comment
              JOIN github_users author ON author.id = comment.author_id
             WHERE comment.issue_id = ? ORDER BY comment.id
            """,
            (row["issue_id"],),
        ).fetchall()
        diff = {"patch": "", "truncated": False, "available": False}
        if git_data_plane is not None and row["base_sha"] and row["head_sha"]:
            try:
                git_diff = git_data_plane.repository(repository_id).diff(
                    row["base_sha"], row["head_sha"]
                )
                diff = {**git_diff, "available": True}
            except GitStorageError:
                pass
        return {
            "id": str(row["issue_id"]),
            "number": row["number"],
            "title": row["title"],
            "description": row["body"],
            "state": row["state"],
            "author": row["author_login"],
            "draft": bool(row["draft"]),
            "merged": bool(row["merged"]),
            "mergeableState": row["mergeable_state"],
            "base": {"ref": row["base_ref"], "sha": row["base_sha"]},
            "head": {"ref": row["head_ref"], "sha": row["head_sha"]},
            "mergeCommitSha": row["merge_commit_sha"],
            "reviews": [
                {
                    "id": str(review["id"]),
                    "reviewer": review["reviewer_login"],
                    "state": review["state"],
                    "body": review["body"],
                    "commitSha": review["commit_sha"],
                    "submittedAt": review["submitted_at"],
                }
                for review in reviews
            ],
            "reviewComments": [
                {
                    "id": str(comment["id"]),
                    "author": comment["author_login"],
                    "body": comment["body"],
                    "path": comment["path"],
                    "line": comment["line"],
                    "side": comment["side"],
                    "createdAt": comment["created_at"],
                }
                for comment in comments
            ],
            "diff": diff,
            "createdAt": row["created_at"],
            "updatedAt": row["updated_at"],
        }

    @staticmethod
    def _builds(
        session: RelationalSession, repository_id: int
    ) -> list[dict[str, Any]]:
        rows = session.execute(
            """
            SELECT run.*, actor.login AS actor_login
              FROM github_workflow_runs run
              LEFT JOIN github_users actor ON actor.id = run.actor_id
             WHERE run.repository_id = ? ORDER BY run.run_number DESC, run.run_attempt DESC
            """,
            (repository_id,),
        ).fetchall()
        return [
            {
                "id": str(row["id"]),
                "name": row["name"],
                "event": row["event"],
                "status": row["status"],
                "conclusion": row["conclusion"],
                "headBranch": row["head_branch"],
                "headSha": row["head_sha"],
                "runNumber": row["run_number"],
                "runAttempt": row["run_attempt"],
                "actor": row["actor_login"],
                "createdAt": row["created_at"],
                "updatedAt": row["updated_at"],
            }
            for row in rows
        ]
