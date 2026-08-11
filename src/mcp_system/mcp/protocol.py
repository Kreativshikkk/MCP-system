"""Stateful MCP JSON-RPC server and newline-delimited stdio transport."""

from __future__ import annotations

import json
import sys
from typing import Any, Mapping, TextIO

from .dispatcher import MCPDispatcher, MCPProtocolError


LATEST_PROTOCOL_VERSION = "2025-11-25"
SUPPORTED_PROTOCOL_VERSIONS = (LATEST_PROTOCOL_VERSION, "2025-06-18")


class MCPJSONRPCServer:
    def __init__(self, dispatcher: MCPDispatcher) -> None:
        self.dispatcher = dispatcher
        self.state = "new"
        self.protocol_version: str | None = None

    def handle(self, message: Any) -> dict[str, Any] | None:
        request_id: Any = None
        is_notification = isinstance(message, Mapping) and "id" not in message
        try:
            if not isinstance(message, Mapping):
                raise MCPProtocolError(-32600, "Invalid Request")
            request_id = message.get("id")
            if message.get("jsonrpc") != "2.0" or not isinstance(
                message.get("method"), str
            ):
                raise MCPProtocolError(-32600, "Invalid Request")
            method = message["method"]
            params = message.get("params", {})
            if not isinstance(params, Mapping):
                raise MCPProtocolError(-32602, "params must be an object")

            if "id" not in message:
                self._handle_notification(method, params)
                return None
            result = self._handle_request(method, params)
            return {"jsonrpc": "2.0", "id": request_id, "result": result}
        except MCPProtocolError as exc:
            if is_notification:
                return None
            error: dict[str, Any] = {"code": exc.code, "message": exc.message}
            if exc.data is not None:
                error["data"] = exc.data
            return {"jsonrpc": "2.0", "id": request_id, "error": error}
        except Exception:
            if is_notification:
                return None
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32603, "message": "Internal error"},
            }

    def _handle_request(
        self, method: str, params: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        if method == "initialize":
            return self._initialize(params)
        if method == "ping":
            return {}
        if self.state != "ready":
            raise MCPProtocolError(-32002, "Server is not initialized")
        if method == "tools/list":
            cursor = params.get("cursor")
            if cursor is not None:
                raise MCPProtocolError(-32602, "Pagination cursor is not supported")
            return {"tools": self.dispatcher.list_tools()}
        if method == "tools/call":
            name = params.get("name")
            arguments = params.get("arguments", {})
            if not isinstance(name, str) or not name:
                raise MCPProtocolError(-32602, "tools/call requires a tool name")
            if not isinstance(arguments, Mapping):
                raise MCPProtocolError(-32602, "tool arguments must be an object")
            return self.dispatcher.call_tool(name, arguments)
        raise MCPProtocolError(-32601, f"Method not found: {method}")

    def _initialize(self, params: Mapping[str, Any]) -> Mapping[str, Any]:
        if self.state != "new":
            raise MCPProtocolError(-32600, "Server is already initialized")
        requested = params.get("protocolVersion")
        client_info = params.get("clientInfo")
        capabilities = params.get("capabilities")
        if not isinstance(requested, str):
            raise MCPProtocolError(-32602, "protocolVersion is required")
        if not isinstance(client_info, Mapping) or not isinstance(
            client_info.get("name"), str
        ):
            raise MCPProtocolError(-32602, "valid clientInfo is required")
        if not isinstance(capabilities, Mapping):
            raise MCPProtocolError(-32602, "capabilities must be an object")
        negotiated = (
            requested
            if requested in SUPPORTED_PROTOCOL_VERSIONS
            else LATEST_PROTOCOL_VERSION
        )
        self.protocol_version = negotiated
        self.state = "initializing"
        return {
            "protocolVersion": negotiated,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {
                "name": "mcp-system",
                "title": "MCPSystem Isolated Service Runtime",
                "version": "0.1.0",
                "description": "Persistent local service replicas for agent evaluation",
            },
            "instructions": (
                "Tools operate only on the configured isolated environment as the "
                f"bound actor {self.dispatcher.actor!r}."
            ),
        }

    def _handle_notification(
        self, method: str, params: Mapping[str, Any]
    ) -> None:
        if method == "notifications/initialized":
            if self.state != "initializing":
                raise MCPProtocolError(-32600, "Unexpected initialized notification")
            self.state = "ready"
            return
        if method in ("notifications/cancelled", "notifications/progress"):
            return
        # Unknown notifications are ignored per JSON-RPC semantics.


def run_stdio(
    server: MCPJSONRPCServer,
    input_stream: TextIO | None = None,
    output_stream: TextIO | None = None,
) -> None:
    input_stream = input_stream or sys.stdin
    output_stream = output_stream or sys.stdout
    for line in input_stream:
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            response = {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32700, "message": "Parse error"},
            }
        else:
            if isinstance(message, list):
                response = {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32600, "message": "Batching is not supported"},
                }
            else:
                response = server.handle(message)
        if response is not None:
            output_stream.write(
                json.dumps(response, separators=(",", ":"), ensure_ascii=False) + "\n"
            )
            output_stream.flush()
