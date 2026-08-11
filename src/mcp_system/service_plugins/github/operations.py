"""Transactional GitHub domain operations for repositories and issues."""

from __future__ import annotations

from datetime import datetime, timezone
import re
from typing import Any, Callable, Iterable, Mapping, Sequence

from ...errors import ServiceOperationError
from ...git_storage import GitServiceDataPlane, GitServiceDataPlaneTransaction
from ...plugins import RelationalSession
from .plugin import DEFAULT_LABELS


_UNSET = object()
_PERMISSION_RANK = {"pull": 1, "triage": 2, "push": 3, "maintain": 4, "admin": 5}
_REPOSITORY_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")
_LABEL_COLOR = re.compile(r"^[0-9a-fA-F]{6}$")


class GitHubOperationError(ServiceOperationError):
    status_code = 500
    error = "internal_error"

    def __init__(self, message: str) -> None:
        super().__init__(message)

    def to_dict(self) -> dict[str, Any]:
        return {
            "message": self.message,
            "documentation_url": "https://docs.github.com/rest",
            "status": str(self.status_code),
        }


class GitHubNotFound(GitHubOperationError):
    status_code = 404
    error = "not_found"


class GitHubForbidden(GitHubOperationError):
    status_code = 403
    error = "forbidden"


class GitHubConflict(GitHubOperationError):
    status_code = 409
    error = "conflict"


class GitHubValidationError(GitHubOperationError):
    status_code = 422
    error = "validation_failed"


class GitHubOperations:
    """GitHub-compatible state transitions bound to one service transaction."""

    def __init__(
        self,
        session: RelationalSession,
        *,
        actor_login: str,
        now: Callable[[], datetime] | None = None,
        git_data_plane: GitServiceDataPlane | GitServiceDataPlaneTransaction | None = None,
    ) -> None:
        self.session = session
        self.actor_login = actor_login
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.git_data_plane = git_data_plane
        self._actor: Mapping[str, Any] | None = None

    # Users and organizations

    def get_authenticated_user(self) -> dict[str, Any]:
        return self._user_dict(self._require_actor())

    def get_user(self, username: str) -> dict[str, Any]:
        row = self.session.execute(
            "SELECT * FROM github_users WHERE lower(login) = lower(?)",
            (username,),
        ).fetchone()
        if row is None:
            raise GitHubNotFound("Not Found")
        return self._user_dict(row)

    def get_organization(self, organization: str) -> dict[str, Any]:
        row = self._require_organization(organization)
        return self._organization_dict(row)

    def list_organization_members(self, organization: str) -> list[dict[str, Any]]:
        organization_row = self._require_organization(organization)
        self._require_organization_membership(organization_row["id"])
        rows = self.session.execute(
            """
            SELECT user_row.*
              FROM github_organization_members membership
              JOIN github_users user_row ON user_row.id = membership.user_id
             WHERE membership.organization_id = ? AND membership.state = 'active'
             ORDER BY lower(user_row.login)
            """,
            (organization_row["id"],),
        ).fetchall()
        return [self._user_dict(row) for row in rows]

    # Repositories

    def list_repositories(self, organization: str) -> list[dict[str, Any]]:
        organization_row = self._require_organization(organization)
        actor = self._require_actor()
        rows = self.session.execute(
            """
            SELECT repository.*, organization.login AS owner_login,
                   organization.name AS owner_name,
                   collaborator.permission AS actor_permission
              FROM github_repositories repository
              JOIN github_organizations organization
                ON organization.id = repository.owner_organization_id
              LEFT JOIN github_repository_collaborators collaborator
                ON collaborator.repository_id = repository.id
               AND collaborator.user_id = ?
             WHERE repository.owner_organization_id = ?
               AND (repository.private = false OR collaborator.user_id IS NOT NULL)
             ORDER BY lower(repository.name)
            """,
            (actor["id"], organization_row["id"]),
        ).fetchall()
        return [self._repository_dict(row) for row in rows]

    def get_repository(self, owner: str, repository: str) -> dict[str, Any]:
        return self._repository_dict(self._require_repository(owner, repository))

    def create_repository(
        self,
        organization: str,
        *,
        name: str,
        description: str | None = None,
        private: bool = False,
        default_branch: str = "main",
        default_labels: bool = True,
    ) -> dict[str, Any]:
        organization_row = self._require_organization(organization)
        self._require_organization_admin(organization_row["id"])
        if not _REPOSITORY_NAME.fullmatch(name):
            raise GitHubValidationError("Validation Failed: invalid repository name")
        if not default_branch.strip():
            raise GitHubValidationError("Validation Failed: default_branch is required")
        duplicate = self.session.execute(
            """
            SELECT 1 FROM github_repositories
             WHERE owner_organization_id = ? AND lower(name) = lower(?)
            """,
            (organization_row["id"], name),
        ).fetchone()
        if duplicate:
            raise GitHubValidationError("Validation Failed: repository already exists")

        timestamp = self._now_value()
        repository_id = self.session.execute(
            """
            INSERT INTO github_repositories(
                owner_organization_id, name, full_name, description, private,
                archived, default_branch, next_issue_number, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id
            """,
            (
                organization_row["id"],
                name,
                f"{organization_row['login']}/{name}",
                description,
                private,
                False,
                default_branch,
                1,
                timestamp,
                timestamp,
            ),
        ).fetchone()["id"]

        members = self.session.execute(
            """
            SELECT user_id, role FROM github_organization_members
             WHERE organization_id = ? AND state = 'active'
            """,
            (organization_row["id"],),
        ).fetchall()
        if members:
            self.session.executemany(
                """
                INSERT INTO github_repository_collaborators(
                    repository_id, user_id, permission
                ) VALUES (?, ?, ?)
                """,
                [
                    (
                        repository_id,
                        member["user_id"],
                        "admin" if member["role"] == "admin" else "push",
                    )
                    for member in members
                ],
            )
        if default_labels:
            self.session.executemany(
                """
                INSERT INTO github_labels(
                    repository_id, name, color, description, is_default
                ) VALUES (?, ?, ?, ?, ?)
                """,
                [
                    (repository_id, label, color, description, True)
                    for label, color, description in DEFAULT_LABELS
                ],
            )
        if self.git_data_plane is not None:
            self.git_data_plane.repository(repository_id).initialize()
        return self.get_repository(organization_row["login"], name)

    def update_repository(
        self,
        owner: str,
        repository: str,
        *,
        name: str | object = _UNSET,
        description: str | None | object = _UNSET,
        private: bool | object = _UNSET,
        archived: bool | object = _UNSET,
        default_branch: str | object = _UNSET,
    ) -> dict[str, Any]:
        row = self._require_repository(owner, repository, minimum_permission="admin")
        assignments: list[str] = []
        parameters: list[Any] = []
        new_name = row["name"]
        if name is not _UNSET:
            if not isinstance(name, str) or not _REPOSITORY_NAME.fullmatch(name):
                raise GitHubValidationError("Validation Failed: invalid repository name")
            new_name = name
            assignments.extend(("name = ?", "full_name = ?"))
            parameters.extend((name, f"{row['owner_login']}/{name}"))
        if description is not _UNSET:
            if description is not None and not isinstance(description, str):
                raise GitHubValidationError("Validation Failed: invalid description")
            assignments.append("description = ?")
            parameters.append(description)
        for column, value in (("private", private), ("archived", archived)):
            if value is not _UNSET:
                if not isinstance(value, bool):
                    raise GitHubValidationError(f"Validation Failed: {column} must be boolean")
                assignments.append(f"{column} = ?")
                parameters.append(value)
        if default_branch is not _UNSET:
            if not isinstance(default_branch, str) or not default_branch.strip():
                raise GitHubValidationError("Validation Failed: invalid default_branch")
            assignments.append("default_branch = ?")
            parameters.append(default_branch)
        if assignments:
            assignments.append("updated_at = ?")
            parameters.extend((self._now_value(), row["id"]))
            self.session.execute(
                f"UPDATE github_repositories SET {', '.join(assignments)} WHERE id = ?",
                tuple(parameters),
            )
        return self.get_repository(row["owner_login"], new_name)

    # Labels

    def list_labels(self, owner: str, repository: str) -> list[dict[str, Any]]:
        repository_row = self._require_repository(owner, repository)
        rows = self.session.execute(
            """
            SELECT * FROM github_labels WHERE repository_id = ?
             ORDER BY lower(name)
            """,
            (repository_row["id"],),
        ).fetchall()
        return [self._label_dict(row, repository_row) for row in rows]

    def create_label(
        self,
        owner: str,
        repository: str,
        *,
        name: str,
        color: str = "ededed",
        description: str | None = None,
    ) -> dict[str, Any]:
        repository_row = self._require_repository(
            owner, repository, minimum_permission="triage"
        )
        if not name.strip():
            raise GitHubValidationError("Validation Failed: label name is required")
        normalized_color = color.removeprefix("#").lower()
        if not _LABEL_COLOR.fullmatch(normalized_color):
            raise GitHubValidationError("Validation Failed: color must be six hex digits")
        existing = self._find_label(repository_row["id"], name)
        if existing:
            raise GitHubValidationError("Validation Failed: label already exists")
        row = self.session.execute(
            """
            INSERT INTO github_labels(
                repository_id, name, color, description, is_default
            ) VALUES (?, ?, ?, ?, ?) RETURNING *
            """,
            (repository_row["id"], name, normalized_color, description, False),
        ).fetchone()
        return self._label_dict(row, repository_row)

    # Issues

    def list_issues(
        self, owner: str, repository: str, *, state: str = "open"
    ) -> list[dict[str, Any]]:
        repository_row = self._require_repository(owner, repository)
        if state not in ("open", "closed", "all"):
            raise GitHubValidationError("Validation Failed: invalid state")
        statement = "SELECT * FROM github_issues WHERE repository_id = ?"
        parameters: list[Any] = [repository_row["id"]]
        if state != "all":
            statement += " AND state = ?"
            parameters.append(state)
        statement += " ORDER BY number DESC"
        rows = self.session.execute(statement, tuple(parameters)).fetchall()
        return [self._issue_dict(row, repository_row) for row in rows]

    def get_issue(
        self, owner: str, repository: str, issue_number: int
    ) -> dict[str, Any]:
        repository_row = self._require_repository(owner, repository)
        issue = self._require_issue(repository_row["id"], issue_number)
        return self._issue_dict(issue, repository_row)

    def create_issue(
        self,
        owner: str,
        repository: str,
        *,
        title: str,
        body: str | None = None,
        labels: Sequence[str] = (),
        assignees: Sequence[str] = (),
    ) -> dict[str, Any]:
        repository_row = self._require_repository(
            owner, repository, minimum_permission="triage"
        )
        actor = self._require_actor()
        if not title.strip():
            raise GitHubValidationError("Validation Failed: title is required")
        resolved_labels = self._resolve_labels(repository_row["id"], labels)
        resolved_assignees = self._resolve_assignees(repository_row["id"], assignees)
        issue_number = self._allocate_issue_number(repository_row["id"])
        timestamp = self._now_value()
        issue_id = self.session.execute(
            """
            INSERT INTO github_issues(
                repository_id, number, title, body, state, state_reason,
                author_id, locked, is_pull_request, comments_count,
                created_at, updated_at, closed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id
            """,
            (
                repository_row["id"],
                issue_number,
                title,
                body,
                "open",
                None,
                actor["id"],
                False,
                False,
                0,
                timestamp,
                timestamp,
                None,
            ),
        ).fetchone()["id"]
        self._insert_issue_labels(issue_id, resolved_labels)
        self._insert_issue_assignees(issue_id, resolved_assignees)
        return self.get_issue(owner, repository, issue_number)

    def update_issue(
        self,
        owner: str,
        repository: str,
        issue_number: int,
        *,
        title: str | object = _UNSET,
        body: str | None | object = _UNSET,
        state: str | object = _UNSET,
        state_reason: str | None | object = _UNSET,
        labels: Sequence[str] | object = _UNSET,
        assignees: Sequence[str] | object = _UNSET,
    ) -> dict[str, Any]:
        repository_row = self._require_repository(
            owner, repository, minimum_permission="triage"
        )
        issue = self._require_issue(repository_row["id"], issue_number)
        assignments: list[str] = []
        parameters: list[Any] = []
        if title is not _UNSET:
            if not isinstance(title, str) or not title.strip():
                raise GitHubValidationError("Validation Failed: title is required")
            assignments.append("title = ?")
            parameters.append(title)
        if body is not _UNSET:
            if body is not None and not isinstance(body, str):
                raise GitHubValidationError("Validation Failed: invalid body")
            assignments.append("body = ?")
            parameters.append(body)
        if state is not _UNSET:
            if state not in ("open", "closed"):
                raise GitHubValidationError("Validation Failed: invalid state")
            assignments.extend(("state = ?", "closed_at = ?"))
            parameters.extend((state, self._now_value() if state == "closed" else None))
            if state_reason is _UNSET:
                state_reason = "completed" if state == "closed" else "reopened"
        if state_reason is not _UNSET:
            if state_reason not in (None, "completed", "not_planned", "reopened"):
                raise GitHubValidationError("Validation Failed: invalid state_reason")
            target_state = state if state is not _UNSET else issue["state"]
            if state_reason in ("completed", "not_planned") and target_state != "closed":
                raise GitHubValidationError(
                    "Validation Failed: closed state_reason requires state=closed"
                )
            if state_reason == "reopened" and target_state != "open":
                raise GitHubValidationError(
                    "Validation Failed: reopened state_reason requires state=open"
                )
            assignments.append("state_reason = ?")
            parameters.append(state_reason)
        if assignments:
            assignments.append("updated_at = ?")
            parameters.extend((self._now_value(), issue["id"]))
            self.session.execute(
                f"UPDATE github_issues SET {', '.join(assignments)} WHERE id = ?",
                tuple(parameters),
            )
        if labels is not _UNSET:
            self.set_issue_labels(owner, repository, issue_number, labels)  # type: ignore[arg-type]
        if assignees is not _UNSET:
            current = self.session.execute(
                "SELECT user_id FROM github_issue_assignees WHERE issue_id = ?",
                (issue["id"],),
            ).fetchall()
            if current:
                self.session.execute(
                    "DELETE FROM github_issue_assignees WHERE issue_id = ?",
                    (issue["id"],),
                )
            self._insert_issue_assignees(
                issue["id"],
                self._resolve_assignees(repository_row["id"], assignees),  # type: ignore[arg-type]
            )
        return self.get_issue(owner, repository, issue_number)

    # Issue labels and assignees

    def list_issue_labels(
        self, owner: str, repository: str, issue_number: int
    ) -> list[dict[str, Any]]:
        repository_row = self._require_repository(owner, repository)
        issue = self._require_issue(repository_row["id"], issue_number)
        return self._issue_labels(issue["id"], repository_row)

    def add_issue_labels(
        self,
        owner: str,
        repository: str,
        issue_number: int,
        labels: Sequence[str],
    ) -> list[dict[str, Any]]:
        repository_row = self._require_repository(
            owner, repository, minimum_permission="triage"
        )
        issue = self._require_issue(repository_row["id"], issue_number)
        self._insert_issue_labels(
            issue["id"], self._resolve_labels(repository_row["id"], labels)
        )
        return self._issue_labels(issue["id"], repository_row)

    def set_issue_labels(
        self,
        owner: str,
        repository: str,
        issue_number: int,
        labels: Sequence[str],
    ) -> list[dict[str, Any]]:
        repository_row = self._require_repository(
            owner, repository, minimum_permission="triage"
        )
        issue = self._require_issue(repository_row["id"], issue_number)
        resolved = self._resolve_labels(repository_row["id"], labels)
        self.session.execute(
            "DELETE FROM github_issue_labels WHERE issue_id = ?", (issue["id"],)
        )
        self._insert_issue_labels(issue["id"], resolved)
        return self._issue_labels(issue["id"], repository_row)

    def remove_issue_label(
        self, owner: str, repository: str, issue_number: int, name: str
    ) -> list[dict[str, Any]]:
        repository_row = self._require_repository(
            owner, repository, minimum_permission="triage"
        )
        issue = self._require_issue(repository_row["id"], issue_number)
        label_row = self._find_label(repository_row["id"], name)
        if label_row is None:
            raise GitHubNotFound("Label does not exist")
        linked = self.session.execute(
            "SELECT 1 FROM github_issue_labels WHERE issue_id = ? AND label_id = ?",
            (issue["id"], label_row["id"]),
        ).fetchone()
        if linked is None:
            raise GitHubNotFound("Label does not exist on this issue")
        self.session.execute(
            "DELETE FROM github_issue_labels WHERE issue_id = ? AND label_id = ?",
            (issue["id"], label_row["id"]),
        )
        return self._issue_labels(issue["id"], repository_row)

    def remove_all_issue_labels(
        self, owner: str, repository: str, issue_number: int
    ) -> None:
        repository_row = self._require_repository(
            owner, repository, minimum_permission="triage"
        )
        issue = self._require_issue(repository_row["id"], issue_number)
        self.session.execute(
            "DELETE FROM github_issue_labels WHERE issue_id = ?", (issue["id"],)
        )

    def add_assignees(
        self,
        owner: str,
        repository: str,
        issue_number: int,
        assignees: Sequence[str],
    ) -> dict[str, Any]:
        repository_row = self._require_repository(
            owner, repository, minimum_permission="triage"
        )
        issue = self._require_issue(repository_row["id"], issue_number)
        self._insert_issue_assignees(
            issue["id"],
            self._resolve_assignees(repository_row["id"], assignees),
        )
        return self.get_issue(owner, repository, issue_number)

    def remove_assignees(
        self,
        owner: str,
        repository: str,
        issue_number: int,
        assignees: Sequence[str],
    ) -> dict[str, Any]:
        repository_row = self._require_repository(
            owner, repository, minimum_permission="triage"
        )
        issue = self._require_issue(repository_row["id"], issue_number)
        resolved = self._resolve_assignees(repository_row["id"], assignees)
        for user in resolved:
            self.session.execute(
                "DELETE FROM github_issue_assignees WHERE issue_id = ? AND user_id = ?",
                (issue["id"], user["id"]),
            )
        return self.get_issue(owner, repository, issue_number)

    # Comments

    def list_comments(
        self, owner: str, repository: str, issue_number: int
    ) -> list[dict[str, Any]]:
        repository_row = self._require_repository(owner, repository)
        issue = self._require_issue(repository_row["id"], issue_number)
        rows = self.session.execute(
            """
            SELECT comment.*, user_row.login AS author_login,
                   user_row.user_type AS author_type,
                   user_row.site_admin AS author_site_admin
              FROM github_issue_comments comment
              JOIN github_users user_row ON user_row.id = comment.author_id
             WHERE comment.issue_id = ? ORDER BY comment.created_at, comment.id
            """,
            (issue["id"],),
        ).fetchall()
        return [self._comment_dict(row, repository_row, issue_number) for row in rows]

    def create_comment(
        self, owner: str, repository: str, issue_number: int, *, body: str
    ) -> dict[str, Any]:
        repository_row = self._require_repository(
            owner, repository, minimum_permission="pull"
        )
        issue = self._require_issue(repository_row["id"], issue_number)
        actor = self._require_actor()
        if not body.strip():
            raise GitHubValidationError("Validation Failed: body is required")
        timestamp = self._now_value()
        comment_id = self.session.execute(
            """
            INSERT INTO github_issue_comments(
                issue_id, author_id, body, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?) RETURNING id
            """,
            (issue["id"], actor["id"], body, timestamp, timestamp),
        ).fetchone()["id"]
        self.session.execute(
            """
            UPDATE github_issues
               SET comments_count = comments_count + 1, updated_at = ?
             WHERE id = ?
            """,
            (timestamp, issue["id"]),
        )
        row = self.session.execute(
            """
            SELECT comment.*, user_row.login AS author_login,
                   user_row.user_type AS author_type,
                   user_row.site_admin AS author_site_admin
              FROM github_issue_comments comment
              JOIN github_users user_row ON user_row.id = comment.author_id
             WHERE comment.id = ?
            """,
            (comment_id,),
        ).fetchone()
        return self._comment_dict(row, repository_row, issue_number)

    # Resolution and serialization helpers

    def _require_actor(self) -> Mapping[str, Any]:
        if self._actor is None:
            actor = self.session.execute(
                "SELECT * FROM github_users WHERE lower(login) = lower(?)",
                (self.actor_login,),
            ).fetchone()
            if actor is None:
                raise GitHubForbidden("Resource not accessible by integration")
            self._actor = actor
        return self._actor

    def _require_organization(self, login: str) -> Mapping[str, Any]:
        row = self.session.execute(
            "SELECT * FROM github_organizations WHERE lower(login) = lower(?)",
            (login,),
        ).fetchone()
        if row is None:
            raise GitHubNotFound("Not Found")
        return row

    def _require_organization_membership(self, organization_id: int) -> Mapping[str, Any]:
        actor = self._require_actor()
        membership = self.session.execute(
            """
            SELECT * FROM github_organization_members
             WHERE organization_id = ? AND user_id = ? AND state = 'active'
            """,
            (organization_id, actor["id"]),
        ).fetchone()
        if membership is None:
            raise GitHubForbidden("Resource not accessible by integration")
        return membership

    def _require_organization_admin(self, organization_id: int) -> None:
        membership = self._require_organization_membership(organization_id)
        if membership["role"] != "admin":
            raise GitHubForbidden("Resource not accessible by integration")

    def _require_repository(
        self,
        owner: str,
        repository: str,
        *,
        minimum_permission: str | None = None,
    ) -> Mapping[str, Any]:
        actor = self._require_actor()
        row = self.session.execute(
            """
            SELECT repository.*, organization.login AS owner_login,
                   organization.name AS owner_name,
                   collaborator.permission AS actor_permission
              FROM github_repositories repository
              JOIN github_organizations organization
                ON organization.id = repository.owner_organization_id
              LEFT JOIN github_repository_collaborators collaborator
                ON collaborator.repository_id = repository.id
               AND collaborator.user_id = ?
             WHERE lower(organization.login) = lower(?)
               AND lower(repository.name) = lower(?)
            """,
            (actor["id"], owner, repository),
        ).fetchone()
        if row is None or (row["private"] and row["actor_permission"] is None):
            raise GitHubNotFound("Not Found")
        if minimum_permission is not None:
            actual = row["actor_permission"]
            if actual is None or _PERMISSION_RANK[actual] < _PERMISSION_RANK[minimum_permission]:
                raise GitHubForbidden("Resource not accessible by integration")
        return row

    def _require_issue(self, repository_id: int, issue_number: int) -> Mapping[str, Any]:
        row = self.session.execute(
            """
            SELECT * FROM github_issues
             WHERE repository_id = ? AND number = ?
            """,
            (repository_id, issue_number),
        ).fetchone()
        if row is None:
            raise GitHubNotFound("Not Found")
        return row

    def _allocate_issue_number(self, repository_id: int) -> int:
        number_row = self.session.execute(
            """
            UPDATE github_repositories
               SET next_issue_number = next_issue_number + 1, updated_at = ?
             WHERE id = ? RETURNING next_issue_number
            """,
            (self._now_value(), repository_id),
        ).fetchone()
        if number_row is None:
            raise GitHubNotFound("Not Found")
        return number_row["next_issue_number"] - 1

    def _find_label(self, repository_id: int, name: str) -> Mapping[str, Any] | None:
        return self.session.execute(
            """
            SELECT * FROM github_labels
             WHERE repository_id = ? AND lower(name) = lower(?)
            """,
            (repository_id, name),
        ).fetchone()

    def _resolve_labels(
        self, repository_id: int, names: Sequence[str]
    ) -> list[Mapping[str, Any]]:
        result: list[Mapping[str, Any]] = []
        seen: set[int] = set()
        for name in names:
            label = self._find_label(repository_id, name)
            if label is None:
                raise GitHubValidationError(f"Validation Failed: label {name!r} does not exist")
            if label["id"] not in seen:
                result.append(label)
                seen.add(label["id"])
        return result

    def _resolve_assignees(
        self, repository_id: int, logins: Sequence[str]
    ) -> list[Mapping[str, Any]]:
        result: list[Mapping[str, Any]] = []
        seen: set[int] = set()
        for login in logins:
            user = self.session.execute(
                "SELECT * FROM github_users WHERE lower(login) = lower(?)", (login,)
            ).fetchone()
            if user is None:
                raise GitHubValidationError(
                    f"Validation Failed: assignee {login!r} does not exist"
                )
            collaborator = self.session.execute(
                """
                SELECT 1 FROM github_repository_collaborators
                 WHERE repository_id = ? AND user_id = ?
                """,
                (repository_id, user["id"]),
            ).fetchone()
            if collaborator is None:
                raise GitHubValidationError(
                    f"Validation Failed: {login!r} is not a collaborator"
                )
            if user["id"] not in seen:
                result.append(user)
                seen.add(user["id"])
        return result

    def _insert_issue_labels(
        self, issue_id: int, labels: Iterable[Mapping[str, Any]]
    ) -> None:
        for label in labels:
            self.session.execute(
                """
                INSERT INTO github_issue_labels(issue_id, label_id)
                VALUES (?, ?) ON CONFLICT DO NOTHING
                """,
                (issue_id, label["id"]),
            )

    def _insert_issue_assignees(
        self, issue_id: int, users: Iterable[Mapping[str, Any]]
    ) -> None:
        for user in users:
            self.session.execute(
                """
                INSERT INTO github_issue_assignees(issue_id, user_id)
                VALUES (?, ?) ON CONFLICT DO NOTHING
                """,
                (issue_id, user["id"]),
            )

    def _issue_labels(
        self, issue_id: int, repository: Mapping[str, Any]
    ) -> list[dict[str, Any]]:
        rows = self.session.execute(
            """
            SELECT label.* FROM github_issue_labels issue_label
              JOIN github_labels label ON label.id = issue_label.label_id
             WHERE issue_label.issue_id = ? ORDER BY lower(label.name)
            """,
            (issue_id,),
        ).fetchall()
        return [self._label_dict(row, repository) for row in rows]

    def _issue_assignees(self, issue_id: int) -> list[dict[str, Any]]:
        rows = self.session.execute(
            """
            SELECT user_row.* FROM github_issue_assignees assignee
              JOIN github_users user_row ON user_row.id = assignee.user_id
             WHERE assignee.issue_id = ? ORDER BY lower(user_row.login)
            """,
            (issue_id,),
        ).fetchall()
        return [self._user_dict(row) for row in rows]

    @staticmethod
    def _user_dict(row: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "login": row["login"],
            "id": row["id"],
            "node_id": f"U_{row['id']}",
            "name": row["name"],
            "email": row["email"],
            "type": row["user_type"],
            "site_admin": bool(row["site_admin"]),
            "url": f"https://api.github.com/users/{row['login']}",
            "html_url": f"https://github.com/{row['login']}",
        }

    @staticmethod
    def _organization_dict(row: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "login": row["login"],
            "id": row["id"],
            "node_id": f"O_{row['id']}",
            "name": row["name"],
            "url": f"https://api.github.com/orgs/{row['login']}",
            "repos_url": f"https://api.github.com/orgs/{row['login']}/repos",
        }

    def _repository_dict(self, row: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "id": row["id"],
            "node_id": f"R_{row['id']}",
            "name": row["name"],
            "full_name": row["full_name"],
            "private": bool(row["private"]),
            "archived": bool(row["archived"]),
            "description": row["description"],
            "default_branch": row["default_branch"],
            "owner": {
                "login": row["owner_login"],
                "type": "Organization",
                "url": f"https://api.github.com/orgs/{row['owner_login']}",
            },
            "url": f"https://api.github.com/repos/{row['full_name']}",
            "html_url": f"https://github.com/{row['full_name']}",
            "created_at": self._serialize_time(row["created_at"]),
            "updated_at": self._serialize_time(row["updated_at"]),
        }

    @staticmethod
    def _label_dict(
        row: Mapping[str, Any], repository: Mapping[str, Any]
    ) -> dict[str, Any]:
        return {
            "id": row["id"],
            "node_id": f"L_{row['id']}",
            "name": row["name"],
            "color": row["color"],
            "description": row["description"],
            "default": bool(row["is_default"]),
            "url": f"https://api.github.com/repos/{repository['full_name']}/labels/{row['name']}",
        }

    def _issue_dict(
        self, row: Mapping[str, Any], repository: Mapping[str, Any]
    ) -> dict[str, Any]:
        author = self.session.execute(
            "SELECT * FROM github_users WHERE id = ?", (row["author_id"],)
        ).fetchone()
        assignees = self._issue_assignees(row["id"])
        result = {
            "id": row["id"],
            "node_id": f"I_{row['id']}",
            "number": row["number"],
            "title": row["title"],
            "body": row["body"],
            "state": row["state"],
            "state_reason": row["state_reason"],
            "locked": bool(row["locked"]),
            "comments": row["comments_count"],
            "user": self._user_dict(author),
            "labels": self._issue_labels(row["id"], repository),
            "assignee": assignees[0] if assignees else None,
            "assignees": assignees,
            "created_at": self._serialize_time(row["created_at"]),
            "updated_at": self._serialize_time(row["updated_at"]),
            "closed_at": self._serialize_time(row["closed_at"]),
            "url": f"https://api.github.com/repos/{repository['full_name']}/issues/{row['number']}",
            "html_url": f"https://github.com/{repository['full_name']}/issues/{row['number']}",
            "repository_url": f"https://api.github.com/repos/{repository['full_name']}",
        }
        if row["is_pull_request"]:
            result["pull_request"] = {
                "url": f"https://api.github.com/repos/{repository['full_name']}/pulls/{row['number']}",
                "html_url": f"https://github.com/{repository['full_name']}/pull/{row['number']}",
            }
        return result

    def _comment_dict(
        self,
        row: Mapping[str, Any],
        repository: Mapping[str, Any],
        issue_number: int,
    ) -> dict[str, Any]:
        return {
            "id": row["id"],
            "node_id": f"IC_{row['id']}",
            "body": row["body"],
            "user": {
                "login": row["author_login"],
                "id": row["author_id"],
                "type": row["author_type"],
                "site_admin": bool(row["author_site_admin"]),
            },
            "created_at": self._serialize_time(row["created_at"]),
            "updated_at": self._serialize_time(row["updated_at"]),
            "url": f"https://api.github.com/repos/{repository['full_name']}/issues/comments/{row['id']}",
            "html_url": f"https://github.com/{repository['full_name']}/issues/{issue_number}#issuecomment-{row['id']}",
        }

    def _now_value(self) -> str:
        return self.now().astimezone(timezone.utc).isoformat()

    @staticmethod
    def _serialize_time(value: Any) -> str | None:
        if value is None:
            return None
        if isinstance(value, datetime):
            return value.isoformat()
        return str(value)
