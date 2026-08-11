"""Dependency-free domain models for the control plane."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
import re
from typing import Any, Mapping

from .errors import ConfigurationError


_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_-]{0,62}$")


def require_identifier(value: str, field_name: str) -> None:
    if not _IDENTIFIER.fullmatch(value):
        raise ConfigurationError(
            f"{field_name} must match {_IDENTIFIER.pattern!r}; got {value!r}"
        )


class EnvironmentStatus(StrEnum):
    PROVISIONING = "provisioning"
    READY = "ready"
    FAILED = "failed"
    DELETED = "deleted"


class ServiceStatus(StrEnum):
    PROVISIONING = "provisioning"
    READY = "ready"
    FAILED = "failed"


class TemplateStatus(StrEnum):
    PROVISIONING = "provisioning"
    READY = "ready"
    FAILED = "failed"


class OperationStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


@dataclass(frozen=True, slots=True)
class EnvironmentSnapshot:
    id: str
    source_environment_id: str
    name: str
    created_at: datetime
    operation_count: int
    operation_cursor: str | None
    mcp_surfaces: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SnapshotDiff:
    before_snapshot_id: str
    after_snapshot_id: str
    metadata: Mapping[str, Any]
    services: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class ServiceInstanceSpec:
    instance_id: str
    plugin_id: str
    plugin_version: str
    seed: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        require_identifier(self.instance_id, "service instance_id")
        require_identifier(self.plugin_id, "plugin_id")
        if not self.plugin_version.strip():
            raise ConfigurationError("plugin_version must not be empty")


@dataclass(frozen=True, slots=True)
class EnvironmentSpec:
    name: str
    services: tuple[ServiceInstanceSpec, ...]
    mcp_surfaces: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ConfigurationError("environment name must not be empty")
        if not self.services:
            raise ConfigurationError("environment must contain at least one service")

        service_ids = [service.instance_id for service in self.services]
        if len(service_ids) != len(set(service_ids)):
            raise ConfigurationError("service instance_id values must be unique")

        for surface in self.mcp_surfaces:
            require_identifier(surface, "MCP surface id")
        if len(self.mcp_surfaces) != len(set(self.mcp_surfaces)):
            raise ConfigurationError("MCP surface ids must be unique")


@dataclass(frozen=True, slots=True)
class Environment:
    id: str
    name: str
    status: EnvironmentStatus
    created_at: datetime
    updated_at: datetime
    template_id: str | None = None
    snapshot_id: str | None = None
    failure_reason: str | None = None


@dataclass(frozen=True, slots=True)
class ServiceInstance:
    environment_id: str
    instance_id: str
    plugin_id: str
    plugin_version: str
    status: ServiceStatus
    database_path: str


@dataclass(frozen=True, slots=True)
class OperationRecord:
    """One durable attempt to invoke a provider-domain operation."""

    id: str
    environment_id: str
    service_instance_id: str
    plugin_id: str
    actor: str
    transport: str
    operation: str
    request: Mapping[str, Any]
    status: OperationStatus
    started_at: datetime
    completed_at: datetime | None = None
    result: Any | None = None
    error: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        if not self.id.strip():
            raise ConfigurationError("operation id must not be empty")
        if not self.environment_id.strip():
            raise ConfigurationError("operation environment_id must not be empty")
        require_identifier(self.service_instance_id, "operation service_instance_id")
        require_identifier(self.plugin_id, "operation plugin_id")
        require_identifier(self.transport, "operation transport")
        if not self.actor.strip():
            raise ConfigurationError("operation actor must not be empty")
        if not self.operation.strip():
            raise ConfigurationError("operation name must not be empty")
        if self.status is OperationStatus.RUNNING:
            if (
                self.completed_at is not None
                or self.result is not None
                or self.error is not None
            ):
                raise ConfigurationError("a running operation cannot have an outcome")
        elif self.completed_at is None:
            raise ConfigurationError("a completed operation requires completed_at")
        if self.status is OperationStatus.SUCCEEDED and self.error is not None:
            raise ConfigurationError("a successful operation cannot have an error")
        if self.status in (OperationStatus.FAILED, OperationStatus.INTERRUPTED):
            if self.error is None:
                raise ConfigurationError("a failed operation requires an error")


@dataclass(frozen=True, slots=True)
class TemplateSpec:
    template_id: str
    name: str
    version: str
    services: tuple[ServiceInstanceSpec, ...]
    mcp_surfaces: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        require_identifier(self.template_id, "template_id")
        if not self.version.strip():
            raise ConfigurationError("template version must not be empty")
        EnvironmentSpec(self.name, self.services, self.mcp_surfaces)

    def as_environment_spec(self, *, name: str | None = None) -> EnvironmentSpec:
        return EnvironmentSpec(
            name=name or self.name,
            services=self.services,
            mcp_surfaces=self.mcp_surfaces,
        )


@dataclass(frozen=True, slots=True)
class Template:
    id: str
    name: str
    version: str
    status: TemplateStatus
    created_at: datetime
    updated_at: datetime
    failure_reason: str | None = None


@dataclass(frozen=True, slots=True)
class TemplateService:
    template_id: str
    instance_id: str
    plugin_id: str
    plugin_version: str
    status: ServiceStatus
    database_path: str
    seed: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class StoredTemplate:
    template: Template
    services: tuple[TemplateService, ...]
    mcp_surfaces: tuple[str, ...]

    def to_spec(self) -> TemplateSpec:
        return TemplateSpec(
            template_id=self.template.id,
            name=self.template.name,
            version=self.template.version,
            services=tuple(
                ServiceInstanceSpec(
                    instance_id=service.instance_id,
                    plugin_id=service.plugin_id,
                    plugin_version=service.plugin_version,
                    seed=service.seed,
                )
                for service in self.services
            ),
            mcp_surfaces=self.mcp_surfaces,
        )
