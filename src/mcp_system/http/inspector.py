"""Read-only HTTP API and static application for inspecting runtime traces."""

from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from ..core import MCPSystem
from ..errors import (
    ConfigurationError,
    EnvironmentNotFoundError,
    ServiceNotFoundError,
)
from ..inspector import InspectorProjectionRegistry
from ..models import Environment, OperationRecord
from .base import HTTPRequest, HTTPResponse


_ASSET_ROOT = Path(__file__).resolve().parent.parent / "inspector_assets"
_ASSETS = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/assets/app.css": ("app.css", "text/css; charset=utf-8"),
    "/assets/app.js": ("app.js", "text/javascript; charset=utf-8"),
}


class InspectorHTTPRouter:
    """Serves benchmark-author views without exposing them as agent tools."""

    def __init__(
        self,
        system: MCPSystem,
        projections: InspectorProjectionRegistry | None = None,
    ) -> None:
        self.system = system
        self.projections = projections or InspectorProjectionRegistry.builtins()

    def dispatch(self, request: HTTPRequest) -> HTTPResponse:
        if request.method.upper() != "GET":
            return self._json_response(405, {"message": "Method Not Allowed"})
        if request.path in _ASSETS:
            return self._asset_response(request.path)
        if request.path == "/api/health":
            return self._json_response(200, {"status": "ok"})
        if request.path == "/api/environments":
            return self._json_response(
                200,
                {
                    "environments": [
                        _environment_dict(environment)
                        for environment in self.system.list_environments()
                    ]
                },
            )

        parts = request.path.strip("/").split("/")
        if (
            len(parts) == 4
            and parts[0] == "api"
            and parts[1] == "environments"
            and parts[3] == "operations"
        ):
            return self._operations_response(unquote(parts[2]), request)
        if (
            len(parts) == 4
            and parts[0] == "api"
            and parts[1] == "environments"
            and parts[3] == "workbench"
        ):
            return self._workbench_response(unquote(parts[2]))
        return self._json_response(404, {"message": "Not Found"})

    def _workbench_response(self, environment_id: str) -> HTTPResponse:
        try:
            services = self.system.list_services(environment_id)
        except EnvironmentNotFoundError:
            return self._json_response(404, {"message": "Environment not found"})
        projected: list[dict[str, Any]] = []
        unsupported: list[dict[str, str]] = []
        for service in services:
            adapter = self.projections.resolve(
                service.plugin_id, service.plugin_version
            )
            if adapter is None:
                unsupported.append(
                    {
                        "instanceId": service.instance_id,
                        "pluginId": service.plugin_id,
                        "pluginVersion": service.plugin_version,
                    }
                )
                continue
            plugin = self.system.registry.resolve(
                service.plugin_id, service.plugin_version
            )
            git_data_plane = None
            if "git_data_plane" in plugin.manifest.capabilities:
                git_data_plane = self.system.open_git_data_plane(
                    environment_id, service.instance_id
                )
            with self.system.open_service_database(
                environment_id, service.instance_id
            ) as session:
                projection = adapter.project(session, git_data_plane)
            projected.append(
                {
                    "instanceId": service.instance_id,
                    "pluginId": service.plugin_id,
                    "pluginVersion": service.plugin_version,
                    "projection": projection,
                }
            )
        return self._json_response(
            200,
            {
                "environmentId": environment_id,
                "services": projected,
                "unsupportedServices": unsupported,
            },
        )

    def _operations_response(
        self, environment_id: str, request: HTTPRequest
    ) -> HTTPResponse:
        try:
            limit = _query_integer(request, "limit", default=250)
            service_values = request.query.get("service_instance_id", ())
            service_instance_id = service_values[-1] if service_values else None
            environment = self.system.require_environment(environment_id)
            operations = self.system.list_operations(
                environment_id,
                service_instance_id=service_instance_id,
                limit=limit,
            )
        except EnvironmentNotFoundError:
            return self._json_response(404, {"message": "Environment not found"})
        except ServiceNotFoundError:
            return self._json_response(404, {"message": "Service not found"})
        except ConfigurationError as exc:
            return self._json_response(400, {"message": str(exc)})
        return self._json_response(
            200,
            {
                "environment": _environment_dict(environment),
                "operations": [_operation_dict(operation) for operation in operations],
                "limit": limit,
                "truncated": len(operations) == limit,
            },
        )

    @staticmethod
    def _asset_response(path: str) -> HTTPResponse:
        filename, content_type = _ASSETS[path]
        try:
            body = (_ASSET_ROOT / filename).read_bytes()
        except OSError:
            return InspectorHTTPRouter._json_response(
                500, {"message": "Inspector asset is unavailable"}
            )
        return HTTPResponse(200, body, _headers(content_type))

    @staticmethod
    def _json_response(status: int, value: Any) -> HTTPResponse:
        return HTTPResponse(
            status,
            json.dumps(
                value,
                separators=(",", ":"),
                ensure_ascii=False,
                default=_json_default,
            ).encode(),
            _headers("application/json; charset=utf-8"),
        )


def _environment_dict(environment: Environment) -> dict[str, Any]:
    return {
        "id": environment.id,
        "name": environment.name,
        "status": environment.status,
        "templateId": environment.template_id,
        "snapshotId": environment.snapshot_id,
        "createdAt": _timestamp(environment.created_at),
        "updatedAt": _timestamp(environment.updated_at),
        "failureReason": environment.failure_reason,
    }


def _operation_dict(operation: OperationRecord) -> dict[str, Any]:
    return {
        "id": operation.id,
        "environmentId": operation.environment_id,
        "serviceInstanceId": operation.service_instance_id,
        "pluginId": operation.plugin_id,
        "actor": operation.actor,
        "transport": operation.transport,
        "operation": operation.operation,
        "request": operation.request,
        "status": operation.status,
        "result": operation.result,
        "error": operation.error,
        "startedAt": _timestamp(operation.started_at),
        "completedAt": (
            None if operation.completed_at is None else _timestamp(operation.completed_at)
        ),
    }


def _query_integer(request: HTTPRequest, name: str, *, default: int) -> int:
    values = request.query.get(name, ())
    if not values:
        return default
    try:
        return int(values[-1])
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be an integer") from exc


def _timestamp(value: datetime) -> str:
    return value.isoformat()


def _json_default(value: object) -> object:
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"{type(value).__name__} is not JSON serializable")


def _headers(content_type: str) -> dict[str, str]:
    return {
        "Content-Type": content_type,
        "Cache-Control": "no-store",
        "Content-Security-Policy": (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'"
        ),
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
    }
