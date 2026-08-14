"""Byte-stable export and import of a whole environment.

`snapshot_environment` produces an on-disk clone: fast, but its bytes are
database pages and a manifest stamped with the wall clock, so two exports of
the same logical state differ. A harness that treats a world digest as the
world's identity needs the opposite property:

    export(import(export(x))) == export(x)

so this module writes a *logical* document instead — relational rows in a
canonical order, plus every Git object keyed by its sha — and imports it back
into a freshly provisioned environment.

The envelope carries no timestamps and no ids of its own; every byte comes
from the state being described.
"""

from __future__ import annotations

import base64
import json
from typing import Any, Mapping

from .errors import ConfigurationError
from .models import EnvironmentSpec, EnvironmentStatus, ServiceInstanceSpec, ServiceStatus

FORMAT_VERSION = 1


def export_environment(system: Any, environment_id: str) -> bytes:
    """Serialise one environment into a canonical, byte-stable document."""
    environment = system.require_environment(environment_id)
    if environment.status is not EnvironmentStatus.READY:
        raise ConfigurationError(
            f"environment {environment_id!r} is {environment.status}"
        )
    services = []
    for service in system.list_services(environment_id):
        plugin = system.registry.resolve(service.plugin_id, service.plugin_version)
        record: dict[str, Any] = {
            "instanceId": service.instance_id,
            "pluginId": service.plugin_id,
            "pluginVersion": service.plugin_version,
            "relational": _canonical_relational(
                system.service_storage.inspect(service.database_path)
            ),
        }
        if system._has_git_data_plane(plugin):
            record["git"] = _export_git(system, environment_id, service.instance_id)
        services.append(record)

    document = {
        "formatVersion": FORMAT_VERSION,
        "mcpSurfaces": sorted(system.list_mcp_surfaces(environment_id)),
        "services": sorted(services, key=lambda item: item["instanceId"]),
    }
    return json.dumps(
        document, sort_keys=True, separators=(",", ":"), default=_encode_scalar
    ).encode("utf-8")


def import_environment(system: Any, blob: bytes, *, name: str | None = None):
    """Rebuild an environment from `export_environment` output.

    Returns the new Environment. The id is new — callers that need stable
    identity keep their own mapping, which is cheaper than teaching every
    storage backend to restore in place.
    """
    document = json.loads(blob.decode("utf-8"))
    version = document.get("formatVersion")
    if version != FORMAT_VERSION:
        raise ConfigurationError(
            f"unsupported portable format version {version!r}"
            f" (this build reads {FORMAT_VERSION})"
        )

    spec = EnvironmentSpec(
        name=name or "imported environment",
        services=tuple(
            ServiceInstanceSpec(
                instance_id=record["instanceId"],
                plugin_id=record["pluginId"],
                plugin_version=record["pluginVersion"],
                seed={},
            )
            for record in document["services"]
        ),
        mcp_surfaces=tuple(document["mcpSurfaces"]),
    )
    environment = system.create_environment(spec, bootstrap=False)
    try:
        for record in document["services"]:
            instance_id = record["instanceId"]
            service = system.control_plane.get_service(environment.id, instance_id)
            system.service_storage.load(
                service.database_path, _decode_relational(record["relational"])
            )
            if "git" in record:
                _import_git(system, environment.id, instance_id, record["git"])
    except Exception:
        system.control_plane.set_environment_status(
            environment.id, EnvironmentStatus.FAILED, system.clock.now(),
            failure_reason="portable import failed",
        )
        raise
    return system.require_environment(environment.id)


# ---------- relational ----------


def _canonical_relational(dump: Mapping[str, Any]) -> dict[str, Any]:
    """Sort tables by name and rows by their primary key, then by full value.

    Row order out of a SELECT is not guaranteed, and a snapshot digest that
    depends on it would drift for reasons that have nothing to do with state.
    """
    canonical: dict[str, Any] = {}
    for table in sorted(dump):
        content = dump[table]
        columns = list(content["columns"])
        primary_key = [c for c in content["primaryKey"] if c in columns] or columns
        rows = [
            {column: _encode_scalar(row[column]) for column in columns}
            for row in content["rows"]
        ]
        rows.sort(key=lambda row: json.dumps(
            [row[column] for column in primary_key] + [row[c] for c in columns],
            sort_keys=True, default=str,
        ))
        canonical[table] = {"columns": columns, "primaryKey": list(content["primaryKey"]),
                            "rows": rows}
    return canonical


def _decode_relational(dump: Mapping[str, Any]) -> dict[str, Any]:
    return {
        table: {
            "columns": content["columns"],
            "primaryKey": content["primaryKey"],
            "rows": [
                {column: _decode_scalar(value) for column, value in row.items()}
                for row in content["rows"]
            ],
        }
        for table, content in dump.items()
    }


def _encode_scalar(value: Any) -> Any:
    """JSON cannot carry bytes; tag them instead of guessing an encoding."""
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {"__bytes__": base64.b64encode(bytes(value)).decode("ascii")}
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return {"__repr__": str(value)}


def _decode_scalar(value: Any) -> Any:
    if isinstance(value, dict) and "__bytes__" in value:
        return base64.b64decode(value["__bytes__"])
    if isinstance(value, dict) and "__repr__" in value:
        return value["__repr__"]
    return value


# ---------- git ----------


def _export_git(system: Any, environment_id: str, instance_id: str) -> dict[str, Any]:
    locator = system.git_storage.build_locator(environment_id, instance_id)
    if not system.git_storage.exists(locator):
        return {"repositories": {}}
    data_plane = system.git_storage.open(locator)
    repositories: dict[str, Any] = {}
    for repository_id in system.git_storage.repository_ids(locator):
        repository = data_plane.repository(repository_id)
        repositories[str(repository_id)] = {
            "refs": dict(sorted(repository.all_refs().items())),
            "objects": [
                {
                    "sha": sha,
                    "type": object_type,
                    "content": base64.b64encode(content).decode("ascii"),
                }
                for sha, object_type, content in repository.export_objects()
            ],
        }
    return {"repositories": repositories}


def _import_git(
    system: Any, environment_id: str, instance_id: str, payload: Mapping[str, Any]
) -> None:
    locator = system.git_storage.build_locator(environment_id, instance_id)
    data_plane = system.git_storage.open(locator)
    for repository_id, content in payload["repositories"].items():
        repository = data_plane.repository(int(repository_id))
        repository.initialize()
        repository.import_objects([
            (item["sha"], item["type"], base64.b64decode(item["content"]))
            for item in content["objects"]
        ])
        for name, sha in content["refs"].items():
            repository.set_ref(name, sha)
