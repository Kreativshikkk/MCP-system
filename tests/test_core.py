from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import sqlite3
import tempfile
import unittest
from typing import Any, Mapping, Sequence

from mcp_system import (
    EnvironmentSpec,
    EnvironmentStatus,
    MCPSystem,
    Migration,
    PluginManifest,
    PluginRegistry,
    ServiceInstanceSpec,
    TemplateSpec,
)
from mcp_system.config import load_environment_spec
from mcp_system.errors import ConfigurationError
from backend_contract import assert_template_clone_contract


class SequentialIds:
    def __init__(self) -> None:
        self.value = 0

    def new_environment_id(self) -> str:
        self.value += 1
        return f"env{self.value:04d}"


@dataclass(frozen=True)
class KeyValuePlugin:
    manifest: PluginManifest = PluginManifest(
        plugin_id="key_value",
        version="1.0.0",
        capabilities=("persistent_state",),
    )

    def migrations(self, storage_kind: str) -> Sequence[Migration]:
        if storage_kind != "sqlite":
            raise ValueError(f"unsupported storage kind: {storage_kind}")
        return (
            Migration(
                version=1,
                statements=(
                    """
                    CREATE TABLE entries (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    )
                    """,
                ),
            ),
        )

    def validate_bootstrap(self, config: Mapping[str, Any]) -> None:
        if "entries" in config and not isinstance(config["entries"], dict):
            raise ValueError("entries must be a mapping")

    def seed(self, connection: sqlite3.Connection, config: Mapping[str, Any]) -> None:
        connection.executemany(
            "INSERT INTO entries(key, value) VALUES (?, ?)",
            sorted(dict(config.get("entries", {})).items()),
        )


class FailingPlugin:
    manifest = PluginManifest(plugin_id="failing", version="1.0.0")

    def migrations(self, storage_kind: str) -> Sequence[Migration]:
        return (Migration(1, ("CREATE TABLE partial_state(id INTEGER)",)),)

    def validate_bootstrap(self, config: Mapping[str, Any]) -> None:
        return None

    def seed(self, connection: sqlite3.Connection, config: Mapping[str, Any]) -> None:
        raise RuntimeError("intentional seed failure")


def registry_with(*plugins: object) -> PluginRegistry:
    registry = PluginRegistry()
    for plugin in plugins:
        registry.register(plugin)  # type: ignore[arg-type]
    return registry


def environment_spec(seed_value: str) -> EnvironmentSpec:
    return EnvironmentSpec(
        name="local-company",
        services=(
            ServiceInstanceSpec(
                instance_id="code_host",
                plugin_id="key_value",
                plugin_version="1.0.0",
                seed={"entries": {"seed": seed_value}},
            ),
        ),
        mcp_surfaces=("github_standard", "codebase"),
    )


class MCPSystemCoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.data_root = Path(self.temp_dir.name)
        self.registry = registry_with(KeyValuePlugin())
        self.system = MCPSystem(
            self.data_root, self.registry, ids=SequentialIds()
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_environment_state_survives_runtime_restart(self) -> None:
        environment = self.system.create_environment(environment_spec("original"))

        with self.system.open_service_database(
            environment.id, "code_host"
        ) as connection:
            connection.execute(
                "INSERT INTO entries(key, value) VALUES (?, ?)",
                ("runtime", "persisted"),
            )

        restarted = MCPSystem(self.data_root, self.registry)
        restored = restarted.require_environment(environment.id)
        self.assertEqual(restored.status, EnvironmentStatus.READY)
        with restarted.open_service_database(
            environment.id, "code_host"
        ) as connection:
            rows = connection.execute(
                "SELECT key, value FROM entries ORDER BY key"
            ).fetchall()

        self.assertEqual(
            [(row["key"], row["value"]) for row in rows],
            [("runtime", "persisted"), ("seed", "original")],
        )

    def test_environments_have_isolated_service_databases(self) -> None:
        first = self.system.create_environment(environment_spec("first"))
        second = self.system.create_environment(environment_spec("second"))

        with self.system.open_service_database(first.id, "code_host") as connection:
            connection.execute(
                "INSERT INTO entries(key, value) VALUES ('private', 'first-only')"
            )

        with self.system.open_service_database(second.id, "code_host") as connection:
            rows = connection.execute(
                "SELECT key, value FROM entries ORDER BY key"
            ).fetchall()

        self.assertEqual(
            [(row["key"], row["value"]) for row in rows],
            [("seed", "second")],
        )

    def test_selected_mcp_surfaces_are_persisted(self) -> None:
        environment = self.system.create_environment(environment_spec("seed"))
        restarted = MCPSystem(self.data_root, self.registry)

        self.assertEqual(
            restarted.list_mcp_surfaces(environment.id),
            ("codebase", "github_standard"),
        )

    def test_failed_provisioning_is_visible_and_not_ready(self) -> None:
        system = MCPSystem(
            self.data_root,
            registry_with(FailingPlugin()),
            ids=SequentialIds(),
        )
        spec = EnvironmentSpec(
            name="broken",
            services=(
                ServiceInstanceSpec("broken", "failing", "1.0.0"),
            ),
        )

        with self.assertRaisesRegex(RuntimeError, "intentional seed failure"):
            system.create_environment(spec)

        environments = system.list_environments()
        self.assertEqual(len(environments), 1)
        self.assertEqual(environments[0].status, EnvironmentStatus.FAILED)
        self.assertIn("intentional seed failure", environments[0].failure_reason or "")

    def test_template_clones_are_persistent_and_mutually_isolated(self) -> None:
        def mutate(system: MCPSystem, environment_id: str) -> None:
            with system.open_service_database(
                environment_id, "code_host"
            ) as connection:
                connection.execute(
                    "INSERT INTO entries(key, value) VALUES ('private', 'first-only')"
                )

        def read(system: MCPSystem, environment_id: str) -> list[tuple[str, str]]:
            with system.open_service_database(
                environment_id, "code_host"
            ) as connection:
                rows = connection.execute(
                    "SELECT key, value FROM entries ORDER BY key"
                ).fetchall()
            return [(row["key"], row["value"]) for row in rows]

        assert_template_clone_contract(
            self,
            system=self.system,
            restart=lambda: MCPSystem(self.data_root, self.registry),
            template_spec=TemplateSpec(
                template_id="github_default",
                name="GitHub default",
                version="1.0.0",
                services=environment_spec("baseline").services,
                mcp_surfaces=("github_standard",),
            ),
            mutate_first=mutate,
            read_second=read,
            expected_second_state=[("seed", "baseline")],
        )

    def test_environment_can_be_loaded_from_strict_toml(self) -> None:
        config_path = self.data_root / "environment.toml"
        config_path.write_text(
            """
[environment]
name = "configured company"
mcp_surfaces = ["github_standard", "codebase"]

[[services]]
instance_id = "code_host"
plugin = "key_value"
version = "1.0.0"

[services.seed.entries]
seed = "from-toml"
""".strip(),
            encoding="utf-8",
        )

        environment = self.system.create_environment_from_toml(config_path)
        self.assertEqual(environment.name, "configured company")
        with self.system.open_service_database(
            environment.id, "code_host"
        ) as connection:
            value = connection.execute(
                "SELECT value FROM entries WHERE key = 'seed'"
            ).fetchone()["value"]
        self.assertEqual(value, "from-toml")

    def test_toml_loader_rejects_unknown_keys(self) -> None:
        config_path = self.data_root / "invalid.toml"
        config_path.write_text(
            """
[environment]
name = "invalid"
mcp_surfaces = []
unexpected = true

[[services]]
instance_id = "code_host"
plugin = "key_value"
version = "1.0.0"
""".strip(),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(ConfigurationError, "unknown keys"):
            load_environment_spec(config_path)


if __name__ == "__main__":
    unittest.main()
