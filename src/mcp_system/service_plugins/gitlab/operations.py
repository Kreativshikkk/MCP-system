"""Transactional operations for the bounded GitLab v4 domain."""

from __future__ import annotations

import base64
from datetime import datetime, timezone
import hashlib
from typing import Any, Callable, Mapping, Sequence
from uuid import uuid4

from ...errors import ServiceOperationError
from ...git_storage import GitServiceDataPlane, GitServiceDataPlaneTransaction
from ...plugins import RelationalSession


class GitLabOperationError(ServiceOperationError):
    status_code = 500
    error = "internal_error"

    def to_dict(self) -> dict[str, Any]:
        return {"message": self.message}


class GitLabNotFound(GitLabOperationError):
    status_code = 404
    error = "not_found"


class GitLabForbidden(GitLabOperationError):
    status_code = 403
    error = "forbidden"


class GitLabConflict(GitLabOperationError):
    status_code = 409
    error = "conflict"


class GitLabValidationError(GitLabOperationError):
    status_code = 400
    error = "validation_failed"


class GitLabOperations:
    def __init__(self, session: RelationalSession, *, actor_username: str,
                 now: Callable[[], datetime] | None = None,
                 git_data_plane: GitServiceDataPlane | GitServiceDataPlaneTransaction | None = None) -> None:
        self.session = session
        self.actor_username = actor_username
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.git_data_plane = git_data_plane
        self._actor: Mapping[str, Any] | None = None

    def get_current_user(self) -> dict[str, Any]:
        return self._user(self._require_actor())

    def get_user(self, username: str) -> dict[str, Any]:
        row = self.session.execute("SELECT * FROM gitlab_users WHERE lower(username)=lower(?)", (username,)).fetchone()
        if row is None:
            raise GitLabNotFound("404 User Not Found")
        return self._user(row)

    def list_users(self, *, username: str | None = None, search: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM gitlab_users WHERE 1=1"
        parameters: list[Any] = []
        if username is not None:
            sql += " AND lower(username)=lower(?)"
            parameters.append(username)
        if search is not None:
            sql += " AND (lower(username) LIKE lower(?) OR lower(name) LIKE lower(?))"
            parameters.extend((f"%{search}%", f"%{search}%"))
        return [self._user(row) for row in self.session.execute(sql + " ORDER BY id", parameters).fetchall()]

    def get_group(self, group: str) -> dict[str, Any]:
        row = self._require_group(group)
        return {"id": row["id"], "name": row["name"], "path": row["path"], "visibility": row["visibility"]}

    def list_group_members(self, group: str) -> list[dict[str, Any]]:
        namespace = self._require_group(group)
        rows = self.session.execute("""SELECT u.*, m.access_level FROM gitlab_group_members m JOIN gitlab_users u ON u.id=m.user_id
            WHERE m.namespace_id=? ORDER BY lower(u.username)""", (namespace["id"],)).fetchall()
        return [{**self._user(row), "access_level": row["access_level"]} for row in rows]

    def list_projects(self, group: str) -> list[dict[str, Any]]:
        namespace = self._require_group(group)
        rows = self.session.execute("SELECT * FROM gitlab_projects WHERE namespace_id=? ORDER BY lower(path)", (namespace["id"],)).fetchall()
        return [self._project(row) for row in rows if self._has_project_access(row["id"])]

    def get_project(self, project: str) -> dict[str, Any]:
        return self._project(self._require_project(project))

    def list_labels(self, project: str) -> list[dict[str, Any]]:
        row = self._require_project(project)
        labels = self.session.execute("SELECT * FROM gitlab_labels WHERE project_id=? ORDER BY lower(name)", (row["id"],)).fetchall()
        return [self._label(label) for label in labels]

    def create_label(self, project: str, *, name: str, color: str,
                     description: str | None = None) -> dict[str, Any]:
        project_row = self._require_project(project, 40)
        if not name.strip() or not color.strip():
            raise GitLabValidationError("name and color are required")
        if not color.startswith("#"):
            color = f"#{color}"
        try:
            label_id = self.session.execute(
                "INSERT INTO gitlab_labels(project_id,name,color,description) VALUES(?,?,?,?) RETURNING id",
                (project_row["id"], name, color, description),
            ).fetchone()["id"]
        except Exception as exc:
            raise GitLabConflict("Label already exists") from exc
        return self._label(self.session.execute("SELECT * FROM gitlab_labels WHERE id=?", (label_id,)).fetchone())

    def update_label(self, project: str, *, name: str, new_name: str | None = None,
                     color: str | None = None, description: str | None = None) -> dict[str, Any]:
        project_row = self._require_project(project, 40)
        current = self.session.execute("SELECT * FROM gitlab_labels WHERE project_id=? AND name=?", (project_row["id"], name)).fetchone()
        if current is None:
            raise GitLabNotFound("404 Label Not Found")
        normalized_color = color or current["color"]
        if not normalized_color.startswith("#"):
            normalized_color = f"#{normalized_color}"
        self.session.execute("UPDATE gitlab_labels SET name=?,color=?,description=? WHERE id=?", (new_name or current["name"], normalized_color, description if description is not None else current["description"], current["id"]))
        return self._label(self.session.execute("SELECT * FROM gitlab_labels WHERE id=?", (current["id"],)).fetchone())

    def delete_label(self, project: str, *, name: str) -> dict[str, Any]:
        project_row = self._require_project(project, 40)
        label = self.session.execute("SELECT id FROM gitlab_labels WHERE project_id=? AND name=?", (project_row["id"], name)).fetchone()
        if label is None:
            raise GitLabNotFound("404 Label Not Found")
        deleted = self._label(self.session.execute("SELECT * FROM gitlab_labels WHERE id=?", (label["id"],)).fetchone())
        self.session.execute("DELETE FROM gitlab_issue_labels WHERE label_id=?", (label["id"],))
        self.session.execute("DELETE FROM gitlab_labels WHERE id=?", (label["id"],))
        return deleted

    def create_issue(self, project: str, *, title: str, description: str | None = None,
                     labels: Sequence[str] | str = (), assignee: str | None = None,
                     assignee_ids: Sequence[int] = ()) -> dict[str, Any]:
        project_row = self._require_project(project, 30)
        if not title.strip():
            raise GitLabValidationError("title is required")
        actor = self._require_actor()
        if assignee_ids:
            assignee_row = self.session.execute("SELECT * FROM gitlab_users WHERE id=?", (assignee_ids[0],)).fetchone()
            if assignee_row is None:
                raise GitLabValidationError("assignee_ids contains an unknown user")
            assignee_id = assignee_row["id"]
        else:
            assignee_id = self._require_user(assignee)["id"] if assignee else None
        iid = project_row["next_issue_iid"]
        timestamp = self._now()
        issue_id = self.session.execute("""INSERT INTO gitlab_issues(project_id,iid,title,description,state,author_id,assignee_id,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?) RETURNING id""", (project_row["id"], iid, title, description, "opened", actor["id"], assignee_id, timestamp, timestamp)).fetchone()["id"]
        self.session.execute("UPDATE gitlab_projects SET next_issue_iid=next_issue_iid+1,updated_at=? WHERE id=?", (timestamp, project_row["id"]))
        normalized_labels = tuple(item.strip() for item in labels.split(",")) if isinstance(labels, str) else labels
        self._set_issue_labels(issue_id, project_row["id"], normalized_labels)
        return self.get_issue(project, iid)

    def list_issues(self, project: str, *, state: str = "all") -> list[dict[str, Any]]:
        row = self._require_project(project)
        sql = "SELECT * FROM gitlab_issues WHERE project_id=?"
        params: list[Any] = [row["id"]]
        if state != "all":
            sql += " AND state=?"
            params.append(state)
        return [self._issue(item) for item in self.session.execute(sql + " ORDER BY iid DESC", params).fetchall()]

    def get_issue(self, project: str, issue_iid: int) -> dict[str, Any]:
        project_row = self._require_project(project)
        row = self.session.execute("SELECT * FROM gitlab_issues WHERE project_id=? AND iid=?", (project_row["id"], issue_iid)).fetchone()
        if row is None:
            raise GitLabNotFound("404 Issue Not Found")
        return self._issue(row)

    def update_issue(self, project: str, issue_iid: int, *, state_event: str | None = None,
                     title: str | None = None, description: str | None = None,
                     labels: str | None = None, add_labels: str | None = None,
                     remove_labels: str | None = None,
                     assignee_ids: Sequence[int] | None = None) -> dict[str, Any]:
        project_row = self._require_project(project, 30)
        current = self.get_issue(project, issue_iid)
        state = current["state"]
        if state_event:
            if state_event not in {"close", "reopen"}:
                raise GitLabValidationError("state_event must be close or reopen")
            state = "closed" if state_event == "close" else "opened"
        timestamp = self._now()
        self.session.execute("UPDATE gitlab_issues SET title=?,description=?,state=?,updated_at=?,closed_at=? WHERE project_id=? AND iid=?",
            (title if title is not None else current["title"], description if description is not None else current["description"], state, timestamp, timestamp if state == "closed" else None, project_row["id"], issue_iid))
        issue_id = current["id"]
        if labels is not None:
            self.session.execute("DELETE FROM gitlab_issue_labels WHERE issue_id=?", (issue_id,))
            self._set_issue_labels(issue_id, project_row["id"], tuple(value.strip() for value in labels.split(",") if value.strip()))
        if add_labels:
            existing = {row["name"].casefold() for row in self.session.execute("SELECT l.name FROM gitlab_issue_labels il JOIN gitlab_labels l ON l.id=il.label_id WHERE il.issue_id=?", (issue_id,)).fetchall()}
            self._set_issue_labels(issue_id, project_row["id"], tuple(value.strip() for value in add_labels.split(",") if value.strip().casefold() not in existing))
        if remove_labels:
            for value in remove_labels.split(","):
                self.session.execute("DELETE FROM gitlab_issue_labels WHERE issue_id=? AND label_id IN (SELECT id FROM gitlab_labels WHERE project_id=? AND lower(name)=lower(?))", (issue_id, project_row["id"], value.strip()))
        if assignee_ids is not None:
            assignee_id = None
            if assignee_ids:
                assignee = self.session.execute("SELECT id FROM gitlab_users WHERE id=?", (assignee_ids[0],)).fetchone()
                if assignee is None:
                    raise GitLabValidationError("assignee_ids contains an unknown user")
                assignee_id = assignee["id"]
            self.session.execute("UPDATE gitlab_issues SET assignee_id=? WHERE id=?", (assignee_id, issue_id))
        return self.get_issue(project, issue_iid)

    def delete_issue(self, project: str, issue_iid: int) -> None:
        project_row = self._require_project(project, 30)
        issue = self.get_issue(project, issue_iid)
        note_ids = self.session.execute("SELECT id FROM gitlab_notes WHERE noteable_type='Issue' AND noteable_id=?", (issue["id"],)).fetchall()
        for note in note_ids:
            self.session.execute("DELETE FROM gitlab_discussion_notes WHERE note_id=?", (note["id"],))
        discussions = self.session.execute("SELECT id FROM gitlab_discussions WHERE noteable_type='Issue' AND noteable_id=?", (issue["id"],)).fetchall()
        for discussion in discussions:
            self.session.execute("DELETE FROM gitlab_discussions WHERE id=?", (discussion["id"],))
        self.session.execute("DELETE FROM gitlab_notes WHERE noteable_type='Issue' AND noteable_id=?", (issue["id"],))
        self.session.execute("DELETE FROM gitlab_issue_labels WHERE issue_id=?", (issue["id"],))
        self.session.execute("DELETE FROM gitlab_issues WHERE project_id=? AND iid=?", (project_row["id"], issue_iid))

    def create_issue_note(self, project: str, issue_iid: int, *, body: str) -> dict[str, Any]:
        project_row = self._require_project(project, 30)
        issue = self.get_issue(project, issue_iid)
        return self._create_note(project_row["id"], "Issue", issue["id"], body)

    def list_issue_notes(self, project: str, issue_iid: int) -> list[dict[str, Any]]:
        issue = self.get_issue(project, issue_iid)
        return self._list_notes("Issue", issue["id"])

    def get_issue_note(self, project: str, issue_iid: int, note_id: int) -> dict[str, Any]:
        issue = self.get_issue(project, issue_iid)
        return self._require_note("Issue", issue["id"], note_id)

    def update_issue_note(self, project: str, issue_iid: int, note_id: int, *, body: str) -> dict[str, Any]:
        self._require_project(project, 30)
        issue = self.get_issue(project, issue_iid)
        self._require_note("Issue", issue["id"], note_id)
        self.session.execute("UPDATE gitlab_notes SET body=?,updated_at=? WHERE id=?", (body, self._now(), note_id))
        return self._require_note("Issue", issue["id"], note_id)

    def delete_issue_note(self, project: str, issue_iid: int, note_id: int) -> dict[str, Any]:
        self._require_project(project, 30)
        issue = self.get_issue(project, issue_iid)
        deleted = self._require_note("Issue", issue["id"], note_id)
        self.session.execute("DELETE FROM gitlab_discussion_notes WHERE note_id=?", (note_id,))
        self.session.execute("DELETE FROM gitlab_notes WHERE id=?", (note_id,))
        return deleted

    def create_commit(self, project: str, *, message: str, author: str,
                      parent_shas: Sequence[str] = (), files: Mapping[str, str | bytes | None] | None = None) -> dict[str, Any]:
        project_row = self._require_project(project, 30)
        if self.git_data_plane is None:
            raise GitLabConflict("Git data plane is not configured")
        author_row = self._require_user(author)
        for sha in parent_shas:
            self._require_commit(project_row["id"], sha)
        created = self.git_data_plane.repository(project_row["id"]).create_commit(
            message=message, author_name=author_row["name"], author_email=author_row["email"] or f"{author}@localhost",
            timestamp=self.now(), parent_shas=tuple(parent_shas), files=files)
        timestamp = self._now()
        self.session.execute("INSERT INTO gitlab_commits(sha,project_id,tree_sha,message,author_id,committed_at) VALUES(?,?,?,?,?,?)",
            (created["sha"], project_row["id"], created["tree_sha"], message, author_row["id"], timestamp))
        for position, parent in enumerate(created["parents"]):
            self.session.execute("INSERT INTO gitlab_commit_parents(commit_sha,parent_sha,position) VALUES(?,?,?)", (created["sha"], parent, position))
        return {"id": created["sha"], "sha": created["sha"], "short_id": created["sha"][:8], "title": message.splitlines()[0], "message": message, "parent_ids": list(created["parents"])}

    def create_repository_commit(self, project: str, *, branch: str,
                                 commit_message: str,
                                 actions: Sequence[Mapping[str, Any]],
                                 start_branch: str | None = None,
                                 start_sha: str | None = None) -> dict[str, Any]:
        """Implement POST /repository/commits using GitLab's action vocabulary."""
        project_row = self._require_project(project, 30)
        branch_row = self.session.execute(
            "SELECT * FROM gitlab_branches WHERE project_id=? AND name=?",
            (project_row["id"], branch),
        ).fetchone()
        parent_sha: str | None = branch_row["head_sha"] if branch_row else None
        if branch_row is None:
            if start_branch is not None and start_sha is not None:
                raise GitLabValidationError(
                    "start_branch and start_sha are mutually exclusive"
                )
            if start_branch is not None:
                parent_sha = self._require_branch(
                    project_row["id"], start_branch
                )["head_sha"]
            elif start_sha is not None:
                parent_sha = self._require_commit(
                    project_row["id"], start_sha
                )["sha"]
            else:
                existing = self.session.execute(
                    "SELECT count(*) AS count FROM gitlab_commits WHERE project_id=?",
                    (project_row["id"],),
                ).fetchone()["count"]
                if existing:
                    raise GitLabNotFound("404 Branch Not Found")
        repository = self._git_repository(project_row["id"])
        files = self._files_from_actions(repository, parent_sha, actions)
        commit = self.create_commit(
            project, message=commit_message, author=self.actor_username,
            parent_shas=(parent_sha,) if parent_sha else (), files=files,
        )
        if branch_row is None:
            self.session.execute(
                "INSERT INTO gitlab_branches(project_id,name,head_sha,protected) VALUES(?,?,?,?)",
                (project_row["id"], branch, commit["id"], False),
            )
            repository.update_branch(branch, commit["id"])
        else:
            self.session.execute("UPDATE gitlab_branches SET head_sha=? WHERE project_id=? AND name=?", (commit["id"], project_row["id"], branch))
            repository.update_branch(branch, commit["id"], expected_old_sha=branch_row["head_sha"])
        return self.get_commit(project, commit["id"])

    def list_commits(self, project: str, *, ref_name: str | None = None) -> list[dict[str, Any]]:
        project_row = self._require_project(project)
        if ref_name:
            self._require_branch(project_row["id"], ref_name)
        rows = self.session.execute("SELECT * FROM gitlab_commits WHERE project_id=? ORDER BY committed_at DESC,sha", (project_row["id"],)).fetchall()
        return [self._commit(row) for row in rows]

    def get_commit(self, project: str, sha: str) -> dict[str, Any]:
        project_row = self._require_project(project)
        return self._commit(self._require_commit(project_row["id"], sha))

    def get_commit_diff(self, project: str, sha: str) -> list[dict[str, Any]]:
        project_row = self._require_project(project)
        commit = self._require_commit(project_row["id"], sha)
        parents = self.session.execute("SELECT parent_sha FROM gitlab_commit_parents WHERE commit_sha=? ORDER BY position", (sha,)).fetchall()
        if not parents:
            return []
        patch = self._git_repository(project_row["id"]).diff(parents[0]["parent_sha"], commit["sha"])["patch"]
        return [{"diff": patch, "new_path": None, "old_path": None, "new_file": False, "renamed_file": False, "deleted_file": False}]

    def get_repository_tree(self, project: str, *, ref: str | None = None,
                            path: str | None = None, recursive: bool = False) -> list[dict[str, Any]]:
        project_row = self._require_project(project)
        resolved = ref or project_row["default_branch"]
        return self._git_repository(project_row["id"]).list_tree(resolved, path=path, recursive=recursive)

    def get_repository_file(self, project: str, file_path: str, *, ref: str) -> dict[str, Any]:
        project_row = self._require_project(project)
        commit_sha = self._resolve_ref(project_row["id"], ref)
        content = self._git_repository(project_row["id"]).read_file(commit_sha, file_path)
        return {"file_name": file_path.rsplit("/", 1)[-1], "file_path": file_path, "size": len(content), "encoding": "base64", "content": base64.b64encode(content).decode(), "content_sha256": hashlib.sha256(content).hexdigest(), "ref": ref, "blob_id": hashlib.sha1(content).hexdigest(), "commit_id": commit_sha, "last_commit_id": commit_sha, "execute_filemode": False}

    def compare_repository(self, project: str, *, from_ref: str, to_ref: str,
                           straight: bool = False) -> dict[str, Any]:
        project_row = self._require_project(project)
        from_sha = self._resolve_ref(project_row["id"], from_ref)
        to_sha = self._resolve_ref(project_row["id"], to_ref)
        patch = self._git_repository(project_row["id"]).diff(from_sha, to_sha)
        return {"commit": self.get_commit(project, to_sha), "commits": [self.get_commit(project, to_sha)], "diffs": [{"old_path": None, "new_path": None, "diff": patch["patch"], "new_file": False, "renamed_file": False, "deleted_file": False}], "compare_timeout": False, "compare_same_ref": from_sha == to_sha, "web_url": None}

    def create_repository_file(self, project: str, file_path: str, *, branch: str,
                               content: str, commit_message: str,
                               encoding: str = "text") -> dict[str, Any]:
        commit = self.create_repository_commit(project, branch=branch, commit_message=commit_message, actions=({"action": "create", "file_path": file_path, "content": content, "encoding": encoding},))
        return {"file_path": file_path, "branch": branch, "commit_id": commit["id"], "content": content}

    def update_repository_file(self, project: str, file_path: str, *, branch: str,
                               content: str, commit_message: str,
                               encoding: str = "text") -> dict[str, Any]:
        commit = self.create_repository_commit(project, branch=branch, commit_message=commit_message, actions=({"action": "update", "file_path": file_path, "content": content, "encoding": encoding},))
        return {"file_path": file_path, "branch": branch, "commit_id": commit["id"], "content": content}

    def delete_repository_file(self, project: str, file_path: str, *, branch: str,
                               commit_message: str) -> dict[str, Any]:
        commit = self.create_repository_commit(project, branch=branch, commit_message=commit_message, actions=({"action": "delete", "file_path": file_path},))
        return {"file_path": file_path, "branch": branch, "commit_id": commit["id"]}

    def create_branch(self, project: str, *, branch: str, ref: str, protected: bool = False) -> dict[str, Any]:
        project_row = self._require_project(project, 30)
        commit = self._require_commit(project_row["id"], self._resolve_ref(project_row["id"], ref))
        try:
            self.session.execute("INSERT INTO gitlab_branches(project_id,name,head_sha,protected) VALUES(?,?,?,?)", (project_row["id"], branch, commit["sha"], protected))
        except Exception as exc:
            raise GitLabConflict("Branch already exists") from exc
        if self.git_data_plane is not None:
            self.git_data_plane.repository(project_row["id"]).update_branch(branch, commit["sha"])
        return {"name": branch, "merged": False, "protected": protected, "commit": {"id": commit["sha"]}}

    def set_branch_head(self, project: str, *, branch: str, sha: str) -> dict[str, Any]:
        """Move a branch to an existing commit in both planes.

        Simulation-only ref writer; intentionally absent from public surfaces.
        Agents move branches by committing or merging, the way they would
        against a real GitLab. A harness that authors history needs to place a
        ref directly, and doing that behind GitLab's back would leave the SQL
        branch row and refs/heads/* disagreeing.
        """
        project_row = self._require_project(project, 40)
        commit = self._require_commit(project_row["id"], sha)
        updated = self.session.execute(
            "UPDATE gitlab_branches SET head_sha=? WHERE project_id=? AND name=?",
            (commit["sha"], project_row["id"], branch),
        )
        if getattr(updated, "rowcount", 1) == 0:
            raise GitLabNotFound("404 Branch Not Found")
        if self.git_data_plane is not None:
            self.git_data_plane.repository(project_row["id"]).update_branch(
                branch, commit["sha"]
            )
        return {"name": branch, "commit": {"id": commit["sha"]}}

    def list_branches(self, project: str) -> list[dict[str, Any]]:
        row = self._require_project(project)
        branches = self.session.execute("SELECT * FROM gitlab_branches WHERE project_id=? ORDER BY lower(name)", (row["id"],)).fetchall()
        return [{"name": item["name"], "protected": bool(item["protected"]), "commit": {"id": item["head_sha"]}} for item in branches]

    def get_branch(self, project: str, branch: str) -> dict[str, Any]:
        project_row = self._require_project(project)
        row = self._require_branch(project_row["id"], branch)
        return {"name": row["name"], "merged": False, "protected": bool(row["protected"]), "default": row["name"] == project_row["default_branch"], "developers_can_push": True, "developers_can_merge": True, "can_push": self._has_project_access(project_row["id"], 30), "commit": self._commit(self._require_commit(project_row["id"], row["head_sha"]))}

    def delete_branch(self, project: str, branch: str) -> None:
        project_row = self._require_project(project, 30)
        if branch == project_row["default_branch"]:
            raise GitLabForbidden("Cannot delete the default branch")
        self._require_branch(project_row["id"], branch)
        self.session.execute("DELETE FROM gitlab_branches WHERE project_id=? AND name=?", (project_row["id"], branch))
        if self.git_data_plane is not None:
            self.git_data_plane.repository(project_row["id"]).delete_branch(branch)

    def list_tags(self, project: str) -> list[dict[str, Any]]:
        project_row = self._require_project(project)
        rows = self.session.execute("SELECT * FROM gitlab_tags WHERE project_id=? ORDER BY lower(name)", (project_row["id"],)).fetchall()
        return [self._tag(row) for row in rows]

    def create_tag(self, project: str, *, tag_name: str, ref: str, message: str | None = None) -> dict[str, Any]:
        project_row = self._require_project(project, 30)
        branch = self.session.execute("SELECT head_sha FROM gitlab_branches WHERE project_id=? AND name=?", (project_row["id"], ref)).fetchone()
        sha = branch["head_sha"] if branch else ref
        self._require_commit(project_row["id"], sha)
        try:
            self.session.execute("INSERT INTO gitlab_tags(project_id,name,commit_sha,message,created_at) VALUES(?,?,?,?,?)", (project_row["id"], tag_name, sha, message, self._now()))
        except Exception as exc:
            raise GitLabConflict("Tag already exists") from exc
        # write refs/tags/* as well: a tag that is only a SQL row is invisible
        # to every git-level read, including the portable exporter
        if self.git_data_plane is not None:
            self.git_data_plane.repository(project_row["id"]).update_tag(tag_name, sha)
        return self._tag(self.session.execute("SELECT * FROM gitlab_tags WHERE project_id=? AND name=?", (project_row["id"], tag_name)).fetchone())

    def get_tag(self, project: str, tag_name: str) -> dict[str, Any]:
        project_row = self._require_project(project)
        row = self.session.execute("SELECT * FROM gitlab_tags WHERE project_id=? AND name=?", (project_row["id"], tag_name)).fetchone()
        if row is None:
            raise GitLabNotFound("404 Tag Not Found")
        return self._tag(row)

    def delete_tag(self, project: str, tag_name: str) -> None:
        project_row = self._require_project(project, 30)
        self.get_tag(project, tag_name)
        self.session.execute("DELETE FROM gitlab_tags WHERE project_id=? AND name=?", (project_row["id"], tag_name))

    def create_merge_request(self, project: str, *, title: str, source_branch: str, target_branch: str,
                             description: str | None = None, draft: bool = False,
                             reviewers: Sequence[str] = (), reviewer_ids: Sequence[int] = ()) -> dict[str, Any]:
        project_row = self._require_project(project, 30)
        source = self._require_branch(project_row["id"], source_branch)
        target = self._require_branch(project_row["id"], target_branch)
        if source_branch == target_branch:
            raise GitLabValidationError("source and target branch must differ")
        iid = project_row["next_merge_request_iid"]
        timestamp = self._now()
        mr_id = self.session.execute("""INSERT INTO gitlab_merge_requests(project_id,iid,title,description,state,author_id,source_branch,source_sha,target_branch,target_sha,draft,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) RETURNING id""", (project_row["id"], iid, title, description, "opened", self._require_actor()["id"], source_branch, source["head_sha"], target_branch, target["head_sha"], draft, timestamp, timestamp)).fetchone()["id"]
        self.session.execute("UPDATE gitlab_projects SET next_merge_request_iid=next_merge_request_iid+1,updated_at=? WHERE id=?", (timestamp, project_row["id"]))
        reviewer_rows = [self._require_user(username) for username in reviewers]
        for user_id in reviewer_ids:
            user = self.session.execute("SELECT * FROM gitlab_users WHERE id=?", (user_id,)).fetchone()
            if user is None:
                raise GitLabValidationError("reviewer_ids contains an unknown user")
            reviewer_rows.append(user)
        for user in reviewer_rows:
            self.session.execute("INSERT INTO gitlab_merge_request_reviewers(merge_request_id,user_id,approved) VALUES(?,?,?)", (mr_id, user["id"], False))
        return self.get_merge_request(project, iid)

    def list_merge_requests(self, project: str, *, state: str = "all") -> list[dict[str, Any]]:
        project_row = self._require_project(project)
        sql = "SELECT * FROM gitlab_merge_requests WHERE project_id=?"
        params: list[Any] = [project_row["id"]]
        if state != "all":
            sql += " AND state=?"
            params.append(state)
        return [self._merge_request(row) for row in self.session.execute(sql + " ORDER BY iid DESC", params).fetchall()]

    def get_merge_request(self, project: str, merge_request_iid: int) -> dict[str, Any]:
        project_row = self._require_project(project)
        row = self.session.execute("SELECT * FROM gitlab_merge_requests WHERE project_id=? AND iid=?", (project_row["id"], merge_request_iid)).fetchone()
        if row is None:
            raise GitLabNotFound("404 Merge Request Not Found")
        return self._merge_request(row)

    def update_merge_request(self, project: str, merge_request_iid: int, *,
                             title: str | None = None, description: str | None = None,
                             state_event: str | None = None) -> dict[str, Any]:
        project_row = self._require_project(project, 30)
        current = self.get_merge_request(project, merge_request_iid)
        state = current["state"]
        if state_event is not None:
            if state_event not in {"close", "reopen"}:
                raise GitLabValidationError("state_event must be close or reopen")
            state = "closed" if state_event == "close" else "opened"
        self.session.execute("UPDATE gitlab_merge_requests SET title=?,description=?,state=?,updated_at=? WHERE project_id=? AND iid=?", (title if title is not None else current["title"], description if description is not None else current["description"], state, self._now(), project_row["id"], merge_request_iid))
        return self.get_merge_request(project, merge_request_iid)

    def approve_merge_request(self, project: str, merge_request_iid: int) -> dict[str, Any]:
        project_row = self._require_project(project, 30)
        mr = self.get_merge_request(project, merge_request_iid)
        actor = self._require_actor()
        existing = self.session.execute("SELECT 1 FROM gitlab_merge_request_reviewers WHERE merge_request_id=? AND user_id=?", (mr["id"], actor["id"])).fetchone()
        if existing:
            self.session.execute("UPDATE gitlab_merge_request_reviewers SET approved=1 WHERE merge_request_id=? AND user_id=?", (mr["id"], actor["id"]))
        else:
            self.session.execute("INSERT INTO gitlab_merge_request_reviewers(merge_request_id,user_id,approved) VALUES(?,?,1)", (mr["id"], actor["id"]))
        return self.get_merge_request_approvals(project, merge_request_iid)

    def unapprove_merge_request(self, project: str, merge_request_iid: int) -> dict[str, Any]:
        self._require_project(project, 30)
        mr = self.get_merge_request(project, merge_request_iid)
        actor = self._require_actor()
        self.session.execute("UPDATE gitlab_merge_request_reviewers SET approved=0 WHERE merge_request_id=? AND user_id=?", (mr["id"], actor["id"]))
        return self.get_merge_request_approvals(project, merge_request_iid)

    def get_merge_request_approvals(self, project: str, merge_request_iid: int) -> dict[str, Any]:
        mr = self.get_merge_request(project, merge_request_iid)
        rows = self.session.execute("SELECT u.*,r.approved FROM gitlab_merge_request_reviewers r JOIN gitlab_users u ON u.id=r.user_id WHERE r.merge_request_id=?", (mr["id"],)).fetchall()
        approved_by = [{"user": self._user(item)} for item in rows if item["approved"]]
        return {"id": mr["id"], "iid": mr["iid"], "project_id": self._require_project(project)["id"], "title": mr["title"], "state": mr["state"], "approvals_required": len(rows), "approvals_left": max(0, len(rows) - len(approved_by)), "approved_by": approved_by, "approved": bool(approved_by) and len(approved_by) == len(rows)}

    def create_merge_request_note(self, project: str, merge_request_iid: int, *, body: str) -> dict[str, Any]:
        project_row = self._require_project(project, 30)
        mr = self.get_merge_request(project, merge_request_iid)
        return self._create_note(project_row["id"], "MergeRequest", mr["id"], body)

    def list_merge_request_notes(self, project: str, merge_request_iid: int) -> list[dict[str, Any]]:
        mr = self.get_merge_request(project, merge_request_iid)
        return self._list_notes("MergeRequest", mr["id"])

    def get_merge_request_note(self, project: str, merge_request_iid: int, note_id: int) -> dict[str, Any]:
        mr = self.get_merge_request(project, merge_request_iid)
        return self._require_note("MergeRequest", mr["id"], note_id)

    def update_merge_request_note(self, project: str, merge_request_iid: int, note_id: int, *, body: str) -> dict[str, Any]:
        self._require_project(project, 30)
        mr = self.get_merge_request(project, merge_request_iid)
        self._require_note("MergeRequest", mr["id"], note_id)
        self.session.execute("UPDATE gitlab_notes SET body=?,updated_at=? WHERE id=?", (body, self._now(), note_id))
        return self._require_note("MergeRequest", mr["id"], note_id)

    def delete_merge_request_note(self, project: str, merge_request_iid: int, note_id: int) -> dict[str, Any]:
        self._require_project(project, 30)
        mr = self.get_merge_request(project, merge_request_iid)
        deleted = self._require_note("MergeRequest", mr["id"], note_id)
        self.session.execute("DELETE FROM gitlab_discussion_notes WHERE note_id=?", (note_id,))
        self.session.execute("DELETE FROM gitlab_notes WHERE id=?", (note_id,))
        return deleted

    def get_merge_request_changes(self, project: str, merge_request_iid: int) -> dict[str, Any]:
        project_row = self._require_project(project)
        mr = self.get_merge_request(project, merge_request_iid)
        diff = self._git_repository(project_row["id"]).diff(mr["diff_refs"]["base_sha"], mr["sha"])
        return {**mr, "changes": [{"old_path": None, "new_path": None, "diff": diff["patch"], "new_file": False, "renamed_file": False, "deleted_file": False}], "overflow": bool(diff["truncated"])}

    def merge_merge_request(self, project: str, merge_request_iid: int, *, sha: str | None = None,
                            merge_commit_message: str | None = None) -> dict[str, Any]:
        project_row = self._require_project(project, 40)
        mr = self.get_merge_request(project, merge_request_iid)
        if mr["state"] != "opened":
            raise GitLabConflict("Merge request cannot be merged")
        if sha is not None and sha != mr["sha"]:
            raise GitLabConflict("SHA does not match HEAD of source branch")
        if mr["has_conflicts"]:
            raise GitLabConflict(
                "Merge request has unresolved conflicts: "
                + ", ".join(mr["conflict_paths"])
            )
        approvals = self.get_merge_request_approvals(project, merge_request_iid)
        if mr["reviewers"] and not approvals["approved"]:
            raise GitLabForbidden("Merge request is not approved")
        message = merge_commit_message or f"Merge branch '{mr['source_branch']}' into '{mr['target_branch']}'"
        repository = self._git_repository(project_row["id"])
        base_sha = mr["diff_refs"]["base_sha"]
        target_sha = mr["diff_refs"]["start_sha"]
        source_sha = mr["sha"]
        base_tree = repository.read_tree_contents(base_sha)
        source_tree = repository.read_tree_contents(source_sha)
        target_tree = repository.read_tree_contents(target_sha)
        merged_tree = self._merge_trees(base_tree, source_tree, target_tree)
        merged_files = self._tree_delta(target_tree, merged_tree)
        commit = self.create_commit(
            project, message=message, author=self.actor_username,
            parent_shas=(target_sha, source_sha), files=merged_files,
        )
        timestamp = self._now()
        actor = self._require_actor()
        self.session.execute("UPDATE gitlab_branches SET head_sha=? WHERE project_id=? AND name=?", (commit["id"], project_row["id"], mr["target_branch"]))
        self._git_repository(project_row["id"]).update_branch(mr["target_branch"], commit["id"], expected_old_sha=target_sha)
        self.session.execute("UPDATE gitlab_merge_requests SET state='merged',source_sha=?,target_sha=?,merged_by_id=?,merge_commit_sha=?,merged_at=?,updated_at=? WHERE id=?", (source_sha, target_sha, actor["id"], commit["id"], timestamp, timestamp, mr["id"]))
        return self.get_merge_request(project, merge_request_iid)

    def resolve_merge_request_conflicts(
        self,
        project: str,
        merge_request_iid: int,
        *,
        commit_message: str,
        actions: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        """Commit an explicit resolution on the source branch with two parents."""
        project_row = self._require_project(project, 30)
        mr = self.get_merge_request(project, merge_request_iid)
        if mr["state"] != "opened":
            raise GitLabConflict("Merge request is not open")
        if not mr["has_conflicts"]:
            raise GitLabConflict("Merge request has no conflicts to resolve")
        repository = self._git_repository(project_row["id"])
        source_sha = mr["sha"]
        target_sha = mr["diff_refs"]["start_sha"]
        files = self._files_from_actions(repository, source_sha, actions)
        if set(files) != set(mr["conflict_paths"]):
            raise GitLabValidationError(
                "resolution actions must touch exactly the conflict paths: "
                + ", ".join(mr["conflict_paths"])
            )
        commit = self.create_commit(
            project,
            message=commit_message,
            author=self.actor_username,
            parent_shas=(source_sha, target_sha),
            files=files,
        )
        self.session.execute(
            "UPDATE gitlab_branches SET head_sha=? WHERE project_id=? AND name=?",
            (commit["id"], project_row["id"], mr["source_branch"]),
        )
        self.session.execute(
            "UPDATE gitlab_merge_requests SET source_sha=?,target_sha=?,updated_at=? "
            "WHERE id=?",
            (commit["id"], target_sha, self._now(), mr["id"]),
        )
        repository.update_branch(
            mr["source_branch"], commit["id"], expected_old_sha=source_sha
        )
        resolved = self.get_merge_request(project, merge_request_iid)
        if resolved["has_conflicts"]:
            raise GitLabConflict("resolution commit did not clear the conflicts")
        return resolved

    def create_merge_request_discussion(self, project: str, merge_request_iid: int, *, body: str) -> dict[str, Any]:
        project_row = self._require_project(project, 30)
        mr = self.get_merge_request(project, merge_request_iid)
        return self._create_discussion(project_row["id"], "MergeRequest", mr["id"], body)

    def list_merge_request_discussions(self, project: str, merge_request_iid: int) -> list[dict[str, Any]]:
        mr = self.get_merge_request(project, merge_request_iid)
        return self._list_discussions("MergeRequest", mr["id"])

    def resolve_merge_request_discussion(self, project: str, merge_request_iid: int,
                                         discussion_id: str, *, resolved: bool) -> dict[str, Any]:
        self._require_project(project, 30)
        mr = self.get_merge_request(project, merge_request_iid)
        discussion = self._require_discussion("MergeRequest", mr["id"], discussion_id)
        self.session.execute("UPDATE gitlab_discussions SET resolved=?,updated_at=? WHERE id=?", (resolved, self._now(), discussion["id"]))
        return self._discussion(self._require_discussion("MergeRequest", mr["id"], discussion_id))

    def create_merge_request_discussion_note(self, project: str, merge_request_iid: int,
                                             discussion_id: str, *, body: str) -> dict[str, Any]:
        project_row = self._require_project(project, 30)
        mr = self.get_merge_request(project, merge_request_iid)
        self._require_discussion("MergeRequest", mr["id"], discussion_id)
        note = self._create_note(project_row["id"], "MergeRequest", mr["id"], body)
        self.session.execute("INSERT INTO gitlab_discussion_notes(discussion_id,note_id) VALUES(?,?)", (discussion_id, note["id"]))
        return note

    def list_merge_request_pipelines(self, project: str, merge_request_iid: int) -> list[dict[str, Any]]:
        mr = self.get_merge_request(project, merge_request_iid)
        return [pipeline for pipeline in self.list_pipelines(project) if pipeline["sha"] == mr["sha"]]

    def create_merge_request_pipeline(self, project: str, merge_request_iid: int) -> dict[str, Any]:
        mr = self.get_merge_request(project, merge_request_iid)
        return self.create_pipeline(project, ref=mr["source_branch"])

    def create_pipeline(self, project: str, *, ref: str) -> dict[str, Any]:
        project_row = self._require_project(project, 30)
        branch = self._require_branch(project_row["id"], ref)
        iid = project_row["next_pipeline_iid"]
        timestamp = self._now()
        pipeline_id = self.session.execute("INSERT INTO gitlab_pipelines(project_id,iid,ref,sha,source,status,user_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?) RETURNING id",
            (project_row["id"], iid, ref, branch["head_sha"], "api", "pending", self._require_actor()["id"], timestamp, timestamp)).fetchone()["id"]
        self.session.execute("UPDATE gitlab_projects SET next_pipeline_iid=next_pipeline_iid+1 WHERE id=?", (project_row["id"],))
        self.session.execute("INSERT INTO gitlab_jobs(project_id,pipeline_id,name,stage,status,allow_failure,trace,created_at) VALUES(?,?,?,?,?,?,?,?)", (project_row["id"], pipeline_id, "test", "test", "pending", False, "", timestamp))
        return self._pipeline(self.session.execute("SELECT * FROM gitlab_pipelines WHERE id=?", (pipeline_id,)).fetchone())

    def list_pipelines(self, project: str) -> list[dict[str, Any]]:
        row = self._require_project(project)
        return [self._pipeline(item) for item in self.session.execute("SELECT * FROM gitlab_pipelines WHERE project_id=? ORDER BY iid DESC", (row["id"],)).fetchall()]

    def get_pipeline(self, project: str, pipeline_id: int) -> dict[str, Any]:
        row = self._require_project(project)
        existing = self.session.execute("SELECT * FROM gitlab_pipelines WHERE project_id=? AND id=?", (row["id"], pipeline_id)).fetchone()
        if existing is None:
            raise GitLabNotFound("404 Pipeline Not Found")
        return self._pipeline(existing)

    def get_latest_pipeline(self, project: str, *, ref: str | None = None) -> dict[str, Any]:
        row = self._require_project(project)
        sql = "SELECT * FROM gitlab_pipelines WHERE project_id=?"
        parameters: list[Any] = [row["id"]]
        if ref:
            sql += " AND ref=?"
            parameters.append(ref)
        pipeline = self.session.execute(sql + " ORDER BY id DESC LIMIT 1", parameters).fetchone()
        if pipeline is None:
            raise GitLabNotFound("404 Pipeline Not Found")
        return self._pipeline(pipeline)

    def cancel_pipeline(self, project: str, pipeline_id: int) -> dict[str, Any]:
        row = self._require_project(project, 30)
        self.get_pipeline(project, pipeline_id)
        timestamp = self._now()
        self.session.execute("UPDATE gitlab_pipelines SET status='canceled',updated_at=? WHERE id=?", (timestamp, pipeline_id))
        self.session.execute("UPDATE gitlab_jobs SET status='canceled',finished_at=? WHERE pipeline_id=? AND status NOT IN ('success','failed','canceled')", (timestamp, pipeline_id))
        return self.get_pipeline(project, pipeline_id)

    def retry_pipeline(self, project: str, pipeline_id: int) -> dict[str, Any]:
        self._require_project(project, 30)
        current = self.get_pipeline(project, pipeline_id)
        return self.create_pipeline(project, ref=current["ref"])

    def update_pipeline(self, project: str, pipeline_id: int, *, status: str) -> dict[str, Any]:
        """Simulation-only CI driver hook; intentionally absent from public surfaces."""
        row = self._require_project(project, 30)
        existing = self.session.execute("SELECT * FROM gitlab_pipelines WHERE project_id=? AND id=?", (row["id"], pipeline_id)).fetchone()
        if existing is None:
            raise GitLabNotFound("404 Pipeline Not Found")
        self.session.execute("UPDATE gitlab_pipelines SET status=?,updated_at=? WHERE id=?", (status, self._now(), pipeline_id))
        return self._pipeline(self.session.execute("SELECT * FROM gitlab_pipelines WHERE id=?", (pipeline_id,)).fetchone())

    def complete_pipeline(self, project: str, pipeline_id: int, *, status: str,
                          trace: str) -> dict[str, Any]:
        """Record an authoritative CI result; intentionally absent from public surfaces.

        This adapter-specific admin hook is called by the CI harness, not by a
        GitLab REST client or an MCP agent.  The operation boundary wraps the
        pipeline and job updates in one database transaction.
        """
        actor = self._require_actor()
        if not actor["is_admin"]:
            raise GitLabForbidden("403 Forbidden")
        row = self._require_project(project)
        if status not in {"success", "failed", "canceled", "skipped"}:
            raise GitLabValidationError("status must be a terminal pipeline status")
        existing = self.session.execute(
            "SELECT id FROM gitlab_pipelines WHERE project_id=? AND id=?",
            (row["id"], pipeline_id),
        ).fetchone()
        if existing is None:
            raise GitLabNotFound("404 Pipeline Not Found")
        timestamp = self._now()
        self.session.execute(
            "UPDATE gitlab_pipelines SET status=?,updated_at=? WHERE id=?",
            (status, timestamp, pipeline_id),
        )
        self.session.execute(
            "UPDATE gitlab_jobs SET status=?,trace=?,finished_at=? WHERE project_id=? AND pipeline_id=?",
            (status, trace, timestamp, row["id"], pipeline_id),
        )
        return self._pipeline(
            self.session.execute(
                "SELECT * FROM gitlab_pipelines WHERE id=?", (pipeline_id,)
            ).fetchone()
        )

    def list_pipeline_jobs(self, project: str, pipeline_id: int) -> list[dict[str, Any]]:
        row = self._require_project(project)
        self.get_pipeline(project, pipeline_id)
        jobs = self.session.execute("SELECT * FROM gitlab_jobs WHERE project_id=? AND pipeline_id=? ORDER BY id", (row["id"], pipeline_id)).fetchall()
        return [self._job(job) for job in jobs]

    def list_jobs(self, project: str) -> list[dict[str, Any]]:
        row = self._require_project(project)
        return [self._job(job) for job in self.session.execute("SELECT * FROM gitlab_jobs WHERE project_id=? ORDER BY id DESC", (row["id"],)).fetchall()]

    def get_job(self, project: str, job_id: int) -> dict[str, Any]:
        row = self._require_project(project)
        job = self.session.execute("SELECT * FROM gitlab_jobs WHERE project_id=? AND id=?", (row["id"], job_id)).fetchone()
        if job is None:
            raise GitLabNotFound("404 Job Not Found")
        return self._job(job)

    def cancel_job(self, project: str, job_id: int) -> dict[str, Any]:
        self._require_project(project, 30)
        self.get_job(project, job_id)
        self.session.execute("UPDATE gitlab_jobs SET status='canceled',finished_at=? WHERE id=?", (self._now(), job_id))
        return self.get_job(project, job_id)

    def retry_job(self, project: str, job_id: int) -> dict[str, Any]:
        row = self._require_project(project, 30)
        old = self.get_job(project, job_id)
        new_id = self.session.execute("INSERT INTO gitlab_jobs(project_id,pipeline_id,name,stage,status,allow_failure,trace,created_at) VALUES(?,?,?,?,?,?,?,?) RETURNING id", (row["id"], old["pipeline"]["id"], old["name"], old["stage"], "pending", old["allow_failure"], "", self._now())).fetchone()["id"]
        return self.get_job(project, new_id)

    def play_job(self, project: str, job_id: int) -> dict[str, Any]:
        self._require_project(project, 30)
        self.get_job(project, job_id)
        self.session.execute("UPDATE gitlab_jobs SET status='pending' WHERE id=?", (job_id,))
        return self.get_job(project, job_id)

    def get_job_trace(self, project: str, job_id: int) -> str:
        row = self._require_project(project)
        job = self.session.execute("SELECT trace FROM gitlab_jobs WHERE project_id=? AND id=?", (row["id"], job_id)).fetchone()
        if job is None:
            raise GitLabNotFound("404 Job Not Found")
        return job["trace"]

    def list_commit_statuses(self, project: str, sha: str) -> list[dict[str, Any]]:
        project_row = self._require_project(project)
        self._require_commit(project_row["id"], sha)
        rows = self.session.execute("SELECT * FROM gitlab_commit_statuses WHERE project_id=? AND commit_sha=? ORDER BY id DESC", (project_row["id"], sha)).fetchall()
        return [self._commit_status(row) for row in rows]

    def set_commit_status(self, project: str, sha: str, *, state: str,
                          name: str = "default", target_url: str | None = None,
                          description: str | None = None) -> dict[str, Any]:
        """CI verdict writer; intentionally absent from public surfaces.

        An agent that can post state="success" can forge a green build, which
        makes every downstream "did CI pass" judgement worthless. Only the
        harness records CI outcomes, the same rule update_pipeline follows.
        """
        project_row = self._require_project(project, 30)
        self._require_commit(project_row["id"], sha)
        if state not in {"pending", "running", "success", "failed", "canceled", "skipped"}:
            raise GitLabValidationError("invalid commit status state")
        status_id = self.session.execute("INSERT INTO gitlab_commit_statuses(project_id,commit_sha,name,status,target_url,description,creator_id,created_at) VALUES(?,?,?,?,?,?,?,?) RETURNING id", (project_row["id"], sha, name, state, target_url, description, self._require_actor()["id"], self._now())).fetchone()["id"]
        return self._commit_status(self.session.execute("SELECT * FROM gitlab_commit_statuses WHERE id=?", (status_id,)).fetchone())

    def list_releases(self, project: str) -> list[dict[str, Any]]:
        project_row = self._require_project(project)
        rows = self.session.execute("SELECT * FROM gitlab_releases WHERE project_id=? ORDER BY released_at DESC", (project_row["id"],)).fetchall()
        return [self._release(row) for row in rows]

    def create_release(self, project: str, *, tag_name: str, name: str | None = None,
                       description: str | None = None,
                       released_at: str | None = None, ref: str | None = None) -> dict[str, Any]:
        project_row = self._require_project(project, 30)
        try:
            self.get_tag(project, tag_name)
        except GitLabNotFound:
            if ref is None:
                raise GitLabValidationError("ref is required when tag does not exist")
            self.create_tag(project, tag_name=tag_name, ref=ref)
        timestamp = self._now()
        try:
            self.session.execute("INSERT INTO gitlab_releases(project_id,tag_name,name,description,author_id,created_at,released_at) VALUES(?,?,?,?,?,?,?)", (project_row["id"], tag_name, name, description, self._require_actor()["id"], timestamp, released_at or timestamp))
        except Exception as exc:
            raise GitLabConflict("Release already exists") from exc
        return self.get_release(project, tag_name)

    def get_release(self, project: str, tag_name: str) -> dict[str, Any]:
        project_row = self._require_project(project)
        row = self.session.execute("SELECT * FROM gitlab_releases WHERE project_id=? AND tag_name=?", (project_row["id"], tag_name)).fetchone()
        if row is None:
            raise GitLabNotFound("404 Release Not Found")
        return self._release(row)

    def update_release(self, project: str, tag_name: str, *, name: str | None = None,
                       description: str | None = None, released_at: str | None = None) -> dict[str, Any]:
        project_row = self._require_project(project, 30)
        current = self.get_release(project, tag_name)
        self.session.execute("UPDATE gitlab_releases SET name=?,description=?,released_at=? WHERE project_id=? AND tag_name=?", (name if name is not None else current["name"], description if description is not None else current["description"], released_at or current["released_at"], project_row["id"], tag_name))
        return self.get_release(project, tag_name)

    def delete_release(self, project: str, tag_name: str) -> dict[str, Any]:
        project_row = self._require_project(project, 30)
        deleted = self.get_release(project, tag_name)
        self.session.execute("DELETE FROM gitlab_releases WHERE project_id=? AND tag_name=?", (project_row["id"], tag_name))
        return deleted

    def _require_actor(self) -> Mapping[str, Any]:
        if self._actor is None:
            self._actor = self._require_user(self.actor_username)
        return self._actor

    def _require_user(self, username: str | None) -> Mapping[str, Any]:
        row = self.session.execute("SELECT * FROM gitlab_users WHERE lower(username)=lower(?)", (username or "",)).fetchone()
        if row is None:
            raise GitLabNotFound("404 User Not Found")
        return row

    def _require_group(self, path: str) -> Mapping[str, Any]:
        row = self.session.execute("SELECT * FROM gitlab_namespaces WHERE (lower(path)=lower(?) OR CAST(id AS TEXT)=?) AND kind='group'", (path, str(path))).fetchone()
        if row is None:
            raise GitLabNotFound("404 Group Not Found")
        return row

    def _require_project(self, path: str, access: int = 10) -> Mapping[str, Any]:
        row = self.session.execute("SELECT * FROM gitlab_projects WHERE lower(path_with_namespace)=lower(?) OR CAST(id AS TEXT)=?", (path, str(path))).fetchone()
        if row is None or not self._has_project_access(row["id"], access):
            raise GitLabNotFound("404 Project Not Found")
        return row

    def _require_project_by_id(self, project_id: int) -> Mapping[str, Any]:
        row = self.session.execute("SELECT * FROM gitlab_projects WHERE id=?", (project_id,)).fetchone()
        if row is None:
            raise GitLabNotFound("404 Project Not Found")
        return row

    def _has_project_access(self, project_id: int, minimum: int = 10) -> bool:
        actor = self._require_actor()
        if actor["is_admin"]:
            return True
        member = self.session.execute("SELECT access_level FROM gitlab_project_members WHERE project_id=? AND user_id=?", (project_id, actor["id"])).fetchone()
        return member is not None and member["access_level"] >= minimum

    def _require_commit(self, project_id: int, sha: str) -> Mapping[str, Any]:
        row = self.session.execute("SELECT * FROM gitlab_commits WHERE project_id=? AND sha=?", (project_id, sha)).fetchone()
        if row is None:
            raise GitLabNotFound("404 Commit Not Found")
        return row

    def _require_branch(self, project_id: int, name: str) -> Mapping[str, Any]:
        row = self.session.execute("SELECT * FROM gitlab_branches WHERE project_id=? AND name=?", (project_id, name)).fetchone()
        if row is None:
            raise GitLabNotFound("404 Branch Not Found")
        return row

    def _resolve_ref(self, project_id: int, ref: str) -> str:
        branch = self.session.execute("SELECT head_sha FROM gitlab_branches WHERE project_id=? AND name=?", (project_id, ref)).fetchone()
        if branch:
            return branch["head_sha"]
        tag = self.session.execute("SELECT commit_sha FROM gitlab_tags WHERE project_id=? AND name=?", (project_id, ref)).fetchone()
        if tag:
            return tag["commit_sha"]
        return self._require_commit(project_id, ref)["sha"]

    def _git_repository(self, project_id: int) -> Any:
        if self.git_data_plane is None:
            raise GitLabConflict("Git data plane is not configured")
        return self.git_data_plane.repository(project_id)

    @staticmethod
    def _files_from_actions(
        repository: Any,
        parent_sha: str | None,
        actions: Sequence[Mapping[str, Any]],
    ) -> dict[str, str | bytes | None]:
        if not actions:
            raise GitLabValidationError("actions is empty")
        files: dict[str, str | bytes | None] = {}
        for action in actions:
            kind = action.get("action")
            path = action.get("file_path")
            if kind not in {"create", "update", "delete", "move"} or not isinstance(path, str):
                raise GitLabValidationError("unsupported or invalid commit action")
            if kind == "delete":
                files[path] = None
                continue
            content = action.get("content")
            if kind == "move":
                previous = action.get("previous_path")
                if not isinstance(previous, str):
                    raise GitLabValidationError("move action requires previous_path")
                if content is None:
                    if parent_sha is None:
                        raise GitLabValidationError(
                            "move action requires a starting revision"
                        )
                    content = repository.read_file(parent_sha, previous)
                files[previous] = None
            if not isinstance(content, (str, bytes)):
                raise GitLabValidationError(f"{kind} action requires content")
            if action.get("encoding", "text") == "base64" and isinstance(content, str):
                try:
                    content = base64.b64decode(content, validate=True)
                except ValueError as exc:
                    raise GitLabValidationError("invalid base64 content") from exc
            files[path] = content
        return files

    @staticmethod
    def _file_conflicts(
        base: Mapping[str, bytes],
        source: Mapping[str, bytes],
        target: Mapping[str, bytes],
    ) -> list[str]:
        return sorted(
            path for path in set(base) | set(source) | set(target)
            if source.get(path) != base.get(path)
            and target.get(path) != base.get(path)
            and source.get(path) != target.get(path)
        )

    @staticmethod
    def _merge_trees(
        base: Mapping[str, bytes],
        source: Mapping[str, bytes],
        target: Mapping[str, bytes],
    ) -> dict[str, bytes]:
        merged: dict[str, bytes] = {}
        for path in set(base) | set(source) | set(target):
            old = base.get(path)
            src = source.get(path)
            dst = target.get(path)
            value = dst if src == old else src
            if src == dst:
                value = src
            if value is not None:
                merged[path] = value
        return merged

    @staticmethod
    def _tree_delta(
        previous: Mapping[str, bytes], target: Mapping[str, bytes]
    ) -> dict[str, bytes | None]:
        changed: dict[str, bytes | None] = {
            path: content for path, content in target.items()
            if previous.get(path) != content
        }
        for path in previous:
            if path not in target:
                changed[path] = None
        return changed

    def _set_issue_labels(self, issue_id: int, project_id: int, labels: Sequence[str]) -> None:
        for name in labels:
            label = self.session.execute("SELECT id FROM gitlab_labels WHERE project_id=? AND lower(name)=lower(?)", (project_id, name)).fetchone()
            if label is None:
                raise GitLabValidationError(f"label does not exist: {name}")
            self.session.execute("INSERT INTO gitlab_issue_labels(issue_id,label_id) VALUES(?,?)", (issue_id, label["id"]))

    def _create_note(self, project_id: int, kind: str, noteable_id: int, body: str) -> dict[str, Any]:
        if not body.strip():
            raise GitLabValidationError("body is required")
        timestamp = self._now()
        note_id = self.session.execute("INSERT INTO gitlab_notes(project_id,noteable_type,noteable_id,author_id,body,system,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?) RETURNING id",
            (project_id, kind, noteable_id, self._require_actor()["id"], body, False, timestamp, timestamp)).fetchone()["id"]
        return self._note(self.session.execute("SELECT * FROM gitlab_notes WHERE id=?", (note_id,)).fetchone())

    def _require_note(self, kind: str, noteable_id: int, note_id: int) -> dict[str, Any]:
        row = self.session.execute("SELECT * FROM gitlab_notes WHERE noteable_type=? AND noteable_id=? AND id=?", (kind, noteable_id, note_id)).fetchone()
        if row is None:
            raise GitLabNotFound("404 Note Not Found")
        return self._note(row)

    def _list_notes(self, kind: str, noteable_id: int) -> list[dict[str, Any]]:
        return [self._note(row) for row in self.session.execute("SELECT * FROM gitlab_notes WHERE noteable_type=? AND noteable_id=? ORDER BY id", (kind, noteable_id)).fetchall()]

    def _create_discussion(self, project_id: int, kind: str, noteable_id: int, body: str) -> dict[str, Any]:
        discussion_id = uuid4().hex
        timestamp = self._now()
        self.session.execute("INSERT INTO gitlab_discussions(id,project_id,noteable_type,noteable_id,resolved,created_at,updated_at) VALUES(?,?,?,?,?,?,?)", (discussion_id, project_id, kind, noteable_id, False, timestamp, timestamp))
        note = self._create_note(project_id, kind, noteable_id, body)
        self.session.execute("INSERT INTO gitlab_discussion_notes(discussion_id,note_id) VALUES(?,?)", (discussion_id, note["id"]))
        return self._discussion(self.session.execute("SELECT * FROM gitlab_discussions WHERE id=?", (discussion_id,)).fetchone())

    def _list_discussions(self, kind: str, noteable_id: int) -> list[dict[str, Any]]:
        rows = self.session.execute("SELECT * FROM gitlab_discussions WHERE noteable_type=? AND noteable_id=? ORDER BY created_at,id", (kind, noteable_id)).fetchall()
        return [self._discussion(row) for row in rows]

    def _require_discussion(self, kind: str, noteable_id: int, discussion_id: str) -> Mapping[str, Any]:
        row = self.session.execute("SELECT * FROM gitlab_discussions WHERE id=? AND noteable_type=? AND noteable_id=?", (discussion_id, kind, noteable_id)).fetchone()
        if row is None:
            raise GitLabNotFound("404 Discussion Not Found")
        return row

    def _discussion(self, row: Mapping[str, Any]) -> dict[str, Any]:
        notes = self.session.execute("SELECT n.* FROM gitlab_discussion_notes dn JOIN gitlab_notes n ON n.id=dn.note_id WHERE dn.discussion_id=? ORDER BY n.id", (row["id"],)).fetchall()
        return {"id": row["id"], "individual_note": False, "notes": [self._note(note) | {"resolvable": True, "resolved": bool(row["resolved"])} for note in notes]}

    @staticmethod
    def _user(row: Mapping[str, Any]) -> dict[str, Any]:
        return {"id": row["id"], "username": row["username"], "name": row["name"], "state": row["state"], "locked": row["state"] != "active", "avatar_url": None, "web_url": f"https://gitlab.local/{row['username']}"}

    def _project(self, row: Mapping[str, Any]) -> dict[str, Any]:
        namespace = self.session.execute("SELECT * FROM gitlab_namespaces WHERE id=?", (row["namespace_id"],)).fetchone()
        full_path = row["path_with_namespace"]
        return {"id": row["id"], "name": row["name"], "name_with_namespace": f"{namespace['name']} / {row['name']}", "path": row["path"], "path_with_namespace": full_path, "description": row["description"], "visibility": row["visibility"], "archived": bool(row["archived"]), "default_branch": row["default_branch"], "namespace": {"id": namespace["id"], "name": namespace["name"], "path": namespace["path"], "kind": namespace["kind"], "full_path": namespace["path"]}, "web_url": f"https://gitlab.local/{full_path}", "ssh_url_to_repo": f"git@gitlab.local:{full_path}.git", "http_url_to_repo": f"https://gitlab.local/{full_path}.git", "issues_enabled": True, "merge_requests_enabled": True, "jobs_enabled": True, "created_at": _json_time(row["created_at"]), "updated_at": _json_time(row["updated_at"]), "last_activity_at": _json_time(row["updated_at"])}

    def _issue(self, row: Mapping[str, Any]) -> dict[str, Any]:
        labels = self.session.execute("SELECT l.name FROM gitlab_issue_labels il JOIN gitlab_labels l ON l.id=il.label_id WHERE il.issue_id=? ORDER BY lower(l.name)", (row["id"],)).fetchall()
        author = self.session.execute("SELECT username FROM gitlab_users WHERE id=?", (row["author_id"],)).fetchone()
        assignee = self.session.execute("SELECT username FROM gitlab_users WHERE id=?", (row["assignee_id"],)).fetchone() if row["assignee_id"] else None
        project = self._require_project_by_id(row["project_id"])
        return {"id": row["id"], "iid": row["iid"], "project_id": row["project_id"], "title": row["title"], "description": row["description"], "state": row["state"], "type": "ISSUE", "author": self.get_user(author["username"]), "assignee": self.get_user(assignee["username"]) if assignee else None, "assignees": [self.get_user(assignee["username"])] if assignee else [], "labels": [item["name"] for item in labels], "user_notes_count": len(self._list_notes("Issue", row["id"])), "web_url": f"https://gitlab.local/{project['path_with_namespace']}/-/issues/{row['iid']}", "references": {"short": f"#{row['iid']}", "relative": f"#{row['iid']}", "full": f"{project['path_with_namespace']}#{row['iid']}"}, "created_at": _json_time(row["created_at"]), "updated_at": _json_time(row["updated_at"]), "closed_at": _json_time(row["closed_at"])}

    def _merge_request(self, row: Mapping[str, Any]) -> dict[str, Any]:
        author = self.session.execute("SELECT username FROM gitlab_users WHERE id=?", (row["author_id"],)).fetchone()
        reviewers = self.session.execute("SELECT u.username,r.approved FROM gitlab_merge_request_reviewers r JOIN gitlab_users u ON u.id=r.user_id WHERE r.merge_request_id=? ORDER BY lower(u.username)", (row["id"],)).fetchall()
        project = self._require_project_by_id(row["project_id"])
        source_sha = row["source_sha"]
        target_sha = row["target_sha"]
        conflict_paths: list[str] = []
        base_sha = target_sha
        if row["state"] == "opened" and self.git_data_plane is not None:
            source = self._require_branch(row["project_id"], row["source_branch"])
            target = self._require_branch(row["project_id"], row["target_branch"])
            source_sha = source["head_sha"]
            target_sha = target["head_sha"]
            repository = self._git_repository(row["project_id"])
            base_sha = repository.merge_base(source_sha, target_sha)
            conflict_paths = self._file_conflicts(
                repository.read_tree_contents(base_sha),
                repository.read_tree_contents(source_sha),
                repository.read_tree_contents(target_sha),
            )
        has_conflicts = bool(conflict_paths)
        merge_status = (
            "cannot_be_merged" if has_conflicts else
            "can_be_merged" if row["state"] == "opened" else "unchecked"
        )
        detailed_status = (
            "conflict" if has_conflicts else
            "mergeable" if row["state"] == "opened" else "not_open"
        )
        return {"id": row["id"], "iid": row["iid"], "project_id": row["project_id"], "title": row["title"], "description": row["description"], "state": row["state"], "author": self.get_user(author["username"]), "source_branch": row["source_branch"], "sha": source_sha, "target_branch": row["target_branch"], "target_sha": target_sha, "draft": bool(row["draft"]), "work_in_progress": bool(row["draft"]), "merge_status": merge_status, "detailed_merge_status": detailed_status, "has_conflicts": has_conflicts, "conflict_paths": conflict_paths, "merge_commit_sha": row["merge_commit_sha"], "reviewers": [self.get_user(item["username"]) for item in reviewers], "diff_refs": {"base_sha": base_sha, "head_sha": source_sha, "start_sha": target_sha}, "user_notes_count": len(self._list_notes("MergeRequest", row["id"])), "web_url": f"https://gitlab.local/{project['path_with_namespace']}/-/merge_requests/{row['iid']}", "created_at": _json_time(row["created_at"]), "updated_at": _json_time(row["updated_at"]), "merged_at": _json_time(row["merged_at"])}

    def _note(self, row: Mapping[str, Any]) -> dict[str, Any]:
        author = self.session.execute("SELECT username,name FROM gitlab_users WHERE id=?", (row["author_id"],)).fetchone()
        return {"id": row["id"], "body": row["body"], "author": {"username": author["username"], "name": author["name"]}, "system": bool(row["system"]), "noteable_id": row["noteable_id"], "noteable_type": row["noteable_type"], "created_at": _json_time(row["created_at"]), "updated_at": _json_time(row["updated_at"])}

    @staticmethod
    def _pipeline(row: Mapping[str, Any]) -> dict[str, Any]:
        return {"id": row["id"], "iid": row["iid"], "project_id": row["project_id"], "ref": row["ref"], "sha": row["sha"], "source": row["source"], "status": row["status"], "web_url": f"https://gitlab.local/-/pipelines/{row['id']}", "created_at": _json_time(row["created_at"]), "updated_at": _json_time(row["updated_at"])}

    def _commit(self, row: Mapping[str, Any]) -> dict[str, Any]:
        parents = self.session.execute("SELECT parent_sha FROM gitlab_commit_parents WHERE commit_sha=? ORDER BY position", (row["sha"],)).fetchall()
        author = self.session.execute("SELECT name,email FROM gitlab_users WHERE id=?", (row["author_id"],)).fetchone() if row["author_id"] else None
        return {"id": row["sha"], "short_id": row["sha"][:8], "created_at": _json_time(row["committed_at"]), "title": row["message"].splitlines()[0], "message": row["message"], "author_name": author["name"] if author else None, "author_email": author["email"] if author else None, "authored_date": _json_time(row["committed_at"]), "committer_name": author["name"] if author else None, "committer_email": author["email"] if author else None, "committed_date": _json_time(row["committed_at"]), "parent_ids": [parent["parent_sha"] for parent in parents], "trailers": {}, "extended_trailers": {}, "web_url": None}

    @staticmethod
    def _label(row: Mapping[str, Any]) -> dict[str, Any]:
        return {"id": row["id"], "name": row["name"], "color": row["color"], "text_color": "#FFFFFF", "description": row["description"], "description_html": row["description"], "subscribed": False, "priority": None, "is_project_label": True}

    def _tag(self, row: Mapping[str, Any]) -> dict[str, Any]:
        return {"name": row["name"], "message": row["message"], "target": row["commit_sha"], "commit": self._commit(self._require_commit(row["project_id"], row["commit_sha"])), "release": None, "protected": False, "created_at": _json_time(row["created_at"])}

    @staticmethod
    def _job(row: Mapping[str, Any]) -> dict[str, Any]:
        return {"id": row["id"], "status": row["status"], "stage": row["stage"], "name": row["name"], "ref": None, "allow_failure": bool(row["allow_failure"]), "created_at": _json_time(row["created_at"]), "started_at": _json_time(row["started_at"]), "finished_at": _json_time(row["finished_at"]), "pipeline": {"id": row["pipeline_id"]}}

    def _commit_status(self, row: Mapping[str, Any]) -> dict[str, Any]:
        creator = self.session.execute("SELECT * FROM gitlab_users WHERE id=?", (row["creator_id"],)).fetchone()
        return {"id": row["id"], "sha": row["commit_sha"], "ref": None, "status": row["status"], "name": row["name"], "target_url": row["target_url"], "description": row["description"], "created_at": _json_time(row["created_at"]), "updated_at": _json_time(row["created_at"]), "allow_failure": False, "author": self._user(creator)}

    def _release(self, row: Mapping[str, Any]) -> dict[str, Any]:
        author = self.session.execute("SELECT * FROM gitlab_users WHERE id=?", (row["author_id"],)).fetchone()
        return {"tag_name": row["tag_name"], "name": row["name"], "description": row["description"], "created_at": _json_time(row["created_at"]), "released_at": _json_time(row["released_at"]), "author": self._user(author), "commit": self.get_tag(self._require_project_by_id(row["project_id"])["path_with_namespace"], row["tag_name"])["commit"], "assets": {"count": 0, "sources": [], "links": []}}

    def _now(self) -> str:
        return self.now().isoformat()


def _json_time(value: Any) -> Any:
    return value.isoformat() if hasattr(value, "isoformat") else value
