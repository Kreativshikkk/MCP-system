from __future__ import annotations

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
from mcp_system.service_plugins import GitHubPlugin
from mcp_system.service_plugins.github import (
    GitHubConflict,
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

        self.assertEqual(len(tables), 20)
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
        with self.system.open_service_database(environment.id, "github") as session:
            projection = GitHubInspectorAdapter().project(session, self.system.open_git_data_plane(environment.id, "github"))
        self.assertEqual(projection["repositories"][0]["builds"][0]["conclusion"], "success")

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
