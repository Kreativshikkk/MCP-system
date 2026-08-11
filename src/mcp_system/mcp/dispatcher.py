"""MCP surface registry and transport-independent tool dispatcher."""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any, Mapping, Sequence

from ..core import MCPSystem
from ..errors import ServiceOperationError


class MCPProtocolError(Exception):
    def __init__(self, code: int, message: str, data: Any | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


class MCPToolInputError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    title: str
    description: str
    input_schema: Mapping[str, Any]
    operation: str
    argument_renames: Mapping[str, str]
    read_only: bool
    idempotent: bool = False
    destructive: bool = False

    def protocol_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "title": self.title,
            "description": self.description,
            "inputSchema": dict(self.input_schema),
            "annotations": {
                "readOnlyHint": self.read_only,
                "destructiveHint": self.destructive,
                "idempotentHint": self.idempotent,
                "openWorldHint": False,
            },
        }


@dataclass(frozen=True, slots=True)
class SurfaceSpec:
    surface_id: str
    plugin_id: str
    tools: tuple[ToolSpec, ...]


class SurfaceRegistry:
    def __init__(self) -> None:
        self._surfaces: dict[str, SurfaceSpec] = {}

    def register(self, surface: SurfaceSpec) -> None:
        if surface.surface_id in self._surfaces:
            raise ValueError(f"MCP surface {surface.surface_id!r} is already registered")
        self._surfaces[surface.surface_id] = surface

    def resolve(self, surface_id: str) -> SurfaceSpec:
        try:
            return self._surfaces[surface_id]
        except KeyError as exc:
            raise MCPProtocolError(
                -32603, f"Selected MCP surface is not registered: {surface_id}"
            ) from exc

    @classmethod
    def builtins(cls) -> "SurfaceRegistry":
        from .github_surface import github_rest_v3_surface
        from .jira_surface import jira_rest_v3_surface
        from .gitlab_surface import gitlab_rest_v4_surface
        from .bitbucket_surface import bitbucket_cloud_v2_surface
        from .linear_surface import linear_graphql_surface
        from .youtrack_surface import youtrack_rest_surface

        registry = cls()
        registry.register(github_rest_v3_surface())
        registry.register(gitlab_rest_v4_surface())
        registry.register(jira_rest_v3_surface())
        registry.register(bitbucket_cloud_v2_surface())
        registry.register(linear_graphql_surface())
        registry.register(youtrack_rest_surface())
        return registry


class MCPDispatcher:
    """Routes selected MCP tools to one isolated environment and actor."""

    def __init__(
        self,
        system: MCPSystem,
        environment_id: str,
        *,
        actor: str,
        bindings: Mapping[str, str] | None = None,
        surfaces: SurfaceRegistry | None = None,
    ) -> None:
        self.system = system
        self.environment_id = environment_id
        self.actor = actor
        self.bindings = dict(bindings or {})
        self.surfaces = surfaces or SurfaceRegistry.builtins()
        self._tools = self._resolve_tools()

    def list_tools(self) -> list[dict[str, Any]]:
        return [tool.protocol_dict() for tool, _ in self._tools.values()]

    def call_tool(self, name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        try:
            tool, instance_id = self._tools[name]
        except KeyError as exc:
            raise MCPProtocolError(-32602, f"Unknown tool: {name}") from exc
        try:
            _validate_value(arguments, tool.input_schema, "arguments")
            provider_arguments = {
                tool.argument_renames.get(key, key): value
                for key, value in arguments.items()
            }
            result = self.system.invoke_service_operation(
                self.environment_id,
                instance_id,
                actor=self.actor,
                transport="mcp",
                operation=tool.operation,
                arguments=provider_arguments,
            )
            structured = {"result": result}
            return {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(
                            structured, sort_keys=True, separators=(",", ":")
                        ),
                    }
                ],
                "structuredContent": structured,
                "isError": False,
            }
        except (MCPToolInputError, ServiceOperationError) as exc:
            if isinstance(exc, ServiceOperationError):
                error = {
                    "type": exc.error,
                    "status": exc.status_code,
                    "message": exc.message,
                }
            else:
                error = {"type": "invalid_arguments", "message": str(exc)}
            structured = {"error": error}
            return {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(
                            structured, sort_keys=True, separators=(",", ":")
                        ),
                    }
                ],
                "structuredContent": structured,
                "isError": True,
            }

    def _resolve_tools(self) -> dict[str, tuple[ToolSpec, str]]:
        selected = self.system.list_mcp_surfaces(self.environment_id)
        tools: dict[str, tuple[ToolSpec, str]] = {}
        for surface_id in selected:
            surface = self.surfaces.resolve(surface_id)
            instance_id = self.bindings.get(surface_id)
            if instance_id is None:
                candidates = [
                    service.instance_id
                    for service in self.system.list_services(self.environment_id)
                    if service.plugin_id == surface.plugin_id
                ]
                if len(candidates) != 1:
                    raise MCPProtocolError(
                        -32603,
                        f"Surface {surface_id!r} requires one explicit binding; "
                        f"found {len(candidates)} {surface.plugin_id!r} services",
                    )
                instance_id = candidates[0]
                self.bindings[surface_id] = instance_id
            service = self.system.control_plane.get_service(
                self.environment_id, instance_id
            )
            if service is None or service.plugin_id != surface.plugin_id:
                raise MCPProtocolError(
                    -32603,
                    f"Surface {surface_id!r} requires plugin {surface.plugin_id!r}",
                )
            for tool in surface.tools:
                if tool.name in tools:
                    raise MCPProtocolError(
                        -32603, f"Duplicate MCP tool name: {tool.name}"
                    )
                tools[tool.name] = (tool, instance_id)
        return tools


def _validate_value(value: Any, schema: Mapping[str, Any], path: str) -> None:
    expected = schema.get("type")
    allowed_types: Sequence[str] = (
        (expected,) if isinstance(expected, str) else tuple(expected or ())
    )
    if allowed_types and not any(_matches_type(value, item) for item in allowed_types):
        raise MCPToolInputError(
            f"{path} must be {' or '.join(allowed_types)}"
        )
    if "enum" in schema and value not in schema["enum"]:
        raise MCPToolInputError(f"{path} must be one of {schema['enum']}")
    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0):
            raise MCPToolInputError(f"{path} must not be empty")
    if isinstance(value, int) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            raise MCPToolInputError(f"{path} must be >= {schema['minimum']}")
    if isinstance(value, list):
        item_schema = schema.get("items")
        if item_schema:
            for index, item in enumerate(value):
                _validate_value(item, item_schema, f"{path}[{index}]")
    if isinstance(value, Mapping):
        properties = schema.get("properties", {})
        required = schema.get("required", ())
        for key in required:
            if key not in value:
                raise MCPToolInputError(f"{path}.{key} is required")
        additional = schema.get("additionalProperties", True)
        for key, item in value.items():
            if not isinstance(key, str):
                raise MCPToolInputError(f"{path} keys must be strings")
            if key in properties:
                _validate_value(item, properties[key], f"{path}.{key}")
            elif additional is False:
                raise MCPToolInputError(f"unknown argument: {key}")
            elif isinstance(additional, Mapping):
                _validate_value(item, additional, f"{path}.{key}")


def _matches_type(value: Any, expected: str) -> bool:
    return {
        "object": isinstance(value, Mapping),
        "array": isinstance(value, list),
        "string": isinstance(value, str),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "number": isinstance(value, (int, float)) and not isinstance(value, bool),
        "boolean": isinstance(value, bool),
        "null": value is None,
    }.get(expected, False)
