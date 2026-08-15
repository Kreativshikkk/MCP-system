"""Isolated local bare-Git repositories for service data planes."""

from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import tempfile
from typing import Mapping, Sequence

from .errors import ConfigurationError


class GitStorageError(ConfigurationError):
    """A local Git object or ref operation failed."""


class BareGitRepository:
    """Small safe wrapper around one local bare repository."""

    def __init__(self, path: Path) -> None:
        self.path = path.resolve()

    def initialize(self) -> None:
        if self.path.exists():
            if not (self.path / "HEAD").is_file():
                raise GitStorageError(f"invalid bare Git repository: {self.path}")
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._run("init", "--bare", str(self.path), git_dir=False)

    def create_commit(
        self,
        *,
        message: str,
        author_name: str,
        author_email: str,
        timestamp: datetime,
        parent_shas: Sequence[str] = (),
        files: Mapping[str, str | bytes | None] | None = None,
    ) -> dict[str, object]:
        """Create a real commit; ``files`` are changes relative to first parent."""
        self.initialize()
        if not message:
            raise GitStorageError("commit message is required")
        for parent_sha in parent_shas:
            self.require_object(parent_sha, "commit")

        index_fd, index_name = tempfile.mkstemp(
            prefix="mcp-git-index-", dir=self.path.parent
        )
        os.close(index_fd)
        os.unlink(index_name)
        index_path = Path(index_name)
        environment = {"GIT_INDEX_FILE": str(index_path)}
        try:
            if parent_shas:
                self._run("read-tree", parent_shas[0], extra_env=environment)
            for raw_path, content in sorted((files or {}).items()):
                file_path = self._validate_file_path(raw_path)
                if content is None:
                    self._run(
                        "update-index",
                        "--force-remove",
                        "--",
                        file_path,
                        extra_env=environment,
                    )
                    continue
                payload = content.encode("utf-8") if isinstance(content, str) else content
                blob_sha = self._run_bytes("hash-object", "-w", "--stdin", data=payload)
                self._run(
                    "update-index",
                    "--add",
                    "--cacheinfo",
                    "100644",
                    blob_sha,
                    file_path,
                    extra_env=environment,
                )
            tree_sha = self._run("write-tree", extra_env=environment)
        finally:
            index_path.unlink(missing_ok=True)

        git_date = self._git_date(timestamp)
        identity = {
            "GIT_AUTHOR_NAME": author_name,
            "GIT_AUTHOR_EMAIL": author_email,
            "GIT_AUTHOR_DATE": git_date,
            "GIT_COMMITTER_NAME": author_name,
            "GIT_COMMITTER_EMAIL": author_email,
            "GIT_COMMITTER_DATE": git_date,
        }
        arguments = ["commit-tree", tree_sha]
        for parent_sha in parent_shas:
            arguments.extend(("-p", parent_sha))
        sha = self._run_bytes(*arguments, data=(message + "\n").encode(), extra_env=identity)
        return {"sha": sha, "tree_sha": tree_sha, "parents": tuple(parent_shas)}

    def update_branch(
        self, name: str, sha: str, *, expected_old_sha: str | None = None
    ) -> None:
        self.initialize()
        self._run("check-ref-format", "--branch", name, git_dir=False)
        self.require_object(sha, "commit")
        arguments = ["update-ref", f"refs/heads/{name}", sha]
        if expected_old_sha is not None:
            arguments.append(expected_old_sha)
        self._run(*arguments)

    def is_ancestor(self, ancestor_sha: str, descendant_sha: str) -> bool:
        self.require_object(ancestor_sha, "commit")
        self.require_object(descendant_sha, "commit")
        result = self._run_process(
            "merge-base", "--is-ancestor", ancestor_sha, descendant_sha, check=False
        )
        if result.returncode not in {0, 1}:
            self._raise_process_error(result)
        return result.returncode == 0

    def resolve_branch(self, name: str) -> str | None:
        self.initialize()
        result = self._run_process(
            "rev-parse", "--verify", f"refs/heads/{name}", check=False
        )
        if result.returncode == 1 or result.returncode == 128:
            return None
        if result.returncode != 0:
            self._raise_process_error(result)
        return result.stdout.strip()

    def delete_branch(self, name: str) -> None:
        self.initialize()
        self._run("check-ref-format", "--branch", name, git_dir=False)
        if self.resolve_branch(name) is not None:
            self._run("update-ref", "-d", f"refs/heads/{name}")

    def object_type(self, sha: str) -> str | None:
        self.initialize()
        result = self._run_process("cat-file", "-t", sha, check=False)
        if result.returncode != 0:
            return None
        return result.stdout.strip()

    def require_object(self, sha: str, expected_type: str) -> None:
        actual_type = self.object_type(sha)
        if actual_type != expected_type:
            raise GitStorageError(
                f"Git object {sha!r} is not an existing {expected_type}"
            )

    def commit_metadata(self, sha: str) -> dict[str, object]:
        self.require_object(sha, "commit")
        raw = self._run("cat-file", "-p", sha)
        headers, _, message = raw.partition("\n\n")
        tree_sha: str | None = None
        parents: list[str] = []
        for line in headers.splitlines():
            if line.startswith("tree "):
                tree_sha = line[5:]
            elif line.startswith("parent "):
                parents.append(line[7:])
        if tree_sha is None:
            raise GitStorageError(f"commit {sha!r} has no tree")
        return {
            "sha": sha,
            "tree_sha": tree_sha,
            "parents": tuple(parents),
            "message": message,
        }

    def merge_base(self, left_sha: str, right_sha: str) -> str:
        """Best common ancestor used for three-way merge/conflict checks."""
        self.require_object(left_sha, "commit")
        self.require_object(right_sha, "commit")
        result = self._run_process(
            "merge-base", left_sha, right_sha, check=False
        )
        if result.returncode != 0 or not result.stdout.strip():
            raise GitStorageError(
                f"commits {left_sha!r} and {right_sha!r} have no merge base"
            )
        return result.stdout.splitlines()[0].strip()

    def diff(
        self, base_sha: str, head_sha: str, *, max_bytes: int = 512_000
    ) -> dict[str, object]:
        """Return a bounded unified diff between two real Git commits."""
        self.require_object(base_sha, "commit")
        self.require_object(head_sha, "commit")
        if max_bytes <= 0:
            raise GitStorageError("diff max_bytes must be positive")
        patch = self._run(
            "diff", "--no-ext-diff", "--unified=3", base_sha, head_sha, "--"
        )
        encoded = patch.encode("utf-8")
        if len(encoded) <= max_bytes:
            return {"patch": patch, "truncated": False}
        bounded = encoded[:max_bytes].decode("utf-8", errors="ignore")
        return {"patch": bounded, "truncated": True}

    def read_file(self, ref: str, path: str) -> bytes:
        """Read one file from a commit or branch without a working tree."""
        self.initialize()
        file_path = self._validate_file_path(path)
        environment = os.environ.copy()
        environment["GIT_DIR"] = str(self.path)
        result = subprocess.run(
            ("git", "show", f"{ref}:{file_path}"), stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, env=environment, check=False,
        )
        if result.returncode != 0:
            raise GitStorageError(
                result.stderr.decode("utf-8", errors="replace").strip()
                or "Git command failed"
            )
        return result.stdout

    def read_tree_contents(self, ref: str) -> dict[str, bytes]:
        """The whole tree at a ref as {path: bytes}.

        A caller that Merkle-hashes a tree needs content, not entry metadata,
        and one `git show` per blob costs a process per file.
        """
        self.initialize()
        entries = [e for e in self.list_tree(ref, recursive=True)
                   if e["type"] == "blob"]
        return {
            entry["path"]: self._read_object(entry["id"], "blob")
            for entry in entries
        }

    def log(
        self, *, to_ref: str, from_ref: str | None = None, merges_only: bool = False
    ) -> list[str]:
        """Commit shas reachable from to_ref, excluding from_ref, newest first."""
        self.initialize()
        arguments = ["rev-list", "--first-parent"]
        if merges_only:
            arguments.append("--merges")
        arguments.append(f"{from_ref}..{to_ref}" if from_ref else to_ref)
        return [line for line in self._run(*arguments).splitlines() if line]

    def update_tag(self, name: str, sha: str) -> None:
        """Write refs/tags/<name>. A tag that is only a SQL row is invisible
        to every git-level read, including the snapshot exporter."""
        self.require_object(sha, "commit")
        self._run("update-ref", f"refs/tags/{name}", sha)

    def create_tag(self, name: str, sha: str) -> None:
        self.require_object(sha, "commit")
        if self.resolve_tag(name) is not None:
            raise GitStorageError(f"tag {name!r} already exists")
        self._run("update-ref", f"refs/tags/{name}", sha, "0" * 40)

    def resolve_tag(self, name: str) -> str | None:
        result = self._run_process(
            "rev-parse", "--verify", f"refs/tags/{name}", check=False
        )
        if result.returncode != 0:
            return None
        return result.stdout.strip() or None

    def export_objects(self) -> list[tuple[str, str, bytes]]:
        """Every object in the repository as (sha, type, content), sorted."""
        self.initialize()
        listing = self._run("cat-file", "--batch-all-objects", "--batch-check")
        objects: list[tuple[str, str, bytes]] = []
        for line in sorted(listing.splitlines()):
            if not line.strip():
                continue
            sha, object_type, _ = line.split(" ", 2)
            objects.append((sha, object_type, self._read_object(sha, object_type)))
        return objects

    def import_objects(self, objects: Sequence[tuple[str, str, bytes]]) -> None:
        """Write raw objects back. Content-addressed, so order does not matter."""
        self.initialize()
        environment = os.environ.copy()
        environment["GIT_DIR"] = str(self.path)
        for sha, object_type, content in objects:
            result = subprocess.run(
                ("git", "hash-object", "-t", object_type, "-w", "--stdin",
                 "--literally"),
                input=content, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                env=environment, check=False,
            )
            if result.returncode != 0:
                raise GitStorageError(
                    result.stderr.decode("utf-8", errors="replace").strip()
                    or "Git object import failed"
                )
            written = result.stdout.decode().strip()
            if written != sha:
                raise GitStorageError(
                    f"imported object hashed to {written}, expected {sha}"
                )

    def resolve_ref(self, name: str) -> str | None:
        """Full ref name (refs/heads/x, refs/tags/y) to sha, or None."""
        result = self._run_process("rev-parse", "--verify", name, check=False)
        if result.returncode != 0:
            return None
        return result.stdout.strip() or None

    def delete_ref(self, name: str) -> None:
        self._run("update-ref", "-d", name)

    def all_refs(self) -> dict[str, str]:
        self.initialize()
        raw = self._run("for-each-ref", "--format=%(refname)%09%(objectname)")
        refs: dict[str, str] = {}
        for line in raw.splitlines():
            name, _, sha = line.partition("\t")
            if name:
                refs[name] = sha
        return refs

    def set_ref(self, name: str, sha: str) -> None:
        self.initialize()
        self._run("update-ref", name, sha)

    def _read_object(self, sha: str, object_type: str) -> bytes:
        environment = os.environ.copy()
        environment["GIT_DIR"] = str(self.path)
        result = subprocess.run(
            ("git", "cat-file", object_type, sha), stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, env=environment, check=False,
        )
        if result.returncode != 0:
            raise GitStorageError(
                result.stderr.decode("utf-8", errors="replace").strip()
                or "Git object read failed"
            )
        return result.stdout

    def list_tree(
        self, ref: str, *, path: str | None = None, recursive: bool = False
    ) -> list[dict[str, str]]:
        """List a Git tree in the shape needed by repository APIs."""
        self.initialize()
        arguments = ["ls-tree"]
        if recursive:
            arguments.append("-r")
        arguments.append(ref)
        if path:
            arguments.extend(("--", self._validate_file_path(path)))
        raw = self._run(*arguments)
        entries: list[dict[str, str]] = []
        for line in raw.splitlines():
            metadata, _, item_path = line.partition("\t")
            mode, object_type, object_id = metadata.split(" ", 2)
            entries.append({"id": object_id, "name": item_path.rsplit("/", 1)[-1], "type": "tree" if object_type == "tree" else "blob", "path": item_path, "mode": mode})
        return entries

    def _run(
        self,
        *arguments: str,
        git_dir: bool = True,
        extra_env: Mapping[str, str] | None = None,
    ) -> str:
        result = self._run_process(
            *arguments, git_dir=git_dir, extra_env=extra_env, check=False
        )
        if result.returncode != 0:
            self._raise_process_error(result)
        return result.stdout.strip()

    def _run_bytes(
        self,
        *arguments: str,
        data: bytes,
        extra_env: Mapping[str, str] | None = None,
    ) -> str:
        environment = os.environ.copy()
        environment["GIT_DIR"] = str(self.path)
        if extra_env:
            environment.update(extra_env)
        result = subprocess.run(
            ("git", *arguments),
            input=data,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
            check=False,
        )
        if result.returncode != 0:
            raise GitStorageError(
                result.stderr.decode("utf-8", errors="replace").strip()
                or "Git command failed"
            )
        return result.stdout.decode().strip()

    def _run_process(
        self,
        *arguments: str,
        git_dir: bool = True,
        extra_env: Mapping[str, str] | None = None,
        check: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        if git_dir:
            environment["GIT_DIR"] = str(self.path)
        if extra_env:
            environment.update(extra_env)
        return subprocess.run(
            ("git", *arguments),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
            check=check,
        )

    @staticmethod
    def _raise_process_error(result: subprocess.CompletedProcess[str]) -> None:
        raise GitStorageError(result.stderr.strip() or "Git command failed")

    @staticmethod
    def _validate_file_path(value: str) -> str:
        path = PurePosixPath(value)
        if not value or path.is_absolute() or ".." in path.parts or value.endswith("/"):
            raise GitStorageError(f"invalid repository path: {value!r}")
        return str(path)

    @staticmethod
    def _git_date(value: datetime) -> str:
        utc = value.astimezone(timezone.utc)
        return f"@{int(utc.timestamp())} +0000"


class GitServiceDataPlane:
    """All bare repositories owned by one isolated service instance."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def repository(self, repository_id: int) -> BareGitRepository:
        if not isinstance(repository_id, int) or repository_id <= 0:
            raise GitStorageError("repository id must be a positive integer")
        return BareGitRepository(self.root / f"{repository_id}.git")

    def transaction(self) -> "GitServiceDataPlaneTransaction":
        return GitServiceDataPlaneTransaction(self)


class TransactionalBareGitRepository:
    """Bare repository proxy that journals mutable refs for rollback."""

    def __init__(
        self, transaction: "GitServiceDataPlaneTransaction", repository_id: int
    ) -> None:
        self.transaction = transaction
        self.repository_id = repository_id
        self.repository = transaction.data_plane.repository(repository_id)

    def initialize(self) -> None:
        self.repository.initialize()

    def create_commit(self, **kwargs: object) -> dict[str, object]:
        return self.repository.create_commit(**kwargs)  # type: ignore[arg-type]

    def commit_metadata(self, sha: str) -> dict[str, object]:
        return self.repository.commit_metadata(sha)

    def merge_base(self, left_sha: str, right_sha: str) -> str:
        return self.repository.merge_base(left_sha, right_sha)

    def diff(
        self, base_sha: str, head_sha: str, *, max_bytes: int = 512_000
    ) -> dict[str, object]:
        return self.repository.diff(base_sha, head_sha, max_bytes=max_bytes)

    def read_file(self, ref: str, path: str) -> bytes:
        return self.repository.read_file(ref, path)

    def list_tree(
        self, ref: str, *, path: str | None = None, recursive: bool = False
    ) -> list[dict[str, str]]:
        return self.repository.list_tree(ref, path=path, recursive=recursive)

    def update_branch(
        self, name: str, sha: str, *, expected_old_sha: str | None = None
    ) -> None:
        self.transaction.remember_ref(
            self.repository_id, f"refs/heads/{name}", self.repository
        )
        self.repository.update_branch(
            name, sha, expected_old_sha=expected_old_sha
        )

    def is_ancestor(self, ancestor_sha: str, descendant_sha: str) -> bool:
        return self.repository.is_ancestor(ancestor_sha, descendant_sha)

    def delete_branch(self, name: str) -> None:
        self.transaction.remember_ref(
            self.repository_id, f"refs/heads/{name}", self.repository
        )
        self.repository.delete_branch(name)

    def update_tag(self, name: str, sha: str) -> None:
        self.transaction.remember_ref(
            self.repository_id, f"refs/tags/{name}", self.repository
        )
        self.repository.update_tag(name, sha)

    def create_tag(self, name: str, sha: str) -> None:
        self.transaction.remember_ref(
            self.repository_id, f"refs/tags/{name}", self.repository
        )
        self.repository.create_tag(name, sha)

    def resolve_tag(self, name: str) -> str | None:
        return self.repository.resolve_tag(name)

    def read_tree_contents(self, ref: str) -> dict[str, bytes]:
        return self.repository.read_tree_contents(ref)

    def log(
        self, *, to_ref: str, from_ref: str | None = None, merges_only: bool = False
    ) -> list[str]:
        return self.repository.log(
            to_ref=to_ref, from_ref=from_ref, merges_only=merges_only
        )


class GitServiceDataPlaneTransaction:
    """Compensates Git ref changes if the relational transaction rolls back."""

    def __init__(self, data_plane: GitServiceDataPlane) -> None:
        self.data_plane = data_plane
        self.original_refs: dict[tuple[int, str], str | None] = {}
        self.finished = False

    def repository(self, repository_id: int) -> TransactionalBareGitRepository:
        if self.finished:
            raise GitStorageError("Git data-plane transaction is already closed")
        return TransactionalBareGitRepository(self, repository_id)

    def remember_ref(
        self, repository_id: int, name: str, repository: BareGitRepository
    ) -> None:
        """`name` is a full ref (refs/heads/x, refs/tags/y)."""
        key = (repository_id, name)
        if key not in self.original_refs:
            self.original_refs[key] = repository.resolve_ref(name)

    def commit(self) -> None:
        self.original_refs.clear()
        self.finished = True

    def rollback(self) -> None:
        if self.finished:
            return
        for (repository_id, name), original_sha in reversed(
            tuple(self.original_refs.items())
        ):
            repository = self.data_plane.repository(repository_id)
            if original_sha is None:
                repository.delete_ref(name)
            else:
                repository.set_ref(name, original_sha)
        self.original_refs.clear()
        self.finished = True


class GitDataPlaneStorage:
    """Filesystem lifecycle for template and environment Git service state."""

    def __init__(self, data_root: Path) -> None:
        self.data_root = data_root.resolve()
        self.environments_root = self.data_root / "git" / "environments"
        self.templates_root = self.data_root / "git" / "templates"
        self.snapshots_root = self.data_root / "git" / "snapshots"
        self.environments_root.mkdir(parents=True, exist_ok=True)
        self.templates_root.mkdir(parents=True, exist_ok=True)
        self.snapshots_root.mkdir(parents=True, exist_ok=True)

    def build_locator(self, environment_id: str, instance_id: str) -> str:
        return str(Path("git") / "environments" / environment_id / instance_id)

    def build_template_locator(self, template_id: str, instance_id: str) -> str:
        return str(Path("git") / "templates" / template_id / instance_id)

    def build_snapshot_locator(self, snapshot_id: str, instance_id: str) -> str:
        return str(Path("git") / "snapshots" / snapshot_id / instance_id)

    def provision(self, locator: str) -> None:
        path = self._resolve(locator)
        if path.exists():
            raise GitStorageError(f"Git data-plane target already exists: {locator}")
        path.mkdir(parents=True)

    def exists(self, locator: str) -> bool:
        return self._resolve(locator).is_dir()

    def clone(self, source_locator: str, target_locator: str) -> None:
        source = self._resolve(source_locator)
        target = self._resolve(target_locator)
        if not source.is_dir():
            raise GitStorageError(f"Git data-plane source does not exist: {source_locator}")
        if target.exists():
            raise GitStorageError(f"Git data-plane target already exists: {target_locator}")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source, target, copy_function=shutil.copy2)

    def open(self, locator: str) -> GitServiceDataPlane:
        path = self._resolve(locator)
        if not path.is_dir():
            raise GitStorageError(f"Git data-plane does not exist: {locator}")
        return GitServiceDataPlane(path)

    def delete(self, locator: str) -> None:
        """Remove the data plane. A missing one is not an error."""
        path = self._resolve(locator)
        if path.is_dir():
            shutil.rmtree(path)

    def repository_ids(self, locator: str) -> list[int]:
        root = self._resolve(locator)
        if not root.is_dir():
            return []
        return sorted(
            int(item.stem) for item in root.glob("*.git") if item.stem.isdigit()
        )

    def inspect(self, locator: str) -> Mapping[str, object]:
        root = self._resolve(locator)
        if not root.is_dir():
            raise GitStorageError(f"Git data-plane does not exist: {locator}")
        repositories: dict[str, object] = {}
        for repository_path in sorted(root.glob("*.git"), key=lambda item: item.name):
            result = subprocess.run(("git", "--git-dir", str(repository_path), "for-each-ref", "--format=%(refname)%09%(objectname)"), text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
            if result.returncode != 0:
                raise GitStorageError(result.stderr.strip() or "Git ref inspection failed")
            refs = {}
            for line in result.stdout.splitlines():
                name, _, sha = line.partition("\t")
                refs[name] = sha
            repositories[repository_path.stem] = {"refs": refs}
        return repositories

    def _resolve(self, locator: str) -> Path:
        path = (self.data_root / locator).resolve()
        if not (
            path.is_relative_to(self.environments_root)
            or path.is_relative_to(self.templates_root)
            or path.is_relative_to(self.snapshots_root)
        ):
            raise GitStorageError("Git data-plane locator escapes the data root")
        return path
