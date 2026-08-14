"""Control-plane persistence contract."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, Sequence

from ..models import (
    Environment,
    EnvironmentSpec,
    EnvironmentStatus,
    OperationRecord,
    OperationStatus,
    ServiceInstance,
    ServiceStatus,
    StoredTemplate,
    Template,
    TemplateSpec,
    TemplateStatus,
)


class ControlPlaneStore(Protocol):
    def initialize(self) -> None: ...

    def recover_interrupted_provisioning(self, at: datetime) -> None: ...

    def create_environment_record(
        self,
        environment_id: str,
        spec: EnvironmentSpec,
        service_paths: dict[str, str],
        at: datetime,
        template_id: str | None = None,
        snapshot_id: str | None = None,
    ) -> None: ...

    def create_template_record(
        self,
        spec: TemplateSpec,
        service_paths: dict[str, str],
        at: datetime,
    ) -> None: ...

    def set_environment_status(
        self,
        environment_id: str,
        status: EnvironmentStatus,
        at: datetime,
        failure_reason: str | None = None,
    ) -> None: ...

    def set_service_status(
        self,
        environment_id: str,
        instance_id: str,
        status: ServiceStatus,
        at: datetime,
    ) -> None: ...

    def set_template_status(
        self,
        template_id: str,
        status: TemplateStatus,
        at: datetime,
        failure_reason: str | None = None,
    ) -> None: ...

    def set_template_service_status(
        self,
        template_id: str,
        instance_id: str,
        status: ServiceStatus,
        at: datetime,
    ) -> None: ...

    def get_environment(self, environment_id: str) -> Environment | None: ...

    def list_environments(self) -> Sequence[Environment]: ...

    def get_service(
        self, environment_id: str, instance_id: str
    ) -> ServiceInstance | None: ...

    def list_services(self, environment_id: str) -> Sequence[ServiceInstance]: ...

    def list_mcp_surfaces(self, environment_id: str) -> tuple[str, ...]: ...

    def get_template(self, template_id: str) -> StoredTemplate | None: ...

    def list_templates(self) -> Sequence[Template]: ...

    def begin_operation(self, operation: OperationRecord) -> int:
        """Insert the record; returns its monotonic per-environment seq."""
        ...

    def complete_operation(
        self,
        operation_id: str,
        status: OperationStatus,
        at: datetime,
        *,
        result: object | None = None,
        error: object | None = None,
    ) -> None: ...

    def list_operations(
        self,
        environment_id: str,
        *,
        service_instance_id: str | None = None,
        limit: int = 100,
        since_seq: int = 0,
        actor: str | None = None,
    ) -> Sequence[OperationRecord]: ...

    def operation_watermark(self, environment_id: str) -> int:
        """Highest assigned seq for the environment (0 when it has none)."""
        ...

    def get_operation_cursor(self, environment_id: str) -> tuple[int, str | None]: ...

    def set_environment_frozen(
        self, environment_id: str, frozen: bool, at: datetime
    ) -> None: ...

    def delete_environment_record(self, environment_id: str) -> None: ...
