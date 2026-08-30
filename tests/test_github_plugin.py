from __future__ import annotations

import base64
import os
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

import psycopg
from psycopg import sql

from mcp_system import MCPSystem, PluginRegistry
from mcp_system.config import load_template_spec
from mcp_system.errors import ConfigurationError
from mcp_system.mcp import MCPDispatcher
from mcp_system.service_plugins import GitHubPlugin
from mcp_system.service_plugins.github import (
    GitHubConflict,
    GitHubNotFound,
    GitHubForbidden,
    GitHubValidationError,
)
from mcp_system.service_plugins.github.inspector import GitHubInspectorAdapter
from mcp_system.storage import PostgresControlPlane, PostgresServiceStorage


CONFIG = Path("configs/templates/github-default.toml")


def github_registry() -> PluginRegistry:
    registry = PluginRegistry()
    registry.register(GitHubPlugin())
    return registry


def exercise_issue_operations(
    case: unittest.TestCase, system: MCPSystem, environment_id: str
) -> None:
    fixed_now = lambda: datetime(2026, 2, 1, 12, 0, tzinfo=timezone.utc)
    plugin = GitHubPlugin()
    with system.open_service_database(environment_id, "github") as session:
        engineer = plugin.create_operations(
            session, actor="engineer", now=fixed_now
        )
        case.assertEqual(engineer.get_authenticated_user()["login"], "engineer")
        case.assertEqual(engineer.get_repository("acme", "product")["id"], 1)

        custom_label = engineer.create_label(
            "acme",
            "product",
            name="priority:high",
            color="#b60205",
            description="Urgent work",
        )
        case.assertEqual(custom_label["color"], "b60205")

        first = engineer.create_issue(
            "acme",
            "product",
            title="Fix authentication",
            body="Login fails for valid credentials",
            labels=("bug", "priority:high"),
            assignees=("qa",),
        )
        second = engineer.create_issue(
            "acme", "product", title="Improve diagnostics"
        )
        case.assertEqual((first["number"], second["number"]), (1, 2))
        case.assertEqual([label["name"] for label in first["labels"]], ["bug", "priority:high"])
        case.assertEqual([user["login"] for user in first["assignees"]], ["qa"])

        comment = engineer.create_comment(
            "acme", "product", 1, body="I can reproduce this locally."
        )
        case.assertEqual(comment["user"]["login"], "engineer")
        closed = engineer.update_issue(
            "acme",
            "product",
            1,
            state="closed",
            state_reason="completed",
        )
        case.assertEqual(closed["state"], "closed")
        case.assertEqual(closed["comments"], 1)

        engineer.remove_assignees("acme", "product", 1, ("qa",))
        engineer.set_issue_labels("acme", "product", 1, ("enhancement",))
        final_issue = engineer.get_issue("acme", "product", 1)
        case.assertEqual(final_issue["assignees"], [])
        case.assertEqual([label["name"] for label in final_issue["labels"]], ["enhancement"])
        case.assertEqual(len(engineer.list_issues("acme", "product", state="all")), 2)

        with case.assertRaises(GitHubForbidden):
            engineer.update_repository("acme", "product", archived=True)

        director = plugin.create_operations(
            session, actor="director", now=fixed_now
        )
        created_repository = director.create_repository(
            "acme", name="platform", private=True
        )
        case.assertEqual(created_repository["full_name"], "acme/platform")

    with system.open_service_database(environment_id, "github") as session:
        persisted = plugin.create_operations(
            session, actor="lead", now=fixed_now
        ).get_issue("acme", "product", 1)
        case.assertEqual(persisted["state"], "closed")
        case.assertEqual(persisted["comments"], 1)


def exercise_pull_request_operations(
    case: unittest.TestCase, system: MCPSystem, environment_id: str
) -> dict[str, str]:
    fixed_now = lambda: datetime(2026, 2, 2, 12, 0, tzinfo=timezone.utc)
    with system.open_service_operations(
        environment_id, "github", actor="engineer", now=fixed_now
    ) as engineer:
        root = engineer.create_commit(
            "acme",
            "product",
            message="Initial commit",
            author="engineer",
            files={"README.md": "# Product\n"},
        )
        root_sha = root["sha"]
        engineer.create_branch(
            "acme", "product", name="main", head_sha=root_sha, protected=True
        )
        feature = engineer.create_commit(
            "acme",
            "product",
            message="Implement authentication",
            author="engineer",
            parent_shas=(root_sha,),
            files={"auth.py": "def authenticate():\n    return True\n"},
        )
        feature_sha = feature["sha"]
        engineer.create_branch(
            "acme", "product", name="feature/auth", head_sha=feature_sha
        )

        issue = engineer.create_issue(
            "acme", "product", title="Track authentication work"
        )
        pull_request = engineer.create_pull_request(
            "acme",
            "product",
            title="Implement authentication",
            head="feature/auth",
            base="main",
            body="Closes #1",
        )
        case.assertEqual((issue["number"], pull_request["number"]), (1, 2))
        case.assertEqual(pull_request["head"]["sha"], feature_sha)
        case.assertEqual(pull_request["base"]["sha"], root_sha)
        case.assertNotIn("state_reason", pull_request)
        case.assertNotIn("repository_url", pull_request)
        case.assertNotIn("pull_request", pull_request)

        requested = engineer.request_reviewers(
            "acme", "product", 2, reviewers=("qa",)
        )
        case.assertEqual(
            [reviewer["login"] for reviewer in requested["requested_reviewers"]],
            ["qa"],
        )
        with case.assertRaises(GitHubValidationError):
            engineer.create_review(
                "acme", "product", 2, event="APPROVE", body="self approval"
            )

    with system.open_service_operations(
        environment_id, "github", actor="qa", now=fixed_now
    ) as qa:
        review = qa.create_review(
            "acme", "product", 2, event="APPROVE", body="Looks good"
        )
        case.assertEqual(review["state"], "APPROVED")
        case.assertEqual(review["commit_id"], feature_sha)
        case.assertEqual(qa.list_requested_reviewers("acme", "product", 2)["users"], [])
        comment = qa.create_review_comment(
            "acme",
            "product",
            2,
            review_id=review["id"],
            body="Please keep this API explicit.",
            path="auth.py",
            line=1,
            side="RIGHT",
        )
        case.assertEqual(comment["user"]["login"], "qa")
        case.assertEqual(comment["commit_id"], feature_sha)
        case.assertEqual(len(qa.list_review_comments("acme", "product", 2)), 1)

    with system.open_service_operations(
        environment_id, "github", actor="engineer", now=fixed_now
    ) as engineer:
        merge_commit = engineer.create_commit(
            "acme",
            "product",
            message="Merge feature/auth",
            author="engineer",
            parent_shas=(root_sha, feature_sha),
        )
        merge_sha = merge_commit["sha"]
        merged = engineer.merge_pull_request(
            "acme", "product", 2, merge_commit_sha=merge_sha
        )
        case.assertTrue(merged["merged"])
        case.assertEqual(merged["sha"], merge_sha)
        case.assertTrue(engineer.is_merged("acme", "product", 2))
        branches = {
            branch["name"]: branch["commit"]["sha"]
            for branch in engineer.list_branches("acme", "product")
        }
        case.assertEqual(branches["main"], merge_sha)
        with case.assertRaises(GitHubConflict):
            engineer.update_pull_request("acme", "product", 2, state="open")

        issues = engineer.list_issues("acme", "product", state="all")
        case.assertEqual(len(issues), 2)
        case.assertIn("pull_request", issues[0])

    with system.open_service_operations(
        environment_id, "github", actor="lead", now=fixed_now
    ) as lead:
        persisted = lead.get_pull_request("acme", "product", 2)
        case.assertTrue(persisted["merged"])
        case.assertEqual(persisted["state"], "closed")
        case.assertEqual(persisted["merge_commit_sha"], merge_sha)
        case.assertEqual(len(lead.list_review_comments("acme", "product", 2)), 1)

    bare_repository = system.open_git_data_plane(
        environment_id, "github"
    ).repository(1)
    case.assertEqual(bare_repository.object_type(merge_sha), "commit")
    case.assertEqual(bare_repository.resolve_branch("main"), merge_sha)
    return {"root": root_sha, "feature": feature_sha, "merge": merge_sha}


def exercise_git_ref_rollback(
    case: unittest.TestCase, system: MCPSystem, environment_id: str
) -> None:
    fixed_now = lambda: datetime(2026, 2, 3, 12, 0, tzinfo=timezone.utc)
    commit_sha: str | None = None
    with case.assertRaisesRegex(RuntimeError, "abort operation"):
        with system.open_service_operations(
            environment_id, "github", actor="engineer", now=fixed_now
        ) as engineer:
            commit = engineer.create_commit(
                "acme",
                "product",
                message="Rolled back commit",
                author="engineer",
                files={"rollback.txt": "unreachable after rollback\n"},
            )
            commit_sha = commit["sha"]
            engineer.create_branch(
                "acme", "product", name="rollback-test", head_sha=commit_sha
            )
            raise RuntimeError("abort operation")

    case.assertIsNotNone(commit_sha)
    repository = system.open_git_data_plane(environment_id, "github").repository(1)
    case.assertIsNone(repository.resolve_branch("rollback-test"))
    # Immutable objects may remain unreachable and can be collected by Git GC.
    case.assertEqual(repository.object_type(commit_sha), "commit")
    with system.open_service_database(environment_id, "github") as session:
        case.assertEqual(
            session.execute("SELECT count(*) AS count FROM github_commits").fetchone()[
                "count"
            ],
            0,
        )


def exercise_inspector_projection(
    case: unittest.TestCase, system: MCPSystem, environment_id: str
) -> None:
    with system.open_service_operations(
        environment_id, "github", actor="engineer"
    ) as operations:
        operations.create_issue(
            "acme", "product", title="Universal projection", labels=("bug",)
        )
    with system.open_service_database(environment_id, "github") as session:
        projection = GitHubInspectorAdapter().project(
            session, system.open_git_data_plane(environment_id, "github")
        )
    repository = projection["repositories"][0]
    case.assertEqual(repository["fullName"], "acme/product")
    case.assertEqual(repository["tickets"][0]["title"], "Universal projection")
    case.assertEqual(repository["tickets"][0]["labels"], ["bug"])


class GitHubPluginSQLiteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.system = MCPSystem(Path(self.temp_dir.name), github_registry())

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_real_schema_bootstrap_and_clone(self) -> None:
        template = self.system.create_template(load_template_spec(CONFIG))
        environment = self.system.create_environment_from_template(template.id)

        with self.system.open_service_database(environment.id, "github") as session:
            tables = session.execute(
                """
                SELECT name FROM sqlite_master
                 WHERE type = 'table' AND name LIKE 'github_%'
                 ORDER BY name
                """
            ).fetchall()
            users = session.execute(
                "SELECT login, user_type FROM github_users ORDER BY id"
            ).fetchall()
            repository = session.execute(
                "SELECT full_name, default_branch FROM github_repositories"
            ).fetchone()
            label_count = session.execute(
                "SELECT count(*) AS count FROM github_labels"
            ).fetchone()["count"]

        self.assertEqual(len(tables), 21)
        self.assertEqual(
            [(row["login"], row["user_type"]) for row in users],
            [
                ("director", "Bot"),
                ("lead", "Bot"),
                ("engineer", "Bot"),
                ("qa", "Bot"),
            ],
        )
        self.assertEqual(repository["full_name"], "acme/product")
        self.assertEqual(repository["default_branch"], "main")
        self.assertEqual(label_count, 9)

    def test_actions_run_jobs_logs_and_inspector_projection(self) -> None:
        template = self.system.create_template(load_template_spec(CONFIG))
        environment = self.system.create_environment_from_template(template.id)
        with self.system.open_service_operations(environment.id, "github", actor="engineer") as operations:
            commit = operations.create_commit("acme", "product", message="CI base", author="engineer", files={"README.md": "ci\n"})
            operations.create_branch("acme", "product", name="main", head_sha=commit["sha"])
            run = operations.create_workflow_run("acme", "product", name="CI", event="push", head_branch="main")
            job = operations.create_workflow_job("acme", "product", run["id"], name="test", status="in_progress")
            completed = operations.update_workflow_job("acme", "product", job["id"], status="completed", conclusion="success", log="1 passed\n")
            operations.update_workflow_run("acme", "product", run["id"], status="completed", conclusion="success")
            self.assertEqual(completed["conclusion"], "success")
            self.assertEqual(operations.list_workflow_runs("acme", "product")["total_count"], 1)
            self.assertEqual(operations.list_workflow_jobs("acme", "product", run["id"])["jobs"][0]["id"], job["id"])
            self.assertEqual(operations.get_workflow_job_log("acme", "product", job["id"])["log"], "1 passed\n")
            release = operations.create_release("acme", "product", tag_name="v0.1.0", name="First", body="Ready")
            self.assertEqual(release["target_commitish"], "main")
            self.assertEqual(operations.list_releases("acme", "product")[0]["tag_name"], "v0.1.0")
            self.assertEqual(operations.get_ref("acme", "product", "tags/v0.1.0")["object"]["sha"], commit["sha"])
            self.assertEqual(self.system.open_git_data_plane(environment.id, "github").repository(1).resolve_tag("v0.1.0"), commit["sha"])
        with self.system.open_service_database(environment.id, "github") as session:
            projection = GitHubInspectorAdapter().project(session, self.system.open_git_data_plane(environment.id, "github"))
        self.assertEqual(projection["repositories"][0]["builds"][0]["conclusion"], "success")

    def test_update_release_mirrors_the_rest_patch_endpoint(self) -> None:
        template = self.system.create_template(load_template_spec(CONFIG))
        environment = self.system.create_environment_from_template(template.id)
        with self.system.open_service_operations(environment.id, "github", actor="engineer") as operations:
            commit = operations.create_commit("acme", "product", message="base", author="engineer", files={"README.md": "x\n"})
            operations.create_branch("acme", "product", name="main", head_sha=commit["sha"])
            draft = operations.create_release("acme", "product", tag_name="v0.1.0", name="", body="", draft=True)
            self.assertIsNone(draft["published_at"])
            # partial update: only body changes; name/prerelease untouched
            updated = operations.update_release("acme", "product", release_id=draft["id"], body="## Notes\n- shipped X")
            self.assertEqual(updated["body"], "## Notes\n- shipped X")
            self.assertEqual(updated["name"], "")
            self.assertIsNone(updated["published_at"])
            # publishing the draft stamps published_at
            published = operations.update_release("acme", "product", release_id=draft["id"], draft=False)
            self.assertFalse(published["draft"])
            self.assertIsNotNone(published["published_at"])
            # unknown release id is a 404
            with self.assertRaises(GitHubNotFound):
                operations.update_release("acme", "product", release_id=999999, body="x")
            # invalid make_latest is rejected
            with self.assertRaises(GitHubValidationError):
                operations.update_release("acme", "product", release_id=draft["id"], make_latest="maybe")

    def _content_fixture(self, *, files: dict[str, object] | None = None) -> tuple[str, str]:
        """Environment with one commit on `main` and a `v1.0` tag on it."""
        template = self.system.create_template(load_template_spec(CONFIG))
        environment = self.system.create_environment_from_template(template.id)
        payload = files if files is not None else {
            "README.md": "# Product\n",
            "src/app.py": "print('hello')\n",
            "src/lib/util.py": "VALUE = 1\n",
            "assets/logo.png": b"\x89PNG\r\n\x1a\n\x00\x00\x00\x0d",
        }
        with self.system.open_service_operations(environment.id, "github", actor="engineer") as operations:
            commit = operations.create_commit("acme", "product", message="base", author="engineer", files=payload)
            operations.create_branch("acme", "product", name="main", head_sha=commit["sha"])
            operations.create_ref("acme", "product", ref="refs/tags/v1.0", sha=commit["sha"])
        return environment.id, commit["sha"]

    def test_get_file_contents_returns_official_resource_shapes(self) -> None:
        environment_id, _ = self._content_fixture()
        with self.system.open_service_operations(environment_id, "github", actor="engineer") as operations:
            text = operations.get_file_contents("acme", "product", path="README.md")
            self.assertEqual(text["uri"], "repo://acme/product/refs/heads/main/contents/README.md")
            self.assertTrue(text["mimeType"].startswith("text/"), text["mimeType"])
            self.assertEqual(text["text"], "# Product\n")
            self.assertNotIn("blob", text)

            source = operations.get_file_contents("acme", "product", path="src/app.py")
            self.assertEqual(source["mimeType"], "text/x-python")

            binary = operations.get_file_contents("acme", "product", path="assets/logo.png")
            self.assertEqual(binary["mimeType"], "image/png")
            self.assertEqual(base64.b64decode(binary["blob"]), b"\x89PNG\r\n\x1a\n\x00\x00\x00\x0d")
            self.assertNotIn("text", binary)

            # a directory is an array of Contents entries, `fields` narrows them
            root = operations.get_file_contents("acme", "product")
            self.assertEqual([(entry["name"], entry["type"]) for entry in root], [("README.md", "file"), ("assets", "dir"), ("src", "dir")])
            directory = operations.get_file_contents("acme", "product", path="src", fields=["name", "type", "path"])
            self.assertEqual(directory, [
                {"name": "app.py", "type": "file", "path": "src/app.py"},
                {"name": "lib", "type": "dir", "path": "src/lib"},
            ])

    def test_get_file_contents_handles_empty_and_oversized_files(self) -> None:
        environment_id, _ = self._content_fixture(files={"empty.txt": "", "big.bin": "x" * 1_200_000})
        with self.system.open_service_operations(environment_id, "github", actor="engineer") as operations:
            empty = operations.get_file_contents("acme", "product", path="empty.txt")
            self.assertEqual((empty["mimeType"], empty["text"]), ("text/plain", ""))

            # a file of 1 MB or more becomes a resource link, never inline content
            oversized = operations.get_file_contents("acme", "product", path="big.bin")
            self.assertEqual(set(oversized), {"uri", "download_url"})
            self.assertEqual(oversized["download_url"], "https://raw.githubusercontent.com/acme/product/main/big.bin")

            rest = operations.get_content("acme", "product", "big.bin")
            self.assertEqual((rest["encoding"], rest["content"], rest["size"]), ("none", "", 1_200_000))

    def test_get_file_contents_detects_text_without_a_known_extension(self) -> None:
        environment_id, _ = self._content_fixture(files={"LICENSE": "MIT\n", "payload": b"\x00\x01\x02"})
        with self.system.open_service_operations(environment_id, "github", actor="engineer") as operations:
            # no extension to guess from: utf-8 decodes, so it is text
            license_file = operations.get_file_contents("acme", "product", path="LICENSE")
            self.assertEqual((license_file["mimeType"], license_file["text"]), ("text/plain", "MIT\n"))

            payload = operations.get_file_contents("acme", "product", path="payload")
            self.assertEqual(payload["mimeType"], "application/octet-stream")
            self.assertEqual(base64.b64decode(payload["blob"]), b"\x00\x01\x02")

    def test_get_file_contents_resolves_every_reference_form(self) -> None:
        environment_id, commit_sha = self._content_fixture()
        with self.system.open_service_operations(environment_id, "github", actor="engineer") as operations:
            second = operations.create_commit("acme", "product", message="next", author="engineer", parent_shas=(commit_sha,), files={"README.md": "# Product v2\n"})
            operations.update_ref("acme", "product", "refs/heads/main", sha=second["sha"])

            for ref in ("main", "refs/heads/main", "heads/main", None):
                with self.subTest(ref=ref):
                    result = operations.get_file_contents("acme", "product", path="README.md", ref=ref)
                    self.assertEqual(result["text"], "# Product v2\n")

            for ref in ("v1.0", "refs/tags/v1.0", "tags/v1.0"):
                with self.subTest(ref=ref):
                    result = operations.get_file_contents("acme", "product", path="README.md", ref=ref)
                    self.assertEqual(result["text"], "# Product\n")
                    self.assertEqual(result["uri"], "repo://acme/product/refs/tags/v1.0/contents/README.md")

            by_sha = operations.get_file_contents("acme", "product", path="README.md", ref=commit_sha)
            self.assertEqual(by_sha["uri"], f"repo://acme/product/{commit_sha}/contents/README.md")
            self.assertEqual(by_sha["text"], "# Product\n")

            # `sha` wins over `ref`, exactly as the official tool documents
            precedence = operations.get_file_contents("acme", "product", path="README.md", ref="main", sha=commit_sha)
            self.assertEqual(precedence["text"], "# Product\n")

            with self.assertRaises(GitHubNotFound) as raised:
                operations.get_file_contents("acme", "product", path="README.md", ref="release-42")
            self.assertIn("could not resolve ref 'release-42' as a branch or a tag", str(raised.exception))

    def test_get_file_contents_falls_back_from_main_to_the_default_branch(self) -> None:
        template = self.system.create_template(load_template_spec(CONFIG))
        environment_id = self.system.create_environment_from_template(template.id).id
        with self.system.open_service_operations(environment_id, "github", actor="engineer") as operations:
            commit = operations.create_commit("acme", "product", message="base", author="engineer", files={"README.md": "# Product\n", "src/app.py": "print('hello')\n", "src/lib/util.py": "VALUE = 1\n"})
            operations.create_branch("acme", "product", name="develop", head_sha=commit["sha"])
        with self.system.open_service_operations(environment_id, "github", actor="lead") as operations:
            operations.update_repository("acme", "product", default_branch="develop")
        with self.system.open_service_operations(environment_id, "github", actor="engineer") as operations:
            resolved = operations.get_file_contents("acme", "product", path="README.md", ref="main")
            self.assertEqual(resolved["uri"], "repo://acme/product/refs/heads/develop/contents/README.md")
            self.assertIn("doesn't exist, default branch 'develop' was used instead", resolved["note"])

            # the same fallback applies to a fully qualified refs/heads/main
            qualified = operations.get_file_contents("acme", "product", path="README.md", ref="refs/heads/main")
            self.assertIn("doesn't exist, default branch 'develop' was used instead", qualified["note"])

            # a directory result carries the note in an envelope, entries intact
            listing = operations.get_file_contents("acme", "product", path="src", ref="main", fields=["name"])
            self.assertIn("default branch 'develop' was used instead", listing["note"])
            self.assertEqual(listing["contents"], [{"name": "app.py"}, {"name": "lib"}])

    def test_get_file_contents_suggests_tree_matches_before_failing(self) -> None:
        environment_id, _ = self._content_fixture()
        with self.system.open_service_operations(environment_id, "github", actor="engineer") as operations:
            suggested = operations.get_file_contents("acme", "product", path="util.py")
            self.assertEqual(suggested["matching_files"], ["src/lib/util.py"])
            self.assertEqual(suggested["resolved_refs"], ["refs/heads/main"])
            self.assertIn("Resolved potential matches in the repository tree", suggested["note"])
            self.assertIn("matching files: src/lib/util.py", suggested["note"])

            # a directory suggestion keeps the trailing slash of the official tool
            directory = operations.get_file_contents("acme", "product", path="lib")
            self.assertEqual(directory["matching_files"], ["src/lib/"])

            with self.assertRaises(GitHubNotFound) as raised:
                operations.get_file_contents("acme", "product", path="does/not/exist.py")
            self.assertEqual(
                str(raised.exception),
                "Failed to get file contents. The path does not point to a file "
                "or directory, or the file does not exist in the repository.",
            )

    def test_get_file_contents_caps_tree_matches_at_three(self) -> None:
        environment_id, _ = self._content_fixture(files={f"pkg{index}/util.py": f"# {index}\n" for index in range(5)})
        with self.system.open_service_operations(environment_id, "github", actor="engineer") as operations:
            suggested = operations.get_file_contents("acme", "product", path="util.py")
            self.assertEqual(suggested["matching_files"], ["pkg0/util.py", "pkg1/util.py", "pkg2/util.py"])

    def test_get_content_mirrors_the_rest_contents_endpoint(self) -> None:
        environment_id, _ = self._content_fixture()
        with self.system.open_service_operations(environment_id, "github", actor="engineer") as operations:
            blob = operations.get_content("acme", "product", "src/app.py", ref="main")
            self.assertEqual(blob["type"], "file")
            self.assertEqual(blob["encoding"], "base64")
            self.assertEqual(base64.b64decode(blob["content"]).decode(), "print('hello')\n")
            self.assertEqual(blob["size"], len("print('hello')\n"))
            self.assertEqual(blob["path"], "src/app.py")
            self.assertEqual(blob["name"], "app.py")
            self.assertEqual(blob["url"], "https://api.github.com/repos/acme/product/contents/src/app.py?ref=main")
            self.assertEqual(blob["git_url"], f"https://api.github.com/repos/acme/product/git/blobs/{blob['sha']}")
            self.assertEqual(blob["html_url"], "https://github.com/acme/product/blob/main/src/app.py")
            self.assertEqual(blob["download_url"], "https://raw.githubusercontent.com/acme/product/main/src/app.py")
            self.assertEqual(blob["_links"], {"self": blob["url"], "git": blob["git_url"], "html": blob["html_url"]})

            listing = operations.get_content("acme", "product", "src")
            self.assertEqual([(entry["type"], entry["path"], entry["size"]) for entry in listing], [("file", "src/app.py", 15), ("dir", "src/lib", 0)])
            self.assertIsNone(listing[1]["download_url"])
            self.assertNotIn("content", listing[0])

            root = operations.get_content("acme", "product")
            self.assertEqual([entry["path"] for entry in root], ["README.md", "assets", "src"])

            with self.assertRaises(GitHubNotFound):
                operations.get_content("acme", "product", "src/missing.py")
            with self.assertRaises(GitHubValidationError):
                operations.get_content("acme", "product", "../../etc/passwd")

    def test_repository_tree_mirrors_the_official_tool_and_rest_endpoint(self) -> None:
        environment_id, commit_sha = self._content_fixture()
        with self.system.open_service_operations(environment_id, "github", actor="engineer") as operations:
            shallow = operations.get_repository_tree("acme", "product")
            self.assertEqual([entry["path"] for entry in shallow["tree"]], ["README.md", "assets", "src"])
            self.assertEqual((shallow["tree_sha"], shallow["owner"], shallow["repo"]), ("main", "acme", "product"))
            self.assertEqual((shallow["recursive"], shallow["count"], shallow["truncated"]), (False, 3, False))
            self.assertEqual(shallow["tree"][0]["size"], len("# Product\n"))
            self.assertNotIn("size", shallow["tree"][2])
            self.assertEqual(shallow["tree"][2]["url"], f"https://api.github.com/repos/acme/product/git/trees/{shallow['tree'][2]['sha']}")

            deep = operations.get_repository_tree("acme", "product", recursive=True)
            self.assertEqual([entry["path"] for entry in deep["tree"]], ["README.md", "assets/logo.png", "src/app.py", "src/lib/util.py"])
            self.assertEqual(deep["count"], 4)
            self.assertTrue(all(entry["type"] == "blob" for entry in deep["tree"]))

            filtered = operations.get_repository_tree("acme", "product", recursive=True, path_filter="src/lib")
            self.assertEqual([entry["path"] for entry in filtered["tree"]], ["src/lib/util.py"])
            self.assertEqual(filtered["count"], 1)

            tagged = operations.get_repository_tree("acme", "product", tree_sha="v1.0")
            self.assertEqual(tagged["sha"], shallow["sha"])

            # REST mirror: by branch name, by commit sha, and by the tree sha itself
            by_branch = operations.get_tree("acme", "product", "main")
            self.assertEqual(by_branch["url"], f"https://api.github.com/repos/acme/product/git/trees/{by_branch['sha']}")
            self.assertFalse(by_branch["truncated"])
            self.assertEqual(operations.get_tree("acme", "product", commit_sha)["sha"], by_branch["sha"])
            by_tree = operations.get_tree("acme", "product", by_branch["sha"], recursive="1")
            self.assertEqual(by_tree["sha"], by_branch["sha"])
            self.assertEqual([entry["path"] for entry in by_tree["tree"]], ["README.md", "assets/logo.png", "src/app.py", "src/lib/util.py"])

            with self.assertRaises(GitHubNotFound):
                operations.get_tree("acme", "product", "no-such-branch")

    def test_content_tools_round_trip_through_mcp(self) -> None:
        environment_id, _ = self._content_fixture()
        engineer = MCPDispatcher(self.system, environment_id, actor="engineer")
        listed = {tool["name"] for tool in engineer.list_tools()}
        self.assertLessEqual({"github_get_file_contents", "github_get_repository_tree"}, listed)

        file_result = engineer.call_tool("github_get_file_contents", {"owner": "acme", "repo": "product", "path": "src/app.py", "ref": "main"})
        self.assertFalse(file_result["isError"], file_result["structuredContent"])
        self.assertEqual(file_result["structuredContent"]["result"]["text"], "print('hello')\n")

        tree_result = engineer.call_tool("github_get_repository_tree", {"owner": "acme", "repo": "product", "recursive": True, "path_filter": "src/"})
        self.assertFalse(tree_result["isError"], tree_result["structuredContent"])
        self.assertEqual([entry["path"] for entry in tree_result["structuredContent"]["result"]["tree"]], ["src/app.py", "src/lib/util.py"])

    def test_refs_dispatch_and_atomic_harness_completion(self) -> None:
        template = self.system.create_template(load_template_spec(CONFIG))
        environment = self.system.create_environment_from_template(template.id)
        engineer = MCPDispatcher(self.system, environment.id, actor="engineer")
        root = engineer.call_tool("github_create_commit", {"owner":"acme","repo":"product","message":"root","author":"engineer","files":{"a.txt":"a\n"}})["structuredContent"]["result"]
        engineer.call_tool("github_create_ref", {"owner":"acme","repo":"product","ref":"refs/heads/main","sha":root["sha"]})
        head = engineer.call_tool("github_create_commit", {"owner":"acme","repo":"product","message":"head","author":"engineer","parent_shas":[root["sha"]],"files":{"a.txt":"b\n"}})["structuredContent"]["result"]
        updated = engineer.call_tool("github_update_ref", {"owner":"acme","repo":"product","ref":"heads/main","sha":head["sha"]})
        self.assertFalse(updated["isError"], updated["structuredContent"])
        self.assertEqual(self.system.open_git_data_plane(environment.id, "github").repository(1).resolve_branch("main"), head["sha"])
        dispatched = engineer.call_tool("github_dispatch_workflow", {"owner":"acme","repo":"product","workflow_id":"ci.yml","ref":"main","inputs":{"suite":"full"}})
        self.assertFalse(dispatched["isError"], dispatched["structuredContent"])
        run = engineer.call_tool("github_list_workflow_runs", {"owner":"acme","repo":"product"})["structuredContent"]["result"]["workflow_runs"][0]
        self.system.invoke_service_operation(environment.id, "github", actor="director", transport="ci-harness", operation="complete_workflow_run", arguments={"owner":"acme","repository":"product","run_id":run["id"],"conclusion":"success","log":"1 passed\n"})
        jobs = engineer.call_tool("github_list_workflow_jobs", {"owner":"acme","repo":"product","run_id":run["id"]})["structuredContent"]["result"]["jobs"]
        self.assertEqual((jobs[0]["status"], jobs[0]["conclusion"]), ("completed", "success"))
        self.assertEqual(engineer.call_tool("github_get_workflow_job_log", {"owner":"acme","repo":"product","job_id":jobs[0]["id"]})["structuredContent"]["result"]["log"], "1 passed\n")

    def test_bootstrap_requires_an_organization_admin(self) -> None:
        plugin = GitHubPlugin()
        with self.assertRaisesRegex(ConfigurationError, "organization admin"):
            plugin.validate_bootstrap(
                {
                    "organization": {"login": "acme"},
                    "users": [{"login": "engineer", "role": "member"}],
                    "repositories": [],
                }
            )

    def test_repository_and_issue_operation_contract(self) -> None:
        template = self.system.create_template(load_template_spec(CONFIG))
        environment = self.system.create_environment_from_template(template.id)
        exercise_issue_operations(self, self.system, environment.id)

    def test_pull_request_review_and_merge_contract(self) -> None:
        template = self.system.create_template(load_template_spec(CONFIG))
        environment = self.system.create_environment_from_template(template.id)
        shas = exercise_pull_request_operations(self, self.system, environment.id)
        clean_clone = self.system.create_environment_from_template(template.id)
        clean_repository = self.system.open_git_data_plane(
            clean_clone.id, "github"
        ).repository(1)
        self.assertIsNone(clean_repository.object_type(shas["merge"]))
        self.assertIsNone(clean_repository.resolve_branch("main"))

    def test_git_refs_roll_back_with_relational_transaction(self) -> None:
        template = self.system.create_template(load_template_spec(CONFIG))
        environment = self.system.create_environment_from_template(template.id)
        exercise_git_ref_rollback(self, self.system, environment.id)

    def test_universal_inspector_projection(self) -> None:
        template = self.system.create_template(load_template_spec(CONFIG))
        environment = self.system.create_environment_from_template(template.id)
        exercise_inspector_projection(self, self.system, environment.id)


@unittest.skipUnless(
    os.getenv("MCP_SYSTEM_TEST_POSTGRES_DSN"),
    "set MCP_SYSTEM_TEST_POSTGRES_DSN to run PostgreSQL integration tests",
)
class GitHubPluginPostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.dsn = os.environ["MCP_SYSTEM_TEST_POSTGRES_DSN"]

    def setUp(self) -> None:
        suffix = uuid4().hex[:10]
        self.control_schema = f"github_control_{suffix}"
        self.storage_namespace = f"github_state_{suffix}"
        self.temp_dir = tempfile.TemporaryDirectory()
        self.system = MCPSystem(
            Path(self.temp_dir.name),
            github_registry(),
            control_plane=PostgresControlPlane(
                self.dsn, schema=self.control_schema
            ),
            service_storage=PostgresServiceStorage(
                self.dsn, namespace=self.storage_namespace
            ),
        )

    def tearDown(self) -> None:
        with psycopg.connect(self.dsn) as connection:
            rows = connection.execute(
                """
                SELECT nspname FROM pg_namespace
                 WHERE nspname = %s OR nspname LIKE %s
                """,
                (self.control_schema, f"{self.storage_namespace}\\_%"),
            ).fetchall()
            for (schema_name,) in rows:
                connection.execute(
                    sql.SQL("DROP SCHEMA {} CASCADE").format(
                        sql.Identifier(schema_name)
                    )
                )
        self.temp_dir.cleanup()

    def test_materialized_instance_has_constraints_and_reset_sequences(self) -> None:
        template = self.system.create_template(load_template_spec(CONFIG))
        environment = self.system.create_environment_from_template(template.id)

        with self.system.open_service_database(environment.id, "github") as session:
            new_user = session.execute(
                """
                INSERT INTO github_users(
                    login, name, email, user_type, site_admin, created_at
                ) VALUES (?, ?, ?, ?, ?, ?) RETURNING id
                """,
                (
                    "release-bot",
                    "Release Bot",
                    "release-bot@acme.local",
                    "Bot",
                    False,
                    "2026-01-01T00:00:00+00:00",
                ),
            ).fetchone()
            issue = session.execute(
                """
                INSERT INTO github_issues(
                    repository_id, number, title, body, state, author_id,
                    locked, is_pull_request, comments_count, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id
                """,
                (
                    1,
                    1,
                    "Bootstrap verification",
                    None,
                    "open",
                    3,
                    False,
                    False,
                    0,
                    "2026-01-01T00:00:00+00:00",
                    "2026-01-01T00:00:00+00:00",
                ),
            ).fetchone()

        self.assertEqual(new_user["id"], 5)
        self.assertEqual(issue["id"], 1)

    def test_repository_and_issue_operation_contract(self) -> None:
        template = self.system.create_template(load_template_spec(CONFIG))
        environment = self.system.create_environment_from_template(template.id)
        exercise_issue_operations(self, self.system, environment.id)

    def test_pull_request_review_and_merge_contract(self) -> None:
        template = self.system.create_template(load_template_spec(CONFIG))
        environment = self.system.create_environment_from_template(template.id)
        shas = exercise_pull_request_operations(self, self.system, environment.id)
        clean_clone = self.system.create_environment_from_template(template.id)
        clean_repository = self.system.open_git_data_plane(
            clean_clone.id, "github"
        ).repository(1)
        self.assertIsNone(clean_repository.object_type(shas["merge"]))
        self.assertIsNone(clean_repository.resolve_branch("main"))

    def test_git_refs_roll_back_with_relational_transaction(self) -> None:
        template = self.system.create_template(load_template_spec(CONFIG))
        environment = self.system.create_environment_from_template(template.id)
        exercise_git_ref_rollback(self, self.system, environment.id)

    def test_universal_inspector_projection(self) -> None:
        template = self.system.create_template(load_template_spec(CONFIG))
        environment = self.system.create_environment_from_template(template.id)
        exercise_inspector_projection(self, self.system, environment.id)


if __name__ == "__main__":
    unittest.main()
