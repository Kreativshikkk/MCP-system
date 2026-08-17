"""Transactional operations for the bounded Bitbucket Cloud domain."""

from __future__ import annotations

import base64
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Sequence
from uuid import uuid4

from ...errors import ServiceOperationError
from ...git_storage import GitServiceDataPlane, GitServiceDataPlaneTransaction, GitStorageError
from ...plugins import RelationalSession


class BitbucketError(ServiceOperationError):
    status_code = 500
    error = "error"


class BitbucketNotFound(BitbucketError):
    status_code = 404
    error = "not_found"


class BitbucketForbidden(BitbucketError):
    status_code = 403
    error = "forbidden"


class BitbucketConflict(BitbucketError):
    status_code = 409
    error = "conflict"


class BitbucketValidationError(BitbucketError):
    status_code = 400
    error = "bad_request"


class BitbucketOperations:
    def __init__(self, session: RelationalSession, *, actor: str,
                 now: Callable[[], datetime] | None = None,
                 git_data_plane: GitServiceDataPlane | GitServiceDataPlaneTransaction | None = None) -> None:
        self.session = session
        self.actor = actor
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.git_data_plane = git_data_plane

    def get_current_user(self) -> dict[str, Any]:
        return self._user(self._require_actor())

    def get_workspace(self, workspace: str) -> dict[str, Any]:
        return self._workspace(self._require_workspace(workspace))

    def list_workspace_members(self, workspace: str) -> dict[str, Any]:
        item = self._require_workspace(workspace)
        rows = self.session.execute("SELECT u.*,m.permission FROM bitbucket_workspace_members m JOIN bitbucket_users u ON u.id=m.user_id WHERE m.workspace_id=? ORDER BY u.id", (item["id"],)).fetchall()
        return self._page([{**self._user(row), "permission": row["permission"]} for row in rows])

    def list_repositories(self, workspace: str) -> dict[str, Any]:
        item = self._require_workspace(workspace)
        rows = self.session.execute("SELECT * FROM bitbucket_repositories WHERE workspace_id=? ORDER BY slug", (item["id"],)).fetchall()
        return self._page([self._repository(row) for row in rows if self._permission(row["id"])])

    def get_repository(self, workspace: str, repo_slug: str) -> dict[str, Any]:
        return self._repository(self._require_repository(workspace, repo_slug))

    def create_commit(self, workspace: str, repo_slug: str, *, branch: str, message: str,
                      files: Mapping[str, str | None], parents: Sequence[str] = ()) -> dict[str, Any]:
        repo = self._require_repository(workspace, repo_slug, "write")
        if self.git_data_plane is None or not message.strip() or not files:
            raise BitbucketValidationError("message and files are required")
        branch_row = self.session.execute("SELECT * FROM bitbucket_branches WHERE repository_id=? AND name=?", (repo["id"], branch)).fetchone()
        parent_hashes = tuple(parents) or ((branch_row["target_hash"],) if branch_row else ())
        for parent in parent_hashes:
            self._require_commit(repo["id"], parent)
        actor = self._require_actor()
        created = self._git(repo["id"]).create_commit(message=message, author_name=actor["display_name"], author_email=actor["email"] or f"{self.actor}@localhost", timestamp=self.now(), parent_shas=parent_hashes, files=files)
        timestamp = self._time()
        self.session.execute("INSERT INTO bitbucket_commits(hash,repository_id,message,author_id,committed_at) VALUES(?,?,?,?,?)", (created["sha"], repo["id"], message, actor["id"], timestamp))
        for position, parent in enumerate(created["parents"]):
            self.session.execute("INSERT INTO bitbucket_commit_parents(commit_hash,parent_hash,position) VALUES(?,?,?)", (created["sha"], parent, position))
        if branch_row:
            self.session.execute("UPDATE bitbucket_branches SET target_hash=? WHERE repository_id=? AND name=?", (created["sha"], repo["id"], branch))
            self._git(repo["id"]).update_branch(branch, created["sha"], expected_old_sha=branch_row["target_hash"])
        else:
            self.session.execute("INSERT INTO bitbucket_branches(repository_id,name,target_hash) VALUES(?,?,?)", (repo["id"], branch, created["sha"]))
            self._git(repo["id"]).update_branch(branch, created["sha"])
        return self.get_commit(workspace, repo_slug, created["sha"])

    def list_commits(self, workspace: str, repo_slug: str, *, include: str | None = None) -> dict[str, Any]:
        repo = self._require_repository(workspace, repo_slug)
        if include:
            self._resolve(repo["id"], include)
        rows = self.session.execute("SELECT * FROM bitbucket_commits WHERE repository_id=? ORDER BY committed_at DESC,hash", (repo["id"],)).fetchall()
        return self._page([self._commit(row) for row in rows])

    def get_commit(self, workspace: str, repo_slug: str, commit: str) -> dict[str, Any]:
        repo = self._require_repository(workspace, repo_slug)
        return self._commit(self._require_commit(repo["id"], self._resolve(repo["id"], commit)))

    def get_file(self, workspace: str, repo_slug: str, commit: str, path: str) -> dict[str, Any]:
        repo = self._require_repository(workspace, repo_slug)
        sha = self._resolve(repo["id"], commit)
        git = self._git(repo["id"])
        try:
            entries = git.list_tree(sha, path=path)
        except GitStorageError as exc:
            raise BitbucketValidationError(str(exc)) from exc
        if not any(
            item["path"] == path and item["type"] == "blob" for item in entries
        ):
            raise BitbucketNotFound("file not found")
        content = git.read_file(sha, path)
        return {"path": path, "commit": {"hash": sha}, "encoding": "base64", "data": base64.b64encode(content).decode()}

    def get_diff(self, workspace: str, repo_slug: str, spec: str) -> dict[str, Any]:
        repo = self._require_repository(workspace, repo_slug)
        left, separator, right = spec.partition("..")
        if not separator:
            raise BitbucketValidationError("spec must be base..head")
        return self._git(repo["id"]).diff(self._resolve(repo["id"], left), self._resolve(repo["id"], right))

    def list_branches(self, workspace: str, repo_slug: str) -> dict[str, Any]:
        repo = self._require_repository(workspace, repo_slug)
        rows = self.session.execute("SELECT * FROM bitbucket_branches WHERE repository_id=? ORDER BY name", (repo["id"],)).fetchall()
        return self._page([self._branch(row) for row in rows])

    def create_branch(self, workspace: str, repo_slug: str, *, name: str, target: str) -> dict[str, Any]:
        repo = self._require_repository(workspace, repo_slug, "write")
        sha = self._resolve(repo["id"], target)
        try:
            self.session.execute("INSERT INTO bitbucket_branches(repository_id,name,target_hash) VALUES(?,?,?)", (repo["id"], name, sha))
        except Exception as exc:
            raise BitbucketConflict("branch already exists") from exc
        self._git(repo["id"]).update_branch(name, sha)
        return self.get_branch(workspace, repo_slug, name)

    def get_branch(self, workspace: str, repo_slug: str, name: str) -> dict[str, Any]:
        repo = self._require_repository(workspace, repo_slug)
        row = self.session.execute("SELECT * FROM bitbucket_branches WHERE repository_id=? AND name=?", (repo["id"], name)).fetchone()
        if row is None:
            raise BitbucketNotFound("branch not found")
        return self._branch(row)

    def delete_branch(self, workspace: str, repo_slug: str, name: str) -> None:
        repo = self._require_repository(workspace, repo_slug, "write")
        if name == repo["mainbranch"]:
            raise BitbucketForbidden("cannot delete main branch")
        self.get_branch(workspace, repo_slug, name)
        self.session.execute("DELETE FROM bitbucket_branches WHERE repository_id=? AND name=?", (repo["id"], name))
        self._git(repo["id"]).delete_branch(name)

    def create_tag(self, workspace: str, repo_slug: str, *, name: str,
                   target: str) -> dict[str, Any]:
        repo = self._require_repository(workspace, repo_slug, "write")
        if not name.strip():
            raise BitbucketValidationError("tag name is required")
        sha = self._resolve(repo["id"], target)
        if self.session.execute(
            "SELECT 1 FROM bitbucket_tags WHERE repository_id=? AND name=?",
            (repo["id"], name),
        ).fetchone():
            raise BitbucketConflict("tag already exists")
        try:
            self._git(repo["id"]).create_tag(name, sha)
            self.session.execute(
                "INSERT INTO bitbucket_tags(repository_id,name,target_hash) VALUES(?,?,?)",
                (repo["id"], name, sha),
            )
        except GitStorageError as exc:
            raise BitbucketValidationError(str(exc)) from exc
        return self.get_tag(workspace, repo_slug, name)

    def get_tag(self, workspace: str, repo_slug: str, name: str) -> dict[str, Any]:
        repo = self._require_repository(workspace, repo_slug)
        row = self.session.execute(
            "SELECT * FROM bitbucket_tags WHERE repository_id=? AND name=?",
            (repo["id"], name),
        ).fetchone()
        if row is None:
            raise BitbucketNotFound("tag not found")
        return self._tag(row)

    def list_tags(self, workspace: str, repo_slug: str) -> dict[str, Any]:
        repo = self._require_repository(workspace, repo_slug)
        rows = self.session.execute(
            "SELECT * FROM bitbucket_tags WHERE repository_id=? ORDER BY name",
            (repo["id"],),
        ).fetchall()
        return self._page([self._tag(row) for row in rows])

    def list_issues(self, workspace: str, repo_slug: str, *, state: str | None = None) -> dict[str, Any]:
        repo = self._require_repository(workspace, repo_slug)
        rows = self.session.execute("SELECT * FROM bitbucket_issues WHERE repository_id=? ORDER BY local_id DESC", (repo["id"],)).fetchall()
        return self._page([self._issue(row) for row in rows if state is None or row["state"] == state])

    def create_issue(self, workspace: str, repo_slug: str, *, title: str, content: str | None = None,
                     kind: str = "bug", priority: str = "major", assignee: str | None = None) -> dict[str, Any]:
        repo = self._require_repository(workspace, repo_slug, "write")
        if not title.strip():
            raise BitbucketValidationError("title is required")
        local_id = repo["next_issue_id"]
        assignee_row = self._require_user(assignee) if assignee else None
        timestamp = self._time()
        row = self.session.execute("INSERT INTO bitbucket_issues(repository_id,local_id,title,content,state,kind,priority,reporter_id,assignee_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?) RETURNING id", (repo["id"], local_id, title, content, "new", kind, priority, self._require_actor()["id"], assignee_row["id"] if assignee_row else None, timestamp, timestamp)).fetchone()
        self.session.execute("UPDATE bitbucket_repositories SET next_issue_id=next_issue_id+1,updated_at=? WHERE id=?", (timestamp, repo["id"]))
        return self._issue(self._require_issue(repo["id"], row["id"], by_local=False))

    def get_issue(self, workspace: str, repo_slug: str, issue_id: int) -> dict[str, Any]:
        repo = self._require_repository(workspace, repo_slug)
        return self._issue(self._require_issue(repo["id"], issue_id))

    def update_issue(self, workspace: str, repo_slug: str, issue_id: int, *, title: str | None = None,
                     content: str | None = None, state: str | None = None, assignee: str | None = None) -> dict[str, Any]:
        repo = self._require_repository(workspace, repo_slug, "write")
        issue = self._require_issue(repo["id"], issue_id)
        if state is not None and state not in {"new", "open", "resolved", "closed"}:
            raise BitbucketValidationError("invalid issue state")
        assignee_id = issue["assignee_id"] if assignee is None else self._require_user(assignee)["id"]
        self.session.execute("UPDATE bitbucket_issues SET title=?,content=?,state=?,assignee_id=?,updated_at=? WHERE id=?", (title or issue["title"], content if content is not None else issue["content"], state or issue["state"], assignee_id, self._time(), issue["id"]))
        return self.get_issue(workspace, repo_slug, issue_id)

    def list_issue_comments(self, workspace: str, repo_slug: str, issue_id: int) -> dict[str, Any]:
        repo = self._require_repository(workspace, repo_slug)
        issue = self._require_issue(repo["id"], issue_id)
        return self._page(self._comments(repo["id"], "issue", issue["id"]))

    def create_issue_comment(self, workspace: str, repo_slug: str, issue_id: int, *, content: str) -> dict[str, Any]:
        repo = self._require_repository(workspace, repo_slug, "write")
        issue = self._require_issue(repo["id"], issue_id)
        return self._create_comment(repo["id"], "issue", issue["id"], content)

    def list_pull_requests(self, workspace: str, repo_slug: str, *, state: str | None = None) -> dict[str, Any]:
        repo = self._require_repository(workspace, repo_slug)
        rows = self.session.execute("SELECT * FROM bitbucket_pull_requests WHERE repository_id=? ORDER BY local_id DESC", (repo["id"],)).fetchall()
        return self._page([self._pull_request(row) for row in rows if state is None or row["state"] == state])

    def create_pull_request(self, workspace: str, repo_slug: str, *, title: str, source_branch: str,
                            destination_branch: str, description: str | None = None,
                            reviewers: Sequence[str] = ()) -> dict[str, Any]:
        repo = self._require_repository(workspace, repo_slug, "write")
        source = self.get_branch(workspace, repo_slug, source_branch)
        destination = self.get_branch(workspace, repo_slug, destination_branch)
        if source_branch == destination_branch:
            raise BitbucketValidationError("source and destination must differ")
        local_id = repo["next_pull_request_id"]
        timestamp = self._time()
        row = self.session.execute("INSERT INTO bitbucket_pull_requests(repository_id,local_id,title,description,state,author_id,source_branch,source_hash,destination_branch,destination_hash,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?) RETURNING id", (repo["id"], local_id, title, description, "OPEN", self._require_actor()["id"], source_branch, source["target"]["hash"], destination_branch, destination["target"]["hash"], timestamp, timestamp)).fetchone()
        self.session.execute("UPDATE bitbucket_repositories SET next_pull_request_id=next_pull_request_id+1,updated_at=? WHERE id=?", (timestamp, repo["id"]))
        for reviewer in reviewers:
            user = self._require_user(reviewer)
            self.session.execute("INSERT INTO bitbucket_pull_request_reviewers(pull_request_id,user_id,state) VALUES(?,?,?)", (row["id"], user["id"], "UNAPPROVED"))
        return self._pull_request(self._require_pr(repo["id"], local_id))

    def get_pull_request(self, workspace: str, repo_slug: str, pull_request_id: int) -> dict[str, Any]:
        repo = self._require_repository(workspace, repo_slug)
        return self._pull_request(self._require_pr(repo["id"], pull_request_id))

    def get_pull_request_diff(self, workspace: str, repo_slug: str, pull_request_id: int) -> dict[str, Any]:
        repo = self._require_repository(workspace, repo_slug)
        pr = self._require_pr(repo["id"], pull_request_id)
        return self._git(repo["id"]).diff(pr["destination_hash"], pr["source_hash"])

    def list_pull_request_comments(self, workspace: str, repo_slug: str, pull_request_id: int) -> dict[str, Any]:
        repo = self._require_repository(workspace, repo_slug)
        pr = self._require_pr(repo["id"], pull_request_id)
        return self._page(self._comments(repo["id"], "pullrequest", pr["id"]))

    def create_pull_request_comment(self, workspace: str, repo_slug: str, pull_request_id: int, *, content: str) -> dict[str, Any]:
        repo = self._require_repository(workspace, repo_slug, "write")
        pr = self._require_pr(repo["id"], pull_request_id)
        return self._create_comment(repo["id"], "pullrequest", pr["id"], content)

    def approve_pull_request(self, workspace: str, repo_slug: str, pull_request_id: int) -> dict[str, Any]:
        return self._review(workspace, repo_slug, pull_request_id, "APPROVED")

    def request_changes(self, workspace: str, repo_slug: str, pull_request_id: int) -> dict[str, Any]:
        return self._review(workspace, repo_slug, pull_request_id, "CHANGES_REQUESTED")

    def merge_pull_request(self, workspace: str, repo_slug: str, pull_request_id: int, *, message: str | None = None) -> dict[str, Any]:
        repo = self._require_repository(workspace, repo_slug, "admin")
        pr = self._require_pr(repo["id"], pull_request_id)
        if pr["state"] != "OPEN":
            raise BitbucketConflict("pull request is not open")
        reviewers = self.session.execute("SELECT * FROM bitbucket_pull_request_reviewers WHERE pull_request_id=?", (pr["id"],)).fetchall()
        if reviewers and any(item["state"] != "APPROVED" for item in reviewers):
            raise BitbucketForbidden("pull request is not approved")
        source_paths = {item["path"] for item in self._git(repo["id"]).list_tree(pr["source_hash"], recursive=True) if item["type"] == "blob"}
        files = {path: self._git(repo["id"]).read_file(pr["source_hash"], path) for path in source_paths}
        merged = self.create_commit(workspace, repo_slug, branch=pr["destination_branch"], message=message or f"Merged pull request #{pull_request_id}", files=files, parents=(pr["destination_hash"], pr["source_hash"]))
        self.session.execute("UPDATE bitbucket_pull_requests SET state='MERGED',merge_commit_hash=?,updated_at=? WHERE id=?", (merged["hash"], self._time(), pr["id"]))
        return self.get_pull_request(workspace, repo_slug, pull_request_id)

    def list_pipelines(self, workspace: str, repo_slug: str) -> dict[str, Any]:
        repo = self._require_repository(workspace, repo_slug)
        rows = self.session.execute("SELECT * FROM bitbucket_pipelines WHERE repository_id=? ORDER BY build_number DESC", (repo["id"],)).fetchall()
        return self._page([self._pipeline(row) for row in rows])

    def create_pipeline(self, workspace: str, repo_slug: str, *, ref_name: str) -> dict[str, Any]:
        repo = self._require_repository(workspace, repo_slug, "write")
        sha = self._resolve(repo["id"], ref_name)
        uuid = f"{{{uuid4()}}}"; step_uuid = f"{{{uuid4()}}}"; timestamp = self._time()
        row = self.session.execute("INSERT INTO bitbucket_pipelines(uuid,repository_id,build_number,ref_name,commit_hash,state,creator_id,created_at) VALUES(?,?,?,?,?,?,?,?) RETURNING id", (uuid, repo["id"], repo["next_pipeline_number"], ref_name, sha, "PENDING", self._require_actor()["id"], timestamp)).fetchone()
        self.session.execute("INSERT INTO bitbucket_pipeline_steps(uuid,pipeline_id,name,state,log,created_at) VALUES(?,?,?,?,?,?)", (step_uuid, row["id"], "test", "PENDING", "", timestamp))
        self.session.execute("UPDATE bitbucket_repositories SET next_pipeline_number=next_pipeline_number+1 WHERE id=?", (repo["id"],))
        return self._pipeline(self._require_pipeline(repo["id"], uuid))

    def get_pipeline(self, workspace: str, repo_slug: str, pipeline_uuid: str) -> dict[str, Any]:
        repo = self._require_repository(workspace, repo_slug)
        return self._pipeline(self._require_pipeline(repo["id"], pipeline_uuid))

    def list_pipeline_steps(self, workspace: str, repo_slug: str, pipeline_uuid: str) -> dict[str, Any]:
        repo = self._require_repository(workspace, repo_slug); pipeline = self._require_pipeline(repo["id"], pipeline_uuid)
        rows = self.session.execute("SELECT * FROM bitbucket_pipeline_steps WHERE pipeline_id=? ORDER BY id", (pipeline["id"],)).fetchall()
        return self._page([self._step(row) for row in rows])

    def get_pipeline_step_log(self, workspace: str, repo_slug: str, pipeline_uuid: str, step_uuid: str) -> str:
        repo = self._require_repository(workspace, repo_slug); pipeline = self._require_pipeline(repo["id"], pipeline_uuid)
        row = self.session.execute("SELECT * FROM bitbucket_pipeline_steps WHERE pipeline_id=? AND uuid=?", (pipeline["id"], step_uuid)).fetchone()
        if row is None: raise BitbucketNotFound("pipeline step not found")
        return row["log"]

    def list_commit_statuses(self, workspace: str, repo_slug: str, commit: str) -> dict[str, Any]:
        repo = self._require_repository(workspace, repo_slug); sha = self._resolve(repo["id"], commit)
        rows = self.session.execute("SELECT * FROM bitbucket_commit_statuses WHERE repository_id=? AND commit_hash=? ORDER BY id", (repo["id"], sha)).fetchall()
        return self._page([self._status(row) for row in rows])

    def create_commit_status(self, workspace: str, repo_slug: str, commit: str, *, key: str,
                             state: str, name: str | None = None, url: str | None = None,
                             description: str | None = None) -> dict[str, Any]:
        """CI verdict writer; intentionally absent from public surfaces — an
        agent that can post SUCCESSFUL can forge a green build."""
        repo = self._require_repository(workspace, repo_slug, "write"); sha = self._resolve(repo["id"], commit)
        if state not in {"INPROGRESS", "SUCCESSFUL", "FAILED", "STOPPED"}: raise BitbucketValidationError("invalid build state")
        self.session.execute("INSERT INTO bitbucket_commit_statuses(repository_id,commit_hash,key,name,state,url,description,created_at) VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(repository_id,commit_hash,key) DO UPDATE SET name=excluded.name,state=excluded.state,url=excluded.url,description=excluded.description", (repo["id"], sha, key, name or key, state, url, description, self._time()))
        return self._status(self.session.execute("SELECT * FROM bitbucket_commit_statuses WHERE repository_id=? AND commit_hash=? AND key=?", (repo["id"], sha, key)).fetchone())

    def update_pipeline(self, workspace: str, repo_slug: str, pipeline_uuid: str, *, state: str, log: str = "") -> dict[str, Any]:
        """Harness-only CI completion hook; intentionally absent from MCP."""
        repo = self._require_repository(workspace, repo_slug, "write"); pipeline = self._require_pipeline(repo["id"], pipeline_uuid)
        finished = self._time() if state in {"COMPLETED", "FAILED", "STOPPED"} else None
        self.session.execute("UPDATE bitbucket_pipelines SET state=?,completed_at=? WHERE id=?", (state, finished, pipeline["id"]))
        self.session.execute("UPDATE bitbucket_pipeline_steps SET state=?,log=?,completed_at=? WHERE pipeline_id=?", ("SUCCESSFUL" if state == "COMPLETED" else state, log, finished, pipeline["id"]))
        return self.get_pipeline(workspace, repo_slug, pipeline_uuid)

    def _review(self, workspace: str, repo_slug: str, pr_id: int, state: str) -> dict[str, Any]:
        repo = self._require_repository(workspace, repo_slug, "write"); pr = self._require_pr(repo["id"], pr_id); actor = self._require_actor()
        existing = self.session.execute("SELECT 1 FROM bitbucket_pull_request_reviewers WHERE pull_request_id=? AND user_id=?", (pr["id"], actor["id"])).fetchone()
        if existing: self.session.execute("UPDATE bitbucket_pull_request_reviewers SET state=? WHERE pull_request_id=? AND user_id=?", (state, pr["id"], actor["id"]))
        else: self.session.execute("INSERT INTO bitbucket_pull_request_reviewers(pull_request_id,user_id,state) VALUES(?,?,?)", (pr["id"], actor["id"], state))
        return self.get_pull_request(workspace, repo_slug, pr_id)

    def _create_comment(self, repo_id: int, kind: str, subject_id: int, content: str) -> dict[str, Any]:
        if not content.strip(): raise BitbucketValidationError("comment content is required")
        timestamp = self._time(); row = self.session.execute("INSERT INTO bitbucket_comments(repository_id,subject_type,subject_id,author_id,content,created_at,updated_at) VALUES(?,?,?,?,?,?,?) RETURNING id", (repo_id, kind, subject_id, self._require_actor()["id"], content, timestamp, timestamp)).fetchone()
        return self._comment(self.session.execute("SELECT c.*,u.username,u.display_name,u.uuid FROM bitbucket_comments c JOIN bitbucket_users u ON u.id=c.author_id WHERE c.id=?", (row["id"],)).fetchone())

    def _comments(self, repo_id: int, kind: str, subject_id: int) -> list[dict[str, Any]]:
        rows = self.session.execute("SELECT c.*,u.username,u.display_name,u.uuid FROM bitbucket_comments c JOIN bitbucket_users u ON u.id=c.author_id WHERE c.repository_id=? AND c.subject_type=? AND c.subject_id=? ORDER BY c.id", (repo_id, kind, subject_id)).fetchall()
        return [self._comment(row) for row in rows]

    def _require_actor(self) -> Mapping[str, Any]: return self._require_user(self.actor)
    def _require_user(self, username: str) -> Mapping[str, Any]:
        row = self.session.execute("SELECT * FROM bitbucket_users WHERE lower(username)=lower(?)", (username,)).fetchone()
        if row is None: raise BitbucketForbidden(f"unknown actor: {username}")
        return row
    def _require_workspace(self, slug: str) -> Mapping[str, Any]:
        row = self.session.execute("SELECT * FROM bitbucket_workspaces WHERE lower(slug)=lower(?)", (slug,)).fetchone()
        if row is None: raise BitbucketNotFound("workspace not found")
        return row
    def _require_repository(self, workspace: str, slug: str, permission: str | None = None) -> Mapping[str, Any]:
        row = self.session.execute("SELECT r.* FROM bitbucket_repositories r JOIN bitbucket_workspaces w ON w.id=r.workspace_id WHERE lower(w.slug)=lower(?) AND lower(r.slug)=lower(?)", (workspace, slug)).fetchone()
        if row is None: raise BitbucketNotFound("repository not found")
        if permission and not self._permission(row["id"], permission): raise BitbucketForbidden("insufficient repository permission")
        return row
    def _permission(self, repo_id: int, required: str | None = None) -> str | bool:
        row = self.session.execute("SELECT m.permission FROM bitbucket_repository_members m JOIN bitbucket_users u ON u.id=m.user_id WHERE m.repository_id=? AND lower(u.username)=lower(?)", (repo_id, self.actor)).fetchone()
        if row is None: return False
        return row["permission"] if required is None else {"read": 1, "write": 2, "admin": 3}[row["permission"]] >= {"read": 1, "write": 2, "admin": 3}[required]
    def _require_commit(self, repo_id: int, sha: str) -> Mapping[str, Any]:
        row = self.session.execute("SELECT * FROM bitbucket_commits WHERE repository_id=? AND hash=?", (repo_id, sha)).fetchone()
        if row is None: raise BitbucketNotFound("commit not found")
        return row
    def _require_issue(self, repo_id: int, value: int, *, by_local: bool = True) -> Mapping[str, Any]:
        field = "local_id" if by_local else "id"; row = self.session.execute(f"SELECT * FROM bitbucket_issues WHERE repository_id=? AND {field}=?", (repo_id, value)).fetchone()
        if row is None: raise BitbucketNotFound("issue not found")
        return row
    def _require_pr(self, repo_id: int, local_id: int) -> Mapping[str, Any]:
        row = self.session.execute("SELECT * FROM bitbucket_pull_requests WHERE repository_id=? AND local_id=?", (repo_id, local_id)).fetchone()
        if row is None: raise BitbucketNotFound("pull request not found")
        return row
    def _require_pipeline(self, repo_id: int, uuid: str) -> Mapping[str, Any]:
        row = self.session.execute("SELECT * FROM bitbucket_pipelines WHERE repository_id=? AND uuid=?", (repo_id, uuid)).fetchone()
        if row is None: raise BitbucketNotFound("pipeline not found")
        return row
    def _resolve(self, repo_id: int, ref: str) -> str:
        branch = self.session.execute("SELECT target_hash FROM bitbucket_branches WHERE repository_id=? AND name=?", (repo_id, ref)).fetchone()
        tag = self.session.execute("SELECT target_hash FROM bitbucket_tags WHERE repository_id=? AND name=?", (repo_id, ref)).fetchone()
        sha = branch["target_hash"] if branch else tag["target_hash"] if tag else ref; self._require_commit(repo_id, sha); return sha
    def _git(self, repo_id: int):
        if self.git_data_plane is None: raise BitbucketConflict("Git data plane is not configured")
        return self.git_data_plane.repository(repo_id)
    def _time(self) -> str: return self.now().isoformat()
    @staticmethod
    def _page(values: list[Any]) -> dict[str, Any]: return {"pagelen": len(values), "size": len(values), "page": 1, "values": values}
    @staticmethod
    def _user(row: Mapping[str, Any]) -> dict[str, Any]: return {"type": "user", "uuid": row["uuid"], "username": row["username"], "display_name": row["display_name"]}
    @staticmethod
    def _workspace(row: Mapping[str, Any]) -> dict[str, Any]: return {"type": "workspace", "uuid": row["uuid"], "slug": row["slug"], "name": row["name"], "is_private": bool(row["is_private"])}
    def _repository(self, row: Mapping[str, Any]) -> dict[str, Any]: return {"type": "repository", "uuid": row["uuid"], "slug": row["slug"], "name": row["name"], "full_name": f"{self._workspace_by_id(row['workspace_id'])['slug']}/{row['slug']}", "description": row["description"], "is_private": bool(row["is_private"]), "mainbranch": {"name": row["mainbranch"]}}
    def _workspace_by_id(self, value: int) -> Mapping[str, Any]: return self.session.execute("SELECT * FROM bitbucket_workspaces WHERE id=?", (value,)).fetchone()
    def _commit(self, row: Mapping[str, Any]) -> dict[str, Any]:
        author = self.session.execute("SELECT * FROM bitbucket_users WHERE id=?", (row["author_id"],)).fetchone(); parents = self.session.execute("SELECT parent_hash FROM bitbucket_commit_parents WHERE commit_hash=? ORDER BY position", (row["hash"],)).fetchall()
        return {"type": "commit", "hash": row["hash"], "message": row["message"], "author": {"user": self._user(author)}, "parents": [{"hash": item["parent_hash"]} for item in parents], "date": str(row["committed_at"])}
    def _branch(self, row: Mapping[str, Any]) -> dict[str, Any]: return {"type": "branch", "name": row["name"], "target": {"hash": row["target_hash"]}}
    def _tag(self, row: Mapping[str, Any]) -> dict[str, Any]: return {"type": "tag", "name": row["name"], "target": {"type": "commit", "hash": row["target_hash"]}}
    def _issue(self, row: Mapping[str, Any]) -> dict[str, Any]: return {"type": "issue", "id": row["local_id"], "title": row["title"], "content": {"raw": row["content"] or ""}, "state": row["state"], "kind": row["kind"], "priority": row["priority"], "reporter": self._user(self.session.execute("SELECT * FROM bitbucket_users WHERE id=?", (row["reporter_id"],)).fetchone()), "assignee": self._user(self.session.execute("SELECT * FROM bitbucket_users WHERE id=?", (row["assignee_id"],)).fetchone()) if row["assignee_id"] else None, "created_on": str(row["created_at"]), "updated_on": str(row["updated_at"])}
    def _pull_request(self, row: Mapping[str, Any]) -> dict[str, Any]:
        author = self.session.execute("SELECT * FROM bitbucket_users WHERE id=?", (row["author_id"],)).fetchone(); reviewers = self.session.execute("SELECT u.*,r.state review_state FROM bitbucket_pull_request_reviewers r JOIN bitbucket_users u ON u.id=r.user_id WHERE r.pull_request_id=? ORDER BY u.id", (row["id"],)).fetchall()
        return {"type": "pullrequest", "id": row["local_id"], "title": row["title"], "description": row["description"] or "", "state": row["state"], "author": self._user(author), "source": {"branch": {"name": row["source_branch"]}, "commit": {"hash": row["source_hash"]}}, "destination": {"branch": {"name": row["destination_branch"]}, "commit": {"hash": row["destination_hash"]}}, "reviewers": [{**self._user(item), "state": item["review_state"]} for item in reviewers], "merge_commit": {"hash": row["merge_commit_hash"]} if row["merge_commit_hash"] else None, "created_on": str(row["created_at"]), "updated_on": str(row["updated_at"])}
    @staticmethod
    def _comment(row: Mapping[str, Any]) -> dict[str, Any]: return {"type": "pullrequest_comment" if row["subject_type"] == "pullrequest" else "issue_comment", "id": row["id"], "user": {"uuid": row["uuid"], "username": row["username"], "display_name": row["display_name"]}, "content": {"raw": row["content"]}, "created_on": str(row["created_at"]), "updated_on": str(row["updated_at"])}
    def _pipeline(self, row: Mapping[str, Any]) -> dict[str, Any]: return {"type": "pipeline", "uuid": row["uuid"], "build_number": row["build_number"], "state": {"name": row["state"]}, "target": {"ref_name": row["ref_name"], "commit": {"hash": row["commit_hash"]}}, "creator": self._user(self.session.execute("SELECT * FROM bitbucket_users WHERE id=?", (row["creator_id"],)).fetchone()), "created_on": str(row["created_at"]), "completed_on": str(row["completed_at"]) if row["completed_at"] else None}
    @staticmethod
    def _step(row: Mapping[str, Any]) -> dict[str, Any]: return {"type": "pipeline_step", "uuid": row["uuid"], "name": row["name"], "state": {"name": row["state"]}}
    @staticmethod
    def _status(row: Mapping[str, Any]) -> dict[str, Any]: return {"type": "build", "key": row["key"], "name": row["name"], "state": row["state"], "url": row["url"], "description": row["description"]}
