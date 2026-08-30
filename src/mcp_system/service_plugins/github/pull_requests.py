"""Git commit, branch, pull-request, review, and merge operations."""

from __future__ import annotations

import base64
import mimetypes
import re
from typing import Any, Mapping, Sequence

from ...git_storage import GitStorageError
from .operations import (
    GitHubConflict,
    GitHubForbidden,
    GitHubNotFound,
    GitHubOperations,
    GitHubValidationError,
    _UNSET,
)


_GIT_SHA = re.compile(r"^[0-9a-fA-F]{40}([0-9a-fA-F]{24})?$")
_FULL_SHA = re.compile(r"[0-9a-fA-F]{40}")
_MAX_INLINE_CONTENT = 1024 * 1024


class GitHubPullRequestOperations(GitHubOperations):
    """Extends core issue operations with the pull-request lifecycle."""

    # Relational Git projections. When a Git data plane is bound, objects and
    # refs are validated against the real bare repository.

    def create_commit(
        self,
        owner: str,
        repository: str,
        *,
        message: str,
        author: str,
        parent_shas: Sequence[str] = (),
        files: Mapping[str, str | bytes | None] | None = None,
    ) -> dict[str, Any]:
        """Create a real Git commit and record its queryable relational projection."""
        repository_row = self._require_repository(
            owner, repository, minimum_permission="push"
        )
        if self.git_data_plane is None:
            raise GitHubConflict("Git data plane is not configured")
        author_row = self.session.execute(
            "SELECT * FROM github_users WHERE lower(login) = lower(?)", (author,)
        ).fetchone()
        if author_row is None:
            raise GitHubValidationError("Validation Failed: commit author does not exist")
        git_repository = self.git_data_plane.repository(repository_row["id"])
        created = git_repository.create_commit(
            message=message,
            author_name=author_row["name"] or author_row["login"],
            author_email=author_row["email"] or f"{author_row['login']}@localhost",
            timestamp=self.now(),
            parent_shas=tuple(self._validate_sha(parent, "parent_sha") for parent in parent_shas),
            files=files,
        )
        return self.record_commit(
            owner,
            repository,
            sha=created["sha"],
            tree_sha=created["tree_sha"],
            message=message,
            author=author,
            parent_shas=created["parents"],
        )

    def record_commit(
        self,
        owner: str,
        repository: str,
        *,
        sha: str,
        message: str,
        author: str,
        parent_shas: Sequence[str] = (),
        tree_sha: str | None = None,
    ) -> dict[str, Any]:
        repository_row = self._require_repository(
            owner, repository, minimum_permission="push"
        )
        normalized_sha = self._validate_sha(sha, "sha")
        normalized_tree = self._validate_sha(tree_sha, "tree_sha") if tree_sha else None
        if not message:
            raise GitHubValidationError("Validation Failed: commit message is required")
        author_row = self.session.execute(
            "SELECT * FROM github_users WHERE lower(login) = lower(?)", (author,)
        ).fetchone()
        if author_row is None:
            raise GitHubValidationError("Validation Failed: commit author does not exist")
        if self.session.execute(
            "SELECT 1 FROM github_commits WHERE sha = ?", (normalized_sha,)
        ).fetchone():
            raise GitHubConflict("Commit already exists")

        normalized_parents: list[str] = []
        for parent_sha in parent_shas:
            parent = self._require_commit(repository_row["id"], parent_sha)
            normalized_parents.append(parent["sha"])
        if self.git_data_plane is not None:
            metadata = self.git_data_plane.repository(
                repository_row["id"]
            ).commit_metadata(normalized_sha)
            if tuple(normalized_parents) != metadata["parents"]:
                raise GitHubValidationError(
                    "Validation Failed: relational parents differ from Git commit"
                )
            if normalized_tree is not None and normalized_tree != metadata["tree_sha"]:
                raise GitHubValidationError(
                    "Validation Failed: tree_sha differs from Git commit"
                )
            normalized_tree = metadata["tree_sha"]
        timestamp = self._now_value()
        self.session.execute(
            """
            INSERT INTO github_commits(
                sha, repository_id, tree_sha, message, author_id, committer_id,
                authored_at, committed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                normalized_sha,
                repository_row["id"],
                normalized_tree,
                message,
                author_row["id"],
                self._require_actor()["id"],
                timestamp,
                timestamp,
            ),
        )
        if normalized_parents:
            self.session.executemany(
                """
                INSERT INTO github_commit_parents(commit_sha, parent_sha, position)
                VALUES (?, ?, ?)
                """,
                [
                    (normalized_sha, parent_sha, position)
                    for position, parent_sha in enumerate(normalized_parents)
                ],
            )
        return self._commit_dict(
            self._require_commit(repository_row["id"], normalized_sha), repository_row
        )

    def list_commits(self, owner: str, repository: str) -> list[dict[str, Any]]:
        repository_row = self._require_repository(owner, repository)
        rows = self.session.execute(
            """
            SELECT * FROM github_commits WHERE repository_id = ?
             ORDER BY committed_at DESC, sha
            """,
            (repository_row["id"],),
        ).fetchall()
        return [self._commit_dict(row, repository_row) for row in rows]

    def create_branch(
        self,
        owner: str,
        repository: str,
        *,
        name: str,
        head_sha: str,
        protected: bool = False,
    ) -> dict[str, Any]:
        repository_row = self._require_repository(
            owner, repository, minimum_permission="push"
        )
        if not name.strip():
            raise GitHubValidationError("Validation Failed: branch name is required")
        commit = self._require_commit(repository_row["id"], head_sha)
        if self.session.execute(
            """
            SELECT 1 FROM github_branches
             WHERE repository_id = ? AND lower(name) = lower(?)
            """,
            (repository_row["id"], name),
        ).fetchone():
            raise GitHubValidationError("Validation Failed: branch already exists")
        self.session.execute(
            """
            INSERT INTO github_branches(repository_id, name, head_sha, protected)
            VALUES (?, ?, ?, ?)
            """,
            (repository_row["id"], name, commit["sha"], protected),
        )
        if self.git_data_plane is not None:
            self.git_data_plane.repository(repository_row["id"]).update_branch(
                name, commit["sha"]
            )
        return self._branch_dict(
            self._require_branch(repository_row["id"], name), repository_row
        )

    def list_branches(self, owner: str, repository: str) -> list[dict[str, Any]]:
        repository_row = self._require_repository(owner, repository)
        rows = self.session.execute(
            """
            SELECT * FROM github_branches WHERE repository_id = ? ORDER BY lower(name)
            """,
            (repository_row["id"],),
        ).fetchall()
        return [self._branch_dict(row, repository_row) for row in rows]

    def create_ref(self, owner: str, repository: str, *, ref: str,
                   sha: str) -> dict[str, Any]:
        repository_row = self._require_repository(owner, repository, minimum_permission="push")
        commit = self._require_commit(repository_row["id"], sha)
        kind, name = self._split_ref(ref)
        if kind == "heads":
            self.create_branch(owner, repository, name=name, head_sha=commit["sha"])
        else:
            if self.session.execute(
                "SELECT 1 FROM github_tags WHERE repository_id=? AND name=?",
                (repository_row["id"], name),
            ).fetchone():
                raise GitHubValidationError("Validation Failed: reference already exists")
            if self.git_data_plane is None:
                raise GitHubConflict("Git data plane is not configured")
            try:
                self.git_data_plane.repository(repository_row["id"]).create_tag(name, commit["sha"])
            except Exception as exc:
                raise GitHubValidationError("Validation Failed: reference already exists") from exc
            self.session.execute(
                "INSERT INTO github_tags(repository_id,name,target_sha) VALUES(?,?,?)",
                (repository_row["id"], name, commit["sha"]),
            )
        return self._ref_dict(repository_row, kind, name, commit["sha"])

    def get_ref(self, owner: str, repository: str, ref: str) -> dict[str, Any]:
        repository_row = self._require_repository(owner, repository)
        kind, name = self._split_ref(ref)
        row = self._ref_row(repository_row["id"], kind, name)
        if row is None:
            raise GitHubNotFound("Not Found")
        return self._ref_dict(repository_row, kind, name, row["sha"])

    def list_matching_refs(self, owner: str, repository: str, ref: str) -> list[dict[str, Any]]:
        repository_row = self._require_repository(owner, repository)
        normalized = ref.removeprefix("refs/").strip("/")
        results: list[dict[str, Any]] = []
        for kind, table, sha_column in (("heads", "github_branches", "head_sha"), ("tags", "github_tags", "target_sha")):
            prefix = f"{kind}/"
            if normalized and not (normalized.startswith(prefix) or prefix.startswith(normalized)):
                continue
            name_prefix = normalized.removeprefix(prefix) if normalized.startswith(prefix) else ""
            rows = self.session.execute(
                f"SELECT name,{sha_column} AS sha FROM {table} WHERE repository_id=? AND name LIKE ? ORDER BY name",
                (repository_row["id"], f"{name_prefix}%"),
            ).fetchall()
            results.extend(self._ref_dict(repository_row, kind, row["name"], row["sha"]) for row in rows)
        return results

    def update_ref(self, owner: str, repository: str, ref: str, *, sha: str,
                   force: bool = False) -> dict[str, Any]:
        repository_row = self._require_repository(owner, repository, minimum_permission="push")
        kind, name = self._split_ref(ref)
        if kind != "heads":
            raise GitHubValidationError("Validation Failed: update_ref supports branch refs")
        current = self._ref_row(repository_row["id"], kind, name)
        if current is None:
            raise GitHubNotFound("Reference does not exist")
        commit = self._require_commit(repository_row["id"], sha)
        if self.git_data_plane is None:
            raise GitHubConflict("Git data plane is not configured")
        git_repository = self.git_data_plane.repository(repository_row["id"])
        if not force and not git_repository.is_ancestor(current["sha"], commit["sha"]):
            raise GitHubValidationError("Update is not a fast forward")
        git_repository.update_branch(name, commit["sha"], expected_old_sha=current["sha"])
        self.session.execute(
            "UPDATE github_branches SET head_sha=? WHERE repository_id=? AND name=? AND head_sha=?",
            (commit["sha"], repository_row["id"], name, current["sha"]),
        )
        return self._ref_dict(repository_row, kind, name, commit["sha"])

    # Actions. Public methods mirror the selected REST read contract; mutation
    # hooks are simulation-only and intentionally absent from the MCP surface.

    def list_workflow_runs(self, owner: str, repository: str) -> dict[str, Any]:
        repository_row = self._require_repository(owner, repository)
        rows = self.session.execute(
            "SELECT * FROM github_workflow_runs WHERE repository_id=? ORDER BY id DESC",
            (repository_row["id"],),
        ).fetchall()
        runs = [self._workflow_run_dict(row, repository_row) for row in rows]
        return {"total_count": len(runs), "workflow_runs": runs}

    def list_workflow_jobs(self, owner: str, repository: str, run_id: int) -> dict[str, Any]:
        repository_row = self._require_repository(owner, repository)
        self._require_workflow_run(repository_row["id"], run_id)
        rows = self.session.execute(
            "SELECT * FROM github_workflow_jobs WHERE run_id=? ORDER BY id", (run_id,)
        ).fetchall()
        jobs = [self._workflow_job_dict(row, owner, repository, run_id) for row in rows]
        return {"total_count": len(jobs), "jobs": jobs}

    def get_workflow_job(self, owner: str, repository: str, job_id: int) -> dict[str, Any]:
        repository_row = self._require_repository(owner, repository)
        row = self.session.execute(
            """SELECT job.* FROM github_workflow_jobs job
                 JOIN github_workflow_runs run ON run.id=job.run_id
                WHERE job.id=? AND run.repository_id=?""",
            (job_id, repository_row["id"]),
        ).fetchone()
        if row is None:
            raise GitHubNotFound("Not Found")
        return self._workflow_job_dict(row, owner, repository, row["run_id"])

    def get_workflow_job_log(self, owner: str, repository: str, job_id: int) -> dict[str, Any]:
        job = self.get_workflow_job(owner, repository, job_id)
        row = self.session.execute("SELECT log FROM github_workflow_jobs WHERE id=?", (job_id,)).fetchone()
        return {"job_id": job_id, "log": row["log"]}

    def create_workflow_run(self, owner: str, repository: str, *, name: str, event: str,
                            head_branch: str, status: str = "queued") -> dict[str, Any]:
        repository_row = self._require_repository(owner, repository, minimum_permission="push")
        if status not in {"queued", "in_progress", "completed", "waiting", "pending"}:
            raise GitHubValidationError("Validation Failed: invalid workflow status")
        branch = self._require_branch(repository_row["id"], head_branch)
        next_number = self.session.execute(
            "SELECT COALESCE(MAX(run_number),0)+1 AS value FROM github_workflow_runs WHERE repository_id=?",
            (repository_row["id"],),
        ).fetchone()["value"]
        timestamp = self._now_value()
        run_id = self.session.execute(
            """INSERT INTO github_workflow_runs(repository_id,name,event,status,conclusion,head_branch,head_sha,run_number,run_attempt,actor_id,created_at,updated_at)
               VALUES(?,?,?,?,NULL,?,?,?,?,?,?,?) RETURNING id""",
            (repository_row["id"], name, event, status, head_branch, branch["head_sha"], next_number, 1, self._require_actor()["id"], timestamp, timestamp),
        ).fetchone()["id"]
        return self._workflow_run_dict(self._require_workflow_run(repository_row["id"], run_id), repository_row)

    def dispatch_workflow(self, owner: str, repository: str, workflow_id: str,
                          *, ref: str, inputs: Mapping[str, str] | None = None) -> None:
        repository_row = self._require_repository(owner, repository, minimum_permission="push")
        self._require_branch(repository_row["id"], ref)
        if inputs is not None and not isinstance(inputs, Mapping):
            raise GitHubValidationError("Validation Failed: inputs must be an object")
        run = self.create_workflow_run(
            owner, repository, name=str(workflow_id), event="workflow_dispatch",
            head_branch=ref, status="pending",
        )
        self.create_workflow_job(owner, repository, run["id"], name=str(workflow_id), status="pending")
        return None

    def complete_workflow_run(self, owner: str, repository: str, run_id: int,
                              *, conclusion: str, log: str) -> dict[str, Any]:
        actor = self._require_actor()
        if not actor["site_admin"]:
            raise GitHubForbidden("Resource not accessible by integration")
        repository_row = self._require_repository(owner, repository)
        self._require_workflow_run(repository_row["id"], run_id)
        allowed = {"success", "failure", "neutral", "cancelled", "skipped", "timed_out", "action_required", "stale"}
        if conclusion not in allowed:
            raise GitHubValidationError("Validation Failed: invalid workflow conclusion")
        timestamp = self._now_value()
        self.session.execute(
            "UPDATE github_workflow_runs SET status='completed',conclusion=?,updated_at=? WHERE id=? AND repository_id=?",
            (conclusion, timestamp, run_id, repository_row["id"]),
        )
        self.session.execute(
            "UPDATE github_workflow_jobs SET status='completed',conclusion=?,started_at=COALESCE(started_at,?),completed_at=?,log=? WHERE run_id=?",
            (conclusion, timestamp, timestamp, log, run_id),
        )
        return self._workflow_run_dict(self._require_workflow_run(repository_row["id"], run_id), repository_row)

    def create_workflow_job(self, owner: str, repository: str, run_id: int, *, name: str,
                            status: str = "queued", log: str = "") -> dict[str, Any]:
        repository_row = self._require_repository(owner, repository, minimum_permission="push")
        self._require_workflow_run(repository_row["id"], run_id)
        if status not in {"queued", "in_progress", "completed", "waiting", "pending"}:
            raise GitHubValidationError("Validation Failed: invalid job status")
        timestamp = self._now_value()
        job_id = self.session.execute(
            "INSERT INTO github_workflow_jobs(run_id,name,status,conclusion,started_at,completed_at,log) VALUES(?,?,?,NULL,?,NULL,?) RETURNING id",
            (run_id, name, status, timestamp if status != "queued" else None, log),
        ).fetchone()["id"]
        return self.get_workflow_job(owner, repository, job_id)

    def update_workflow_job(self, owner: str, repository: str, job_id: int, *, status: str,
                            conclusion: str | None = None, log: str | None = None) -> dict[str, Any]:
        self._require_repository(owner, repository, minimum_permission="push")
        self.get_workflow_job(owner, repository, job_id)
        if status not in {"queued", "in_progress", "completed", "waiting", "pending"}:
            raise GitHubValidationError("Validation Failed: invalid job status")
        allowed = {None, "success", "failure", "neutral", "cancelled", "skipped", "timed_out", "action_required", "stale"}
        if conclusion not in allowed or (status == "completed") != (conclusion is not None):
            raise GitHubValidationError("Validation Failed: conclusion must match completed status")
        timestamp = self._now_value()
        self.session.execute(
            "UPDATE github_workflow_jobs SET status=?,conclusion=?,completed_at=?,log=COALESCE(?,log) WHERE id=?",
            (status, conclusion, timestamp if status == "completed" else None, log, job_id),
        )
        return self.get_workflow_job(owner, repository, job_id)

    def update_workflow_run(self, owner: str, repository: str, run_id: int, *, status: str,
                            conclusion: str | None = None) -> dict[str, Any]:
        repository_row = self._require_repository(owner, repository, minimum_permission="push")
        self._require_workflow_run(repository_row["id"], run_id)
        allowed_status = {"queued", "in_progress", "completed", "waiting", "pending"}
        allowed_conclusion = {None, "success", "failure", "neutral", "cancelled", "skipped", "timed_out", "action_required", "stale"}
        if status not in allowed_status or conclusion not in allowed_conclusion or (status == "completed") != (conclusion is not None):
            raise GitHubValidationError("Validation Failed: invalid workflow result")
        self.session.execute(
            "UPDATE github_workflow_runs SET status=?,conclusion=?,updated_at=? WHERE id=?",
            (status, conclusion, self._now_value(), run_id),
        )
        return self._workflow_run_dict(self._require_workflow_run(repository_row["id"], run_id), repository_row)

    def list_releases(self, owner: str, repository: str) -> list[dict[str, Any]]:
        repository_row = self._require_repository(owner, repository)
        rows = self.session.execute(
            "SELECT * FROM github_releases WHERE repository_id=? ORDER BY id DESC",
            (repository_row["id"],),
        ).fetchall()
        return [self._release_dict(row, repository_row) for row in rows]

    def create_release(self, owner: str, repository: str, *, tag_name: str,
                       target_commitish: str = "main", name: str | None = None,
                       body: str | None = None, draft: bool = False,
                       prerelease: bool = False) -> dict[str, Any]:
        repository_row = self._require_repository(owner, repository, minimum_permission="push")
        if not tag_name.strip():
            raise GitHubValidationError("Validation Failed: tag_name is required")
        branch = self._require_branch(repository_row["id"], target_commitish)
        tag = self.session.execute(
            "SELECT target_sha FROM github_tags WHERE repository_id=? AND name=?",
            (repository_row["id"], tag_name),
        ).fetchone()
        if tag is None:
            self.create_ref(owner, repository, ref=f"refs/tags/{tag_name}", sha=branch["head_sha"])
        timestamp = self._now_value()
        try:
            release_id = self.session.execute(
                """INSERT INTO github_releases(repository_id,tag_name,target_commitish,name,body,draft,prerelease,author_id,created_at,published_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?) RETURNING id""",
                (repository_row["id"], tag_name, branch["name"], name, body, draft, prerelease,
                 self._require_actor()["id"], timestamp, None if draft else timestamp),
            ).fetchone()["id"]
        except Exception as exc:
            raise GitHubValidationError("Validation Failed: release tag already exists") from exc
        row = self.session.execute("SELECT * FROM github_releases WHERE id=?", (release_id,)).fetchone()
        return self._release_dict(row, repository_row)

    def update_release(self, owner: str, repository: str, *, release_id: int,
                       tag_name: str | None = None, target_commitish: str | None = None,
                       name: str | None = None, body: str | None = None,
                       draft: bool | None = None, prerelease: bool | None = None,
                       make_latest: str = "true",
                       discussion_category_name: str | None = None) -> dict[str, Any]:
        """Mirror of GitHub REST `PATCH /repos/{owner}/{repo}/releases/{release_id}`.

        Partial update: only supplied fields change. Publishing a draft
        (draft=false on a currently-drafted release) stamps published_at, the
        same transition create_release models. `make_latest` /
        `discussion_category_name` are accepted for API fidelity but, like the
        create replica, are not separately modelled (no latest pointer or
        discussions table).
        """
        repository_row = self._require_repository(owner, repository, minimum_permission="push")
        row = self.session.execute(
            "SELECT * FROM github_releases WHERE id=? AND repository_id=?",
            (release_id, repository_row["id"]),
        ).fetchone()
        if row is None:
            raise GitHubNotFound("Not Found")
        if make_latest not in ("true", "false", "legacy"):
            raise GitHubValidationError("Validation Failed: make_latest must be true, false, or legacy")

        assignments: list[str] = []
        values: list[Any] = []
        if tag_name is not None:
            if not tag_name.strip():
                raise GitHubValidationError("Validation Failed: tag_name is required")
            assignments.append("tag_name=?")
            values.append(tag_name)
        if target_commitish is not None:
            branch = self._require_branch(repository_row["id"], target_commitish)
            assignments.append("target_commitish=?")
            values.append(branch["name"])
        if name is not None:
            assignments.append("name=?")
            values.append(name)
        if body is not None:
            assignments.append("body=?")
            values.append(body)
        if prerelease is not None:
            assignments.append("prerelease=?")
            values.append(prerelease)
        if draft is not None:
            assignments.append("draft=?")
            values.append(draft)
            # publishing a draft stamps published_at; re-drafting clears it
            if draft and row["published_at"] is not None:
                assignments.append("published_at=?")
                values.append(None)
            elif not draft and row["published_at"] is None:
                assignments.append("published_at=?")
                values.append(self._now_value())

        if assignments:
            try:
                self.session.execute(
                    f"UPDATE github_releases SET {', '.join(assignments)} WHERE id=?",
                    (*values, release_id),
                )
            except Exception as exc:
                raise GitHubValidationError("Validation Failed: release tag already exists") from exc
        row = self.session.execute("SELECT * FROM github_releases WHERE id=?", (release_id,)).fetchone()
        return self._release_dict(row, repository_row)

    # Repository contents.
    #
    # `get_content` and `get_tree` mirror the REST endpoints; `get_file_contents`
    # and `get_repository_tree` mirror the official github-mcp-server tools of
    # the same name, including their reference resolution, resource shapes and
    # 404 recovery, because those are the tools a solver actually calls.

    def get_content(
        self,
        owner: str,
        repository: str,
        path: str = "",
        *,
        ref: str | None = None,
    ) -> dict[str, Any] | list[dict[str, Any]]:
        """Mirror of GitHub REST `GET /repos/{owner}/{repo}/contents/{path}`.

        A file comes back as one object with base64 `content`; a directory as an
        array of the same objects without `content`; an empty path lists the
        repository root. A file over 1 MB carries `content: ""` and
        `encoding: "none"`, exactly as the real endpoint does.
        """
        repository_row = self._require_repository(owner, repository)
        _, commit_sha, _ = self._resolve_git_reference(repository_row, ref)
        git_repository = self._git_repository(repository_row["id"])
        normalized = self._normalize_content_path(path)
        label = self._ref_label(repository_row, ref)
        if not normalized:
            children = self._tree_children(git_repository, commit_sha)
            return [self._content_entry(repository_row, child, "", label) for child in children]
        try:
            matches = git_repository.list_tree(commit_sha, path=normalized, with_size=True)
        except GitStorageError as exc:
            raise GitHubNotFound("Not Found") from exc
        entry = next((item for item in matches if item["path"] == normalized), None)
        if entry is None:
            raise GitHubNotFound("Not Found")
        if entry["type"] == "tree":
            children = self._tree_children(git_repository, entry["id"])
            return [self._content_entry(repository_row, child, normalized, label) for child in children]
        payload = git_repository.read_file(commit_sha, normalized)
        return self._content_entry(repository_row, entry, "", label, payload=payload)

    def get_tree(
        self,
        owner: str,
        repository: str,
        tree_sha: str,
        *,
        recursive: bool | str = False,
    ) -> dict[str, Any]:
        """Mirror of GitHub REST `GET /repos/{owner}/{repo}/git/trees/{tree_sha}`.

        `tree_sha` is a commit SHA, a tree SHA, or a branch or tag name.
        `recursive` follows REST semantics: any value except `0`/`false` enables
        it. The replica never truncates, so `truncated` is always false.
        """
        repository_row = self._require_repository(owner, repository)
        git_repository = self._git_repository(repository_row["id"])
        revision = self._resolve_tree_revision(repository_row, tree_sha)
        try:
            root_sha = str(git_repository.commit_metadata(revision)["tree_sha"])
        except GitStorageError:
            root_sha = revision  # already a tree object
        try:
            entries = git_repository.list_tree(
                revision, recursive=self._truthy(recursive), with_size=True
            )
        except GitStorageError as exc:
            raise GitHubNotFound("Not Found") from exc
        full_name = repository_row["full_name"]
        tree: list[dict[str, Any]] = []
        for entry in entries:
            is_directory = entry["type"] == "tree"
            item: dict[str, Any] = {
                "path": entry["path"],
                "mode": entry["mode"],
                "type": entry["type"],
                "sha": entry["id"],
                "url": f"https://api.github.com/repos/{full_name}/git/{'trees' if is_directory else 'blobs'}/{entry['id']}",
            }
            if not is_directory:
                item["size"] = int(entry["size"] or 0)
            tree.append(item)
        return {
            "sha": root_sha,
            "url": f"https://api.github.com/repos/{full_name}/git/trees/{root_sha}",
            "tree": tree,
            "truncated": False,
        }

    def get_file_contents(
        self,
        owner: str,
        repository: str,
        *,
        path: str = "/",
        ref: str | None = None,
        sha: str | None = None,
        fields: Sequence[str] | None = None,
    ) -> dict[str, Any] | list[dict[str, Any]]:
        """Mirror of the official github-mcp-server `get_file_contents` tool.

        A file is returned resource-style — `{uri, mimeType, text|blob}` — with
        the URI the official server builds; a file of 1 MB or more becomes a
        resource link `{uri, download_url}`. A directory returns Contents
        entries, optionally narrowed to `fields`. A path that does not exist
        falls back to suffix matches in the recursive tree (at most three), and
        only a total miss is a 404.
        """
        repository_row = self._require_repository(owner, repository)
        resolved_ref, commit_sha, fallback_used = self._resolve_git_reference(
            repository_row, ref, sha
        )
        reference = resolved_ref or commit_sha
        note: str | None = None
        if fallback_used:
            note = (
                f"Note: the provided ref {(ref or '').strip()!r} doesn't exist, "
                f"default branch {repository_row['default_branch']!r} was used instead"
            )
        normalized = self._normalize_content_path(path)
        try:
            content = self.get_content(owner, repository, normalized, ref=reference)
        except GitHubNotFound:
            return self._match_files(repository_row, commit_sha, reference, normalized, note)
        if isinstance(content, list):
            entries = [self._select_fields(entry, fields) for entry in content]
            return entries if note is None else {"note": note, "contents": entries}
        return self._file_resource(repository_row, content, normalized, reference, note)

    def get_repository_tree(
        self,
        owner: str,
        repository: str,
        *,
        tree_sha: str | None = None,
        recursive: bool | str = False,
        path_filter: str | None = None,
    ) -> dict[str, Any]:
        """Mirror of the official github-mcp-server `get_repository_tree` tool.

        `tree_sha` defaults to the repository's default branch; `path_filter`
        keeps only entries whose path starts with the given prefix.
        """
        repository_row = self._require_repository(owner, repository)
        effective = (tree_sha or "").strip() or repository_row["default_branch"]
        recursive_flag = self._truthy(recursive)
        tree = self.get_tree(owner, repository, effective, recursive=recursive_flag)
        entries = tree["tree"]
        if path_filter:
            entries = [entry for entry in entries if entry["path"].startswith(path_filter)]
        return {
            "sha": tree["sha"],
            "truncated": tree["truncated"],
            "tree": entries,
            "tree_sha": effective,
            "owner": repository_row["owner_login"],
            "repo": repository_row["name"],
            "recursive": recursive_flag,
            "count": len(entries),
        }

    # Content helpers

    @staticmethod
    def _looks_like_sha(value: str | None) -> bool:
        return bool(value) and _FULL_SHA.fullmatch(value.strip()) is not None

    def _resolve_git_reference(
        self,
        repository_row: Mapping[str, Any],
        ref: str | None = None,
        sha: str | None = None,
    ) -> tuple[str | None, str, bool]:
        """Port of github-mcp-server's `resolveGitReference`.

        Returns `(fully qualified ref or None, commit sha, fallback_used)`.
        `sha` wins over `ref`; a SHA-looking `ref` is used as a SHA; an empty
        `ref` means the default branch; `refs/...` is used as given;
        `heads/...`/`tags/...` are prefixed with `refs/`; a short name is tried
        as a branch, then as a tag, and only the literal name `main` falls back
        to the default branch.
        """
        if sha:
            return None, sha.strip(), False
        original = (ref or "").strip()
        if self._looks_like_sha(original):
            return None, original, False
        default_branch = repository_row["default_branch"]
        fallback_used = False
        if not original:
            resolved = f"refs/heads/{default_branch}"
        elif original.startswith("refs/"):
            resolved = original
        elif original.startswith("heads/") or original.startswith("tags/"):
            resolved = f"refs/{original}"
        else:
            branch_ref = f"refs/heads/{original}"
            tag_ref = f"refs/tags/{original}"
            if self._ref_sha(repository_row["id"], branch_ref) is not None:
                resolved = branch_ref
            elif self._ref_sha(repository_row["id"], tag_ref) is not None:
                resolved = tag_ref
            elif original == "main":
                resolved = f"refs/heads/{default_branch}"
                fallback_used = True
            else:
                raise GitHubNotFound(
                    f"could not resolve ref {original!r} as a branch or a tag"
                )
        commit_sha = self._ref_sha(repository_row["id"], resolved)
        if commit_sha is None and resolved == "refs/heads/main":
            resolved = f"refs/heads/{default_branch}"
            commit_sha = self._ref_sha(repository_row["id"], resolved)
            fallback_used = commit_sha is not None
        if commit_sha is None:
            raise GitHubNotFound(
                f"could not resolve ref {(original or resolved)!r} as a branch or a tag"
            )
        return resolved, commit_sha, fallback_used

    def _ref_sha(self, repository_id: int, ref: str) -> str | None:
        try:
            kind, name = self._split_ref(ref)
        except GitHubValidationError:
            return None  # refs/pull/... and friends are not modelled
        row = self._ref_row(repository_id, kind, name)
        return None if row is None else str(row["sha"])

    def _resolve_tree_revision(
        self, repository_row: Mapping[str, Any], tree_sha: str | None
    ) -> str:
        value = (tree_sha or "").strip()
        if not value:
            raise GitHubValidationError("Validation Failed: tree_sha is required")
        if _GIT_SHA.fullmatch(value):
            return value
        for kind in ("heads", "tags"):
            row = self._ref_row(repository_row["id"], kind, value.removeprefix(f"refs/{kind}/"))
            if row is not None:
                return str(row["sha"])
        raise GitHubNotFound("Not Found")

    def _git_repository(self, repository_id: int) -> Any:
        if self.git_data_plane is None:
            raise GitHubConflict("Git data plane is not configured")
        return self.git_data_plane.repository(repository_id)

    @staticmethod
    def _tree_children(git_repository: Any, revision: str) -> list[dict[str, Any]]:
        try:
            return git_repository.list_tree(revision, with_size=True)
        except GitStorageError as exc:
            raise GitHubNotFound("Not Found") from exc

    @staticmethod
    def _normalize_content_path(path: str | None) -> str:
        value = (path or "").strip()
        if value in ("", "/", "."):
            return ""
        normalized = value.strip("/")
        if "\x00" in normalized or any(
            part in ("", ".", "..") for part in normalized.split("/")
        ):
            raise GitHubValidationError(
                f"Validation Failed: invalid repository path {value!r}"
            )
        return normalized

    @staticmethod
    def _ref_label(repository_row: Mapping[str, Any], ref: str | None) -> str:
        value = (ref or "").strip()
        if not value:
            return str(repository_row["default_branch"])
        for prefix in ("refs/heads/", "refs/tags/", "heads/", "tags/"):
            if value.startswith(prefix):
                return value[len(prefix):]
        return value

    def _content_entry(
        self,
        repository_row: Mapping[str, Any],
        entry: Mapping[str, Any],
        prefix: str,
        label: str,
        *,
        payload: bytes | None = None,
    ) -> dict[str, Any]:
        full_path = "/".join(part for part in (prefix, entry["path"]) if part)
        is_directory = entry["type"] == "tree"
        full_name = repository_row["full_name"]
        api_url = f"https://api.github.com/repos/{full_name}/contents/{full_path}?ref={label}"
        git_url = f"https://api.github.com/repos/{full_name}/git/{'trees' if is_directory else 'blobs'}/{entry['id']}"
        html_url = f"https://github.com/{full_name}/{'tree' if is_directory else 'blob'}/{label}/{full_path}"
        result: dict[str, Any] = {
            "type": "dir" if is_directory else "file",
            "size": 0 if is_directory else int(entry.get("size") or 0),
            "name": full_path.rsplit("/", 1)[-1],
            "path": full_path,
            "sha": entry["id"],
            "url": api_url,
            "git_url": git_url,
            "html_url": html_url,
            "download_url": None if is_directory else f"https://raw.githubusercontent.com/{full_name}/{label}/{full_path}",
            "_links": {"self": api_url, "git": git_url, "html": html_url},
        }
        if payload is not None:
            if result["size"] > _MAX_INLINE_CONTENT:
                result["content"] = ""
                result["encoding"] = "none"
            else:
                result["content"] = base64.b64encode(payload).decode()
                result["encoding"] = "base64"
        return result

    @staticmethod
    def _select_fields(
        entry: Mapping[str, Any], fields: Sequence[str] | None
    ) -> dict[str, Any]:
        if not fields:
            return dict(entry)
        selected = set(fields)
        return {key: value for key, value in entry.items() if key in selected}

    def _file_resource(
        self,
        repository_row: Mapping[str, Any],
        content: Mapping[str, Any],
        path: str,
        reference: str,
        note: str | None,
    ) -> dict[str, Any]:
        uri = f"repo://{repository_row['full_name']}/{reference}/contents/{path}"
        size = int(content["size"])
        if content.get("encoding") == "none" or size >= _MAX_INLINE_CONTENT:
            result: dict[str, Any] = {"uri": uri, "download_url": content["download_url"]}
        else:
            payload = base64.b64decode(content["content"])
            if not payload:
                result = {"uri": uri, "mimeType": "text/plain", "text": ""}
            else:
                media_type, is_text = self._detect_media_type(path, payload)
                result = {"uri": uri, "mimeType": media_type}
                if is_text:
                    result["text"] = payload.decode("utf-8", errors="replace")
                else:
                    result["blob"] = base64.b64encode(payload).decode()
        if note is not None:
            result["note"] = note
        return result

    def _match_files(
        self,
        repository_row: Mapping[str, Any],
        commit_sha: str,
        reference: str,
        path: str,
        note: str | None,
    ) -> dict[str, Any]:
        """The official 404 recovery: suffix matches in the recursive tree."""
        target = path.strip("/")
        matches: list[str] = []
        if target:
            git_repository = self._git_repository(repository_row["id"])
            try:
                entries = git_repository.list_tree(commit_sha, recursive=True)
            except GitStorageError:
                entries = []
            directories: set[str] = set()
            files: list[str] = []
            for entry in entries:
                files.append(entry["path"])
                parts = entry["path"].split("/")[:-1]
                for index in range(1, len(parts) + 1):
                    directories.add("/".join(parts[:index]))
            matches = [
                candidate
                for candidate in sorted(files)
                if candidate == target or candidate.endswith(f"/{target}")
            ]
            matches += [
                f"{candidate}/"
                for candidate in sorted(directories)
                if candidate == target or candidate.endswith(f"/{target}")
            ]
            matches = matches[:3]
        if not matches:
            raise GitHubNotFound(
                "Failed to get file contents. The path does not point to a file "
                "or directory, or the file does not exist in the repository."
            )
        message = (
            "Resolved potential matches in the repository tree "
            f"(resolved refs: {reference}, matching files: {', '.join(matches)})"
        )
        return {
            "note": message if note is None else f"{note}. {message}",
            "resolved_refs": [reference],
            "matching_files": matches,
        }

    @staticmethod
    def _detect_media_type(path: str, payload: bytes) -> tuple[str, bool]:
        """(media type, is_text) — extension first, utf-8 decode as the tiebreak."""
        guessed, _ = mimetypes.guess_type(path)
        if guessed:
            is_text = (
                guessed.startswith("text/")
                or guessed in ("application/json", "application/xml")
                or guessed.endswith("+json")
                or guessed.endswith("+xml")
            )
            return guessed, is_text
        if b"\x00" in payload:
            return "application/octet-stream", False
        try:
            payload.decode("utf-8")
        except UnicodeDecodeError:
            return "application/octet-stream", False
        return "text/plain", True

    @staticmethod
    def _truthy(value: Any) -> bool:
        """REST `recursive`: any value but `0`/`false` enables it."""
        if isinstance(value, bool):
            return value
        if value is None:
            return False
        if isinstance(value, str):
            return value.strip().lower() not in ("", "0", "false")
        return bool(value)

    @staticmethod
    def _split_ref(ref: str) -> tuple[str, str]:
        normalized = ref.removeprefix("refs/").strip("/")
        kind, separator, name = normalized.partition("/")
        if separator != "/" or kind not in {"heads", "tags"} or not name:
            raise GitHubValidationError("Validation Failed: ref must start with refs/heads/ or refs/tags/")
        return kind, name

    def _ref_row(self, repository_id: int, kind: str, name: str) -> Mapping[str, Any] | None:
        if kind == "heads":
            return self.session.execute(
                "SELECT head_sha AS sha FROM github_branches WHERE repository_id=? AND name=?",
                (repository_id, name),
            ).fetchone()
        return self.session.execute(
            "SELECT target_sha AS sha FROM github_tags WHERE repository_id=? AND name=?",
            (repository_id, name),
        ).fetchone()

    @staticmethod
    def _ref_dict(repository: Mapping[str, Any], kind: str, name: str, sha: str) -> dict[str, Any]:
        full_ref = f"refs/{kind}/{name}"
        return {
            "ref": full_ref,
            "node_id": None,
            "url": f"https://api.github.local/repos/{repository['full_name']}/git/{full_ref}",
            "object": {"type": "commit", "sha": sha, "url": f"https://api.github.local/repos/{repository['full_name']}/git/commits/{sha}"},
        }

    # Pull requests

    def create_pull_request(
        self,
        owner: str,
        repository: str,
        *,
        title: str,
        head: str,
        base: str,
        body: str | None = None,
        draft: bool = False,
    ) -> dict[str, Any]:
        repository_row = self._require_repository(
            owner, repository, minimum_permission="push"
        )
        actor = self._require_actor()
        if not title.strip():
            raise GitHubValidationError("Validation Failed: title is required")
        head_branch = self._require_branch(repository_row["id"], head)
        base_branch = self._require_branch(repository_row["id"], base)
        if head_branch["name"].casefold() == base_branch["name"].casefold():
            raise GitHubValidationError("Validation Failed: head and base must differ")
        if head_branch["head_sha"] == base_branch["head_sha"]:
            raise GitHubValidationError("Validation Failed: no commits between head and base")
        existing = self.session.execute(
            """
            SELECT 1 FROM github_pull_requests pull
              JOIN github_issues issue ON issue.id = pull.issue_id
             WHERE issue.repository_id = ? AND issue.state = 'open'
               AND lower(pull.head_ref) = lower(?)
               AND lower(pull.base_ref) = lower(?)
            """,
            (repository_row["id"], head, base),
        ).fetchone()
        if existing:
            raise GitHubValidationError(
                "Validation Failed: a pull request already exists for these branches"
            )

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
                True,
                0,
                timestamp,
                timestamp,
                None,
            ),
        ).fetchone()["id"]
        self.session.execute(
            """
            INSERT INTO github_pull_requests(
                issue_id, head_ref, head_sha, base_ref, base_sha, draft,
                mergeable_state, merged, merged_by_id, merged_at, merge_commit_sha
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                issue_id,
                head_branch["name"],
                head_branch["head_sha"],
                base_branch["name"],
                base_branch["head_sha"],
                draft,
                "clean",
                False,
                None,
                None,
                None,
            ),
        )
        return self.get_pull_request(owner, repository, issue_number)

    def list_pull_requests(
        self, owner: str, repository: str, *, state: str = "open"
    ) -> list[dict[str, Any]]:
        repository_row = self._require_repository(owner, repository)
        if state not in ("open", "closed", "all"):
            raise GitHubValidationError("Validation Failed: invalid state")
        statement = """
            SELECT issue.number FROM github_issues issue
              JOIN github_pull_requests pull ON pull.issue_id = issue.id
             WHERE issue.repository_id = ?
        """
        parameters: list[Any] = [repository_row["id"]]
        if state != "all":
            statement += " AND issue.state = ?"
            parameters.append(state)
        statement += " ORDER BY issue.number DESC"
        rows = self.session.execute(statement, tuple(parameters)).fetchall()
        return [
            self.get_pull_request(owner, repository, row["number"]) for row in rows
        ]

    def get_pull_request(
        self, owner: str, repository: str, pull_number: int
    ) -> dict[str, Any]:
        repository_row = self._require_repository(owner, repository)
        row = self._require_pull_request(repository_row["id"], pull_number)
        return self._pull_request_dict(row, repository_row)

    def update_pull_request(
        self,
        owner: str,
        repository: str,
        pull_number: int,
        *,
        title: str | object = _UNSET,
        body: str | None | object = _UNSET,
        state: str | object = _UNSET,
        base: str | object = _UNSET,
        draft: bool | object = _UNSET,
    ) -> dict[str, Any]:
        repository_row = self._require_repository(
            owner, repository, minimum_permission="push"
        )
        pull = self._require_pull_request(repository_row["id"], pull_number)
        if pull["merged"] and state == "open":
            raise GitHubConflict("Merged pull requests cannot be reopened")
        if title is not _UNSET or body is not _UNSET or state is not _UNSET:
            self.update_issue(
                owner,
                repository,
                pull_number,
                title=title,
                body=body,
                state=state,
            )
        assignments: list[str] = []
        parameters: list[Any] = []
        if base is not _UNSET:
            if not isinstance(base, str):
                raise GitHubValidationError("Validation Failed: invalid base")
            branch = self._require_branch(repository_row["id"], base)
            if branch["name"].casefold() == pull["head_ref"].casefold():
                raise GitHubValidationError("Validation Failed: head and base must differ")
            assignments.extend(("base_ref = ?", "base_sha = ?"))
            parameters.extend((branch["name"], branch["head_sha"]))
        if draft is not _UNSET:
            if not isinstance(draft, bool):
                raise GitHubValidationError("Validation Failed: draft must be boolean")
            assignments.append("draft = ?")
            parameters.append(draft)
        if assignments:
            parameters.append(pull["issue_id"])
            self.session.execute(
                f"UPDATE github_pull_requests SET {', '.join(assignments)} WHERE issue_id = ?",
                tuple(parameters),
            )
        return self.get_pull_request(owner, repository, pull_number)

    # Review requests and reviews

    def list_requested_reviewers(
        self, owner: str, repository: str, pull_number: int
    ) -> dict[str, Any]:
        repository_row = self._require_repository(owner, repository)
        pull = self._require_pull_request(repository_row["id"], pull_number)
        rows = self.session.execute(
            """
            SELECT user_row.* FROM github_pull_request_reviewers reviewer
              JOIN github_users user_row ON user_row.id = reviewer.user_id
             WHERE reviewer.issue_id = ? ORDER BY lower(user_row.login)
            """,
            (pull["issue_id"],),
        ).fetchall()
        return {"users": [self._user_dict(row) for row in rows], "teams": []}

    def request_reviewers(
        self,
        owner: str,
        repository: str,
        pull_number: int,
        reviewers: Sequence[str],
    ) -> dict[str, Any]:
        repository_row = self._require_repository(
            owner, repository, minimum_permission="push"
        )
        pull = self._require_pull_request(repository_row["id"], pull_number)
        if pull["state"] != "open":
            raise GitHubValidationError("Validation Failed: pull request is not open")
        users = self._resolve_assignees(repository_row["id"], reviewers)
        for user in users:
            if user["id"] == pull["author_id"]:
                raise GitHubValidationError("Validation Failed: author cannot review own pull request")
            self.session.execute(
                """
                INSERT INTO github_pull_request_reviewers(issue_id, user_id)
                VALUES (?, ?) ON CONFLICT DO NOTHING
                """,
                (pull["issue_id"], user["id"]),
            )
        return self.get_pull_request(owner, repository, pull_number)

    def remove_requested_reviewers(
        self,
        owner: str,
        repository: str,
        pull_number: int,
        reviewers: Sequence[str],
    ) -> dict[str, Any]:
        repository_row = self._require_repository(
            owner, repository, minimum_permission="push"
        )
        pull = self._require_pull_request(repository_row["id"], pull_number)
        users = self._resolve_assignees(repository_row["id"], reviewers)
        for user in users:
            self.session.execute(
                """
                DELETE FROM github_pull_request_reviewers
                 WHERE issue_id = ? AND user_id = ?
                """,
                (pull["issue_id"], user["id"]),
            )
        return self.get_pull_request(owner, repository, pull_number)

    def create_review(
        self,
        owner: str,
        repository: str,
        pull_number: int,
        *,
        event: str,
        body: str | None = None,
        commit_sha: str | None = None,
    ) -> dict[str, Any]:
        repository_row = self._require_repository(
            owner, repository, minimum_permission="pull"
        )
        pull = self._require_pull_request(repository_row["id"], pull_number)
        actor = self._require_actor()
        event_to_state = {
            "APPROVE": "APPROVED",
            "REQUEST_CHANGES": "CHANGES_REQUESTED",
            "COMMENT": "COMMENTED",
            "PENDING": "PENDING",
        }
        if event not in event_to_state:
            raise GitHubValidationError("Validation Failed: invalid review event")
        if actor["id"] == pull["author_id"] and event in ("APPROVE", "REQUEST_CHANGES"):
            raise GitHubValidationError("Validation Failed: author cannot review own pull request")
        selected_sha = commit_sha or pull["head_sha"]
        self._require_commit(repository_row["id"], selected_sha)
        submitted_at = None if event == "PENDING" else self._now_value()
        review_id = self.session.execute(
            """
            INSERT INTO github_pull_request_reviews(
                issue_id, reviewer_id, state, body, commit_sha, submitted_at
            ) VALUES (?, ?, ?, ?, ?, ?) RETURNING id
            """,
            (
                pull["issue_id"],
                actor["id"],
                event_to_state[event],
                body,
                selected_sha,
                submitted_at,
            ),
        ).fetchone()["id"]
        self.session.execute(
            """
            DELETE FROM github_pull_request_reviewers
             WHERE issue_id = ? AND user_id = ?
            """,
            (pull["issue_id"], actor["id"]),
        )
        return self._review_dict(self._require_review(review_id), repository_row, pull_number)

    def list_reviews(
        self, owner: str, repository: str, pull_number: int
    ) -> list[dict[str, Any]]:
        repository_row = self._require_repository(owner, repository)
        pull = self._require_pull_request(repository_row["id"], pull_number)
        rows = self.session.execute(
            """
            SELECT review.*, user_row.login AS reviewer_login,
                   user_row.name AS reviewer_name, user_row.email AS reviewer_email,
                   user_row.user_type AS reviewer_type,
                   user_row.site_admin AS reviewer_site_admin
              FROM github_pull_request_reviews review
              JOIN github_users user_row ON user_row.id = review.reviewer_id
             WHERE review.issue_id = ? ORDER BY review.id
            """,
            (pull["issue_id"],),
        ).fetchall()
        return [self._review_dict(row, repository_row, pull_number) for row in rows]

    def create_review_comment(
        self,
        owner: str,
        repository: str,
        pull_number: int,
        *,
        body: str,
        path: str,
        commit_sha: str | None = None,
        line: int | None = None,
        side: str | None = None,
        review_id: int | None = None,
    ) -> dict[str, Any]:
        repository_row = self._require_repository(
            owner, repository, minimum_permission="pull"
        )
        pull = self._require_pull_request(repository_row["id"], pull_number)
        actor = self._require_actor()
        if not body:
            raise GitHubValidationError("Validation Failed: body is required")
        if not path or path.startswith("/") or ".." in path.split("/"):
            raise GitHubValidationError("Validation Failed: invalid path")
        if line is not None and (not isinstance(line, int) or line <= 0):
            raise GitHubValidationError("Validation Failed: line must be positive")
        if side not in (None, "LEFT", "RIGHT"):
            raise GitHubValidationError("Validation Failed: invalid side")
        selected_sha = self._require_commit(
            repository_row["id"], commit_sha or pull["head_sha"]
        )["sha"]
        if review_id is not None:
            review = self._require_review(review_id)
            if review["issue_id"] != pull["issue_id"]:
                raise GitHubValidationError(
                    "Validation Failed: review does not belong to pull request"
                )
        timestamp = self._now_value()
        comment_id = self.session.execute(
            """
            INSERT INTO github_pull_request_review_comments(
                review_id, issue_id, author_id, body, path, line, side,
                commit_sha, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id
            """,
            (
                review_id,
                pull["issue_id"],
                actor["id"],
                body,
                path,
                line,
                side,
                selected_sha,
                timestamp,
                timestamp,
            ),
        ).fetchone()["id"]
        return self._review_comment_dict(
            self._require_review_comment(comment_id), repository_row, pull_number
        )

    def list_review_comments(
        self, owner: str, repository: str, pull_number: int
    ) -> list[dict[str, Any]]:
        repository_row = self._require_repository(owner, repository)
        pull = self._require_pull_request(repository_row["id"], pull_number)
        rows = self.session.execute(
            """
            SELECT comment.*, user_row.login AS author_login,
                   user_row.name AS author_name, user_row.email AS author_email,
                   user_row.user_type AS author_type,
                   user_row.site_admin AS author_site_admin
              FROM github_pull_request_review_comments comment
              JOIN github_users user_row ON user_row.id = comment.author_id
             WHERE comment.issue_id = ? ORDER BY comment.id
            """,
            (pull["issue_id"],),
        ).fetchall()
        return [
            self._review_comment_dict(row, repository_row, pull_number)
            for row in rows
        ]

    # Merge

    def is_merged(self, owner: str, repository: str, pull_number: int) -> bool:
        repository_row = self._require_repository(owner, repository)
        return bool(
            self._require_pull_request(repository_row["id"], pull_number)["merged"]
        )

    def merge_pull_request_api(
        self,
        owner: str,
        repository: str,
        pull_number: int,
        *,
        commit_title: str | None = None,
        commit_message: str | None = None,
        sha: str | None = None,
        merge_method: str = "merge",
    ) -> dict[str, Any]:
        """Implement GitHub's public merge request shape over the real Git plane."""
        repository_row = self._require_repository(owner, repository, minimum_permission="push")
        pull = self._require_pull_request(repository_row["id"], pull_number)
        if merge_method not in {"merge", "squash", "rebase"}:
            raise GitHubValidationError("Validation Failed: invalid merge_method")
        if merge_method != "merge":
            raise GitHubConflict(f"Merge method {merge_method!r} is not enabled")
        if sha is not None and sha != pull["head_sha"]:
            raise GitHubConflict("Head branch was modified. Review and try the merge again.")
        if self.git_data_plane is None:
            raise GitHubConflict("Git data plane is not configured")
        git_repository = self.git_data_plane.repository(repository_row["id"])
        base_paths = {item["path"] for item in git_repository.list_tree(pull["base_sha"], recursive=True) if item["type"] == "blob"}
        head_paths = {item["path"] for item in git_repository.list_tree(pull["head_sha"], recursive=True) if item["type"] == "blob"}
        files: dict[str, bytes | None] = {path: git_repository.read_file(pull["head_sha"], path) for path in head_paths}
        files.update({path: None for path in base_paths - head_paths})
        title = commit_title or f"Merge pull request #{pull_number} from {owner}/{pull['head_ref']}"
        message = f"{title}\n\n{commit_message}" if commit_message else title
        created = self.create_commit(
            owner,
            repository,
            message=message,
            author=self.actor_login,
            parent_shas=(pull["base_sha"], pull["head_sha"]),
            files=files,
        )
        return self.merge_pull_request(
            owner, repository, pull_number, merge_commit_sha=created["sha"]
        )

    def merge_pull_request(
        self,
        owner: str,
        repository: str,
        pull_number: int,
        *,
        merge_commit_sha: str,
    ) -> dict[str, Any]:
        repository_row = self._require_repository(
            owner, repository, minimum_permission="push"
        )
        pull = self._require_pull_request(repository_row["id"], pull_number)
        if pull["merged"]:
            return {
                "sha": pull["merge_commit_sha"],
                "merged": True,
                "message": "Pull Request is already merged",
            }
        if pull["state"] != "open":
            raise GitHubConflict("Pull Request is not mergeable")
        if pull["draft"]:
            raise GitHubConflict("Draft Pull Requests are not mergeable")

        head_branch = self._require_branch(repository_row["id"], pull["head_ref"])
        base_branch = self._require_branch(repository_row["id"], pull["base_ref"])
        if base_branch["head_sha"] != pull["base_sha"]:
            self.session.execute(
                "UPDATE github_pull_requests SET mergeable_state = 'behind' WHERE issue_id = ?",
                (pull["issue_id"],),
            )
            raise GitHubConflict("Base branch was modified. Review and try the merge again.")
        merge_commit = self._require_commit(repository_row["id"], merge_commit_sha)
        parents = self.session.execute(
            """
            SELECT parent_sha FROM github_commit_parents
             WHERE commit_sha = ? ORDER BY position
            """,
            (merge_commit["sha"],),
        ).fetchall()
        parent_shas = {row["parent_sha"] for row in parents}
        expected_parents = {base_branch["head_sha"], head_branch["head_sha"]}
        if not expected_parents.issubset(parent_shas):
            raise GitHubValidationError(
                "Validation Failed: merge commit must reference current base and head"
            )

        timestamp = self._now_value()
        actor = self._require_actor()
        self.session.execute(
            """
            UPDATE github_pull_requests
               SET merged = ?, merged_by_id = ?, merged_at = ?,
                   merge_commit_sha = ?, mergeable_state = 'clean'
             WHERE issue_id = ?
            """,
            (True, actor["id"], timestamp, merge_commit["sha"], pull["issue_id"]),
        )
        self.session.execute(
            """
            UPDATE github_issues
               SET state = 'closed', state_reason = 'completed',
                   closed_at = ?, updated_at = ?
             WHERE id = ?
            """,
            (timestamp, timestamp, pull["issue_id"]),
        )
        self.session.execute(
            """
            UPDATE github_branches SET head_sha = ?
             WHERE repository_id = ? AND name = ?
            """,
            (merge_commit["sha"], repository_row["id"], base_branch["name"]),
        )
        if self.git_data_plane is not None:
            self.git_data_plane.repository(repository_row["id"]).update_branch(
                base_branch["name"],
                merge_commit["sha"],
                expected_old_sha=base_branch["head_sha"],
            )
        return {
            "sha": merge_commit["sha"],
            "merged": True,
            "message": "Pull Request successfully merged",
        }

    # Helpers and serializers

    def _require_commit(self, repository_id: int, sha: str) -> Mapping[str, Any]:
        normalized_sha = self._validate_sha(sha, "sha")
        row = self.session.execute(
            "SELECT * FROM github_commits WHERE repository_id = ? AND sha = ?",
            (repository_id, normalized_sha),
        ).fetchone()
        if row is None:
            raise GitHubValidationError("Validation Failed: commit does not exist")
        return row

    def _require_branch(self, repository_id: int, name: str) -> Mapping[str, Any]:
        row = self.session.execute(
            """
            SELECT * FROM github_branches
             WHERE repository_id = ? AND lower(name) = lower(?)
            """,
            (repository_id, name),
        ).fetchone()
        if row is None:
            raise GitHubValidationError("Validation Failed: branch does not exist")
        if row["head_sha"] is None:
            raise GitHubValidationError("Validation Failed: branch has no commits")
        return row

    def _require_workflow_run(self, repository_id: int, run_id: int) -> Mapping[str, Any]:
        row = self.session.execute(
            "SELECT * FROM github_workflow_runs WHERE repository_id=? AND id=?",
            (repository_id, run_id),
        ).fetchone()
        if row is None:
            raise GitHubNotFound("Not Found")
        return row

    def _workflow_run_dict(self, row: Mapping[str, Any], repository: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "id": row["id"], "name": row["name"], "event": row["event"],
            "status": row["status"], "conclusion": row["conclusion"],
            "head_branch": row["head_branch"], "head_sha": row["head_sha"],
            "run_number": row["run_number"], "run_attempt": row["run_attempt"],
            "created_at": row["created_at"], "updated_at": row["updated_at"],
            "jobs_url": f"https://api.github.com/repos/{repository['owner_login']}/{repository['name']}/actions/runs/{row['id']}/jobs",
        }

    def _release_dict(self, row: Mapping[str, Any], repository: Mapping[str, Any]) -> dict[str, Any]:
        author = self.session.execute("SELECT * FROM github_users WHERE id=?", (row["author_id"],)).fetchone()
        return {
            "id": row["id"], "tag_name": row["tag_name"],
            "target_commitish": row["target_commitish"], "name": row["name"],
            "body": row["body"], "draft": bool(row["draft"]),
            "prerelease": bool(row["prerelease"]), "author": self._user_dict(author),
            "created_at": row["created_at"], "published_at": row["published_at"],
            "html_url": f"https://github.com/{repository['full_name']}/releases/tag/{row['tag_name']}",
        }

    @staticmethod
    def _workflow_job_dict(row: Mapping[str, Any], owner: str, repository: str, run_id: int) -> dict[str, Any]:
        return {
            "id": row["id"], "run_id": run_id, "name": row["name"],
            "status": row["status"], "conclusion": row["conclusion"],
            "started_at": row["started_at"], "completed_at": row["completed_at"],
            "steps": [],
            "url": f"https://api.github.com/repos/{owner}/{repository}/actions/jobs/{row['id']}",
        }

    def _require_pull_request(
        self, repository_id: int, pull_number: int
    ) -> Mapping[str, Any]:
        row = self.session.execute(
            """
            SELECT issue.*, pull.issue_id, pull.head_ref, pull.head_sha,
                   pull.base_ref, pull.base_sha, pull.draft,
                   pull.mergeable_state, pull.merged, pull.merged_by_id,
                   pull.merged_at, pull.merge_commit_sha
              FROM github_issues issue
              JOIN github_pull_requests pull ON pull.issue_id = issue.id
             WHERE issue.repository_id = ? AND issue.number = ?
            """,
            (repository_id, pull_number),
        ).fetchone()
        if row is None:
            raise GitHubNotFound("Not Found")
        return row

    def _require_review(self, review_id: int) -> Mapping[str, Any]:
        row = self.session.execute(
            """
            SELECT review.*, user_row.login AS reviewer_login,
                   user_row.name AS reviewer_name, user_row.email AS reviewer_email,
                   user_row.user_type AS reviewer_type,
                   user_row.site_admin AS reviewer_site_admin
              FROM github_pull_request_reviews review
              JOIN github_users user_row ON user_row.id = review.reviewer_id
             WHERE review.id = ?
            """,
            (review_id,),
        ).fetchone()
        if row is None:
            raise GitHubNotFound("Not Found")
        return row

    def _require_review_comment(self, comment_id: int) -> Mapping[str, Any]:
        row = self.session.execute(
            """
            SELECT comment.*, user_row.login AS author_login,
                   user_row.name AS author_name, user_row.email AS author_email,
                   user_row.user_type AS author_type,
                   user_row.site_admin AS author_site_admin
              FROM github_pull_request_review_comments comment
              JOIN github_users user_row ON user_row.id = comment.author_id
             WHERE comment.id = ?
            """,
            (comment_id,),
        ).fetchone()
        if row is None:
            raise GitHubNotFound("Not Found")
        return row

    def _pull_request_dict(
        self, row: Mapping[str, Any], repository: Mapping[str, Any]
    ) -> dict[str, Any]:
        result = self._issue_dict(row, repository)
        for issue_only_field in ("state_reason", "repository_url", "pull_request"):
            result.pop(issue_only_field, None)
        result["url"] = (
            f"https://api.github.com/repos/{repository['full_name']}/pulls/{row['number']}"
        )
        result["html_url"] = (
            f"https://github.com/{repository['full_name']}/pull/{row['number']}"
        )
        result.update(
            {
                "draft": bool(row["draft"]),
                "merged": bool(row["merged"]),
                "mergeable_state": row["mergeable_state"],
                "merged_at": self._serialize_time(row["merged_at"]),
                "merge_commit_sha": row["merge_commit_sha"],
                "head": {
                    "ref": row["head_ref"],
                    "sha": row["head_sha"],
                    "label": f"{repository['owner_login']}:{row['head_ref']}",
                },
                "base": {
                    "ref": row["base_ref"],
                    "sha": row["base_sha"],
                    "label": f"{repository['owner_login']}:{row['base_ref']}",
                },
                "requested_reviewers": self.list_requested_reviewers(
                    repository["owner_login"], repository["name"], row["number"]
                )["users"],
                "requested_teams": [],
            }
        )
        return result

    def _review_dict(
        self,
        row: Mapping[str, Any],
        repository: Mapping[str, Any],
        pull_number: int,
    ) -> dict[str, Any]:
        return {
            "id": row["id"],
            "node_id": f"PRR_{row['id']}",
            "user": {
                "login": row["reviewer_login"],
                "id": row["reviewer_id"],
                "name": row["reviewer_name"],
                "email": row["reviewer_email"],
                "type": row["reviewer_type"],
                "site_admin": bool(row["reviewer_site_admin"]),
            },
            "body": row["body"],
            "state": row["state"],
            "commit_id": row["commit_sha"],
            "submitted_at": self._serialize_time(row["submitted_at"]),
            "pull_request_url": (
                f"https://api.github.com/repos/{repository['full_name']}/pulls/{pull_number}"
            ),
        }

    def _commit_dict(
        self, row: Mapping[str, Any], repository: Mapping[str, Any]
    ) -> dict[str, Any]:
        parents = self.session.execute(
            """
            SELECT parent_sha FROM github_commit_parents
             WHERE commit_sha = ? ORDER BY position
            """,
            (row["sha"],),
        ).fetchall()
        return {
            "sha": row["sha"],
            "node_id": f"C_{row['sha']}",
            "commit": {
                "message": row["message"],
                "author": {"date": self._serialize_time(row["authored_at"])},
                "committer": {"date": self._serialize_time(row["committed_at"])},
                "tree": {"sha": row["tree_sha"]},
            },
            "parents": [{"sha": parent["parent_sha"]} for parent in parents],
            "url": f"https://api.github.com/repos/{repository['full_name']}/commits/{row['sha']}",
            "html_url": f"https://github.com/{repository['full_name']}/commit/{row['sha']}",
        }

    def _review_comment_dict(
        self,
        row: Mapping[str, Any],
        repository: Mapping[str, Any],
        pull_number: int,
    ) -> dict[str, Any]:
        return {
            "id": row["id"],
            "node_id": f"PRRC_{row['id']}",
            "pull_request_review_id": row["review_id"],
            "body": row["body"],
            "path": row["path"],
            "line": row["line"],
            "side": row["side"],
            "commit_id": row["commit_sha"],
            "user": {
                "login": row["author_login"],
                "id": row["author_id"],
                "name": row["author_name"],
                "email": row["author_email"],
                "type": row["author_type"],
                "site_admin": bool(row["author_site_admin"]),
            },
            "created_at": self._serialize_time(row["created_at"]),
            "updated_at": self._serialize_time(row["updated_at"]),
            "url": (
                f"https://api.github.com/repos/{repository['full_name']}/pulls/comments/{row['id']}"
            ),
            "pull_request_url": (
                f"https://api.github.com/repos/{repository['full_name']}/pulls/{pull_number}"
            ),
        }

    def _branch_dict(
        self, row: Mapping[str, Any], repository: Mapping[str, Any]
    ) -> dict[str, Any]:
        return {
            "name": row["name"],
            "commit": {
                "sha": row["head_sha"],
                "url": f"https://api.github.com/repos/{repository['full_name']}/commits/{row['head_sha']}",
            },
            "protected": bool(row["protected"]),
        }

    @staticmethod
    def _validate_sha(value: str, field: str) -> str:
        if not isinstance(value, str) or not _GIT_SHA.fullmatch(value):
            raise GitHubValidationError(
                f"Validation Failed: {field} must be a 40 or 64 character hex SHA"
            )
        return value.lower()
