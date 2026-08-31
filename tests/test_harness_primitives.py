"""Primitives an external harness needs: delete, freeze, watermark, portable
export/import, and the Git reads that Merkle-hashing and release notes depend on.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from mcp_system import MCPSystem, PluginRegistry, TemplateSpec
from mcp_system.config import load_template_spec
from mcp_system.errors import EnvironmentFrozenError, EnvironmentNotFoundError
from mcp_system.git_storage import BareGitRepository
from mcp_system.service_plugins import GitLabPlugin, JiraPlugin


def registry() -> PluginRegistry:
    result = PluginRegistry()
    result.register(JiraPlugin())
    result.register(GitLabPlugin())
    return result


def company_spec() -> TemplateSpec:
    jira = load_template_spec(Path("configs/templates/jira-default.toml"))
    gitlab = load_template_spec(Path("configs/templates/gitlab-default.toml"))
    return TemplateSpec(
        template_id="harness_company",
        name="Harness company",
        version="1.0.0",
        services=(*jira.services, *gitlab.services),
        mcp_surfaces=("jira_rest_v3", "gitlab_rest_v4"),
    )


class HarnessPrimitivesTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.system = MCPSystem(Path(self._tmp.name), registry())
        self.spec = company_spec()
        self.system.create_template(self.spec)
        self.environment = self.system.create_environment_from_template(
            self.spec.template_id
        )

    # ---- helpers ----

    def commit(self, message: str, files: dict[str, str],
               parents: tuple[str, ...] = ()) -> dict:
        return self.system.invoke_service_operation(
            self.environment.id, "gitlab", actor="engineer", transport="mcp",
            operation="create_commit",
            arguments={"project": "acme/product", "message": message,
                       "author": "engineer", "files": files,
                       "parent_shas": list(parents)},
        )

    def seed_repo(self) -> str:
        base = self.commit("Baseline", {"app.py": "VALUE = 1\n"})
        self.system.invoke_service_operation(
            self.environment.id, "gitlab", actor="engineer", transport="mcp",
            operation="create_branch",
            arguments={"project": "acme/product", "branch": "main",
                       "ref": base["sha"]},
        )
        return base["sha"]

    # ---- watermark and the mutation log ----

    def test_watermark_is_monotonic_and_filters_by_actor(self) -> None:
        start = self.system.operation_watermark(self.environment.id)
        self.seed_repo()
        self.system.invoke_service_operation(
            self.environment.id, "jira", actor="lead", transport="mcp",
            operation="create_issue",
            arguments={"fields": {"project": {"key": "PROD"},
                                  "summary": "Do the thing",
                                  "issuetype": {"name": "Task"}}},
        )
        after = self.system.operation_watermark(self.environment.id)
        self.assertGreater(after, start)

        seqs = [op.seq for op in self.system.list_operations(self.environment.id)]
        self.assertEqual(seqs, sorted(seqs))
        self.assertEqual(len(seqs), len(set(seqs)))

        engineer = self.system.list_operations(self.environment.id, actor="engineer")
        self.assertTrue(engineer)
        self.assertTrue(all(op.actor == "engineer" for op in engineer))
        self.assertEqual(
            self.system.list_operations(self.environment.id, actor="nobody"), ()
        )

    def test_since_seq_returns_only_newer_operations(self) -> None:
        self.seed_repo()
        mark = self.system.operation_watermark(self.environment.id)
        self.commit("Later", {"later.py": "x = 1\n"})
        fresh = self.system.list_operations(self.environment.id, since_seq=mark)
        self.assertEqual([op.operation for op in fresh], ["create_commit"])

    def test_tool_call_id_is_recorded(self) -> None:
        self.system.invoke_service_operation(
            self.environment.id, "jira", actor="lead", transport="mcp",
            operation="create_issue", tool_call_id="req-42",
            arguments={"fields": {"project": {"key": "PROD"},
                                  "summary": "Trace me",
                                  "issuetype": {"name": "Task"}}},
        )
        latest = self.system.list_operations(self.environment.id)[-1]
        self.assertEqual(latest.tool_call_id, "req-42")

    # ---- freeze ----

    def test_freeze_refuses_agent_writes_and_admin_can_still_act(self) -> None:
        self.seed_repo()
        self.system.freeze_environment(self.environment.id)
        self.assertTrue(self.system.require_environment(self.environment.id).frozen)

        with self.assertRaises(EnvironmentFrozenError):
            self.commit("Should not land", {"nope.py": "x = 1\n"})

        # allow_frozen is reachable only from the Python API — the MCP
        # dispatcher never passes it — so this is the admin plane, not an agent
        self.system.invoke_service_operation(
            self.environment.id, "gitlab", actor="engineer",
            transport="harness-admin", operation="create_commit",
            allow_frozen=True,
            arguments={"project": "acme/product", "message": "Admin may write",
                       "author": "engineer", "files": {"admin.py": "x = 1\n"},
                       "parent_shas": []},
        )

        self.system.unfreeze_environment(self.environment.id)
        self.commit("Lands again", {"after.py": "x = 1\n"})

    def test_freeze_is_visible_to_another_process_view(self) -> None:
        """The flag lives in the control plane, not in one object's memory."""
        self.seed_repo()
        self.system.freeze_environment(self.environment.id)
        other = MCPSystem(Path(self._tmp.name), registry())
        self.assertTrue(other.require_environment(self.environment.id).frozen)

    # ---- delete ----

    def test_delete_environment_removes_every_trace(self) -> None:
        self.seed_repo()
        services = self.system.list_services(self.environment.id)
        database = Path(self._tmp.name) / services[0].database_path
        git_root = Path(self._tmp.name) / self.system.git_storage.build_locator(
            self.environment.id, "gitlab"
        )
        self.assertTrue(database.exists())
        self.assertTrue(git_root.is_dir())

        self.system.delete_environment(self.environment.id)

        with self.assertRaises(EnvironmentNotFoundError):
            self.system.require_environment(self.environment.id)
        self.assertFalse(database.exists())
        self.assertFalse(git_root.exists())
        self.assertNotIn(
            self.environment.id, [e.id for e in self.system.list_environments()]
        )

    def test_delete_of_a_missing_environment_is_an_error(self) -> None:
        with self.assertRaises(EnvironmentNotFoundError):
            self.system.delete_environment("does-not-exist")

    # ---- portable export / import ----

    def test_export_import_round_trips_byte_for_byte(self) -> None:
        base = self.seed_repo()
        self.system.invoke_service_operation(
            self.environment.id, "jira", actor="lead", transport="mcp",
            operation="create_issue",
            arguments={"fields": {"project": {"key": "PROD"},
                                  "summary": "Carried across",
                                  "issuetype": {"name": "Task"}}},
        )
        blob = self.system.export_environment(self.environment.id)

        restored = self.system.import_environment(blob, name="restored")
        self.assertNotEqual(restored.id, self.environment.id)
        again = self.system.export_environment(restored.id)
        self.assertEqual(again, blob, "export(import(export(x))) must be stable")

        # and the state is genuinely there, not just equal bytes
        issue = self.system.invoke_service_operation(
            restored.id, "jira", actor="lead", transport="mcp",
            operation="get_issue", arguments={"issue_id_or_key": "PROD-1"},
        )
        self.assertEqual(issue["fields"]["summary"], "Carried across")
        file = self.system.invoke_service_operation(
            restored.id, "gitlab", actor="engineer", transport="mcp",
            operation="get_repository_file",
            arguments={"project": "acme/product", "file_path": "app.py",
                       "ref": "main"},
        )
        self.assertEqual(file["content"], "VkFMVUUgPSAxCg==")
        plane = self.system.open_git_data_plane(restored.id, "gitlab")
        self.assertEqual(plane.repository(1).resolve_branch("main"), base)

    def test_export_is_stable_across_repeated_calls(self) -> None:
        self.seed_repo()
        first = self.system.export_environment(self.environment.id)
        second = self.system.export_environment(self.environment.id)
        self.assertEqual(first, second)

    def test_export_changes_when_state_changes(self) -> None:
        self.seed_repo()
        before = self.system.export_environment(self.environment.id)
        self.commit("More", {"more.py": "x = 1\n"})
        self.assertNotEqual(before, self.system.export_environment(self.environment.id))

    def test_import_refuses_an_unknown_format_version(self) -> None:
        from mcp_system.errors import ConfigurationError

        with self.assertRaises(ConfigurationError):
            self.system.import_environment(b'{"formatVersion": 99}')

    # ---- git reads the harness depends on ----

    def test_read_tree_contents_returns_bytes_for_every_file(self) -> None:
        base = self.commit("Baseline",
                           {"app.py": "VALUE = 1\n", "pkg/util.py": "X = 2\n"})
        head = self.commit("Second", {"README.md": "# hi\n"}, (base["sha"],))
        plane = self.system.open_git_data_plane(self.environment.id, "gitlab")
        tree = plane.repository(1).read_tree_contents(head["sha"])
        self.assertEqual(
            tree,
            {"app.py": b"VALUE = 1\n", "pkg/util.py": b"X = 2\n",
             "README.md": b"# hi\n"},
        )

    def test_tree_reads_and_exports_use_binary_safe_batch_reads(self) -> None:
        repository = self.system.open_git_data_plane(
            self.environment.id, "gitlab"
        ).repository(1)
        commit = repository.create_commit(
            message="Binary fixture",
            author_name="engineer",
            author_email="engineer@example.com",
            timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
            files={"fixture.bin": b"before\x00middle\nafter\xff"},
        )

        with patch(
            "mcp_system.git_storage.subprocess.run", wraps=subprocess.run
        ) as run:
            tree = repository.read_tree_contents(commit["sha"])
            exported = repository.export_objects()

        self.assertEqual(tree["fixture.bin"], b"before\x00middle\nafter\xff")
        self.assertIn(
            b"before\x00middle\nafter\xff", [item[2] for item in exported]
        )
        commands = [call.args[0] for call in run.call_args_list]
        self.assertEqual(commands.count(("git", "cat-file", "--batch")), 2)

    def test_commit_batches_blob_writes_and_index_updates(self) -> None:
        repository = self.system.open_git_data_plane(
            self.environment.id, "gitlab"
        ).repository(1)
        base = repository.create_commit(
            message="Base",
            author_name="engineer",
            author_email="engineer@example.com",
            timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
            files={"delete.txt": b"old", "keep.txt": b"before"},
        )

        with patch(
            "mcp_system.git_storage.subprocess.run", wraps=subprocess.run
        ) as run:
            commit = repository.create_commit(
                message="Batch changes",
                author_name="engineer",
                author_email="engineer@example.com",
                timestamp=datetime(2026, 1, 2, tzinfo=timezone.utc),
                parent_shas=(base["sha"],),
                files={
                    "delete.txt": None,
                    "keep.txt": b"after\x00",
                    "new.txt": b"new",
                },
            )

        commands = [call.args[0] for call in run.call_args_list]
        hashes = [
            command for command in commands
            if command[:2] == ("git", "hash-object")
        ]
        indexes = [
            command for command in commands
            if command[:2] == ("git", "update-index")
        ]
        self.assertEqual(len(hashes), 1)
        self.assertIn("--stdin-paths", hashes[0])
        self.assertEqual(
            indexes, [("git", "update-index", "-z", "--index-info")]
        )
        self.assertEqual(
            repository.read_tree_contents(commit["sha"]),
            {"keep.txt": b"after\x00", "new.txt": b"new"},
        )

    def test_import_batches_each_git_object_type(self) -> None:
        head = self.seed_repo()
        source = self.system.open_git_data_plane(
            self.environment.id, "gitlab"
        ).repository(1)
        objects = source.export_objects()
        restored = BareGitRepository(Path(self._tmp.name) / "batch-import.git")
        restored.initialize()

        with patch(
            "mcp_system.git_storage.subprocess.run", wraps=subprocess.run
        ) as run:
            restored.import_objects(objects)

        commands = [call.args[0] for call in run.call_args_list]
        hashes = [
            command for command in commands
            if command[:2] == ("git", "hash-object")
        ]
        self.assertEqual(
            len(hashes), len({object_type for _, object_type, _ in objects})
        )
        self.assertTrue(all("--stdin-paths" in command for command in hashes))
        self.assertEqual(restored.object_type(head), "commit")

    def test_log_honours_the_range_and_merges_only(self) -> None:
        base = self.seed_repo()
        second = self.commit("Second", {"a.py": "x = 1\n"}, (base,))
        plane = self.system.open_git_data_plane(self.environment.id, "gitlab")
        repository = plane.repository(1)
        repository.update_branch("main", second["sha"])

        self.assertEqual(
            repository.log(to_ref=second["sha"], from_ref=base), [second["sha"]]
        )
        self.assertEqual(
            repository.log(to_ref=second["sha"], from_ref=base, merges_only=True), []
        )
        self.assertEqual(len(repository.log(to_ref=second["sha"])), 2)

    def test_tags_are_real_git_refs(self) -> None:
        head = self.seed_repo()
        plane = self.system.open_git_data_plane(self.environment.id, "gitlab")
        repository = plane.repository(1)
        repository.update_tag("v0.1.0", head)
        self.assertEqual(repository.resolve_tag("v0.1.0"), head)
        self.assertIn("refs/tags/v0.1.0", repository.all_refs())
        self.assertEqual(repository.read_tree_contents("v0.1.0")["app.py"],
                         b"VALUE = 1\n")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
