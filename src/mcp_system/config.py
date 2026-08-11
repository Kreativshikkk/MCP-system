"""Strict declarative configuration loaders using Python's TOML parser."""

from __future__ import annotations

from pathlib import Path
import tomllib
from typing import Any, Mapping

from .errors import ConfigurationError
from .models import EnvironmentSpec, ServiceInstanceSpec, TemplateSpec


def load_environment_spec(path: Path) -> EnvironmentSpec:
    document = _load(path)
    _require_keys(document, {"environment", "services"}, "document")
    header = _require_mapping(document.get("environment"), "environment")
    _require_keys(header, {"name", "mcp_surfaces"}, "environment")
    return EnvironmentSpec(
        name=_require_string(header.get("name"), "environment.name"),
        services=_load_services(document.get("services")),
        mcp_surfaces=_load_string_tuple(
            header.get("mcp_surfaces", []), "environment.mcp_surfaces"
        ),
    )


def load_template_spec(path: Path) -> TemplateSpec:
    document = _load(path)
    _require_keys(document, {"template", "services"}, "document")
    header = _require_mapping(document.get("template"), "template")
    _require_keys(
        header, {"id", "name", "version", "mcp_surfaces"}, "template"
    )
    return TemplateSpec(
        template_id=_require_string(header.get("id"), "template.id"),
        name=_require_string(header.get("name"), "template.name"),
        version=_require_string(header.get("version"), "template.version"),
        services=_load_services(document.get("services")),
        mcp_surfaces=_load_string_tuple(
            header.get("mcp_surfaces", []), "template.mcp_surfaces"
        ),
    )


def _load(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as stream:
            return tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigurationError(f"cannot load TOML config {path}: {exc}") from exc


def _load_services(value: Any) -> tuple[ServiceInstanceSpec, ...]:
    if not isinstance(value, list):
        raise ConfigurationError("services must be an array of tables")
    services: list[ServiceInstanceSpec] = []
    for index, raw in enumerate(value):
        context = f"services[{index}]"
        service = _require_mapping(raw, context)
        _require_keys(
            service, {"instance_id", "plugin", "version", "seed"}, context
        )
        seed = service.get("seed", {})
        if not isinstance(seed, dict):
            raise ConfigurationError(f"{context}.seed must be a table")
        services.append(
            ServiceInstanceSpec(
                instance_id=_require_string(
                    service.get("instance_id"), f"{context}.instance_id"
                ),
                plugin_id=_require_string(
                    service.get("plugin"), f"{context}.plugin"
                ),
                plugin_version=_require_string(
                    service.get("version"), f"{context}.version"
                ),
                seed=seed,
            )
        )
    return tuple(services)


def _require_mapping(value: Any, context: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise ConfigurationError(f"{context} must be a table")
    return value


def _require_string(value: Any, context: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigurationError(f"{context} must be a non-empty string")
    return value


def _load_string_tuple(value: Any, context: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ConfigurationError(f"{context} must be an array of strings")
    return tuple(value)


def _require_keys(
    mapping: Mapping[str, Any], allowed: set[str], context: str
) -> None:
    unknown = set(mapping) - allowed
    if unknown:
        raise ConfigurationError(
            f"unknown keys in {context}: {', '.join(sorted(unknown))}"
        )
