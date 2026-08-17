"""Provider differential scenarios, golden cassettes, and semantic comparison."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
import time
from typing import Any, Mapping, Protocol, Sequence
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .errors import ServiceOperationError


@dataclass(frozen=True, slots=True)
class CapturedResponse:
    status: int
    headers: Mapping[str, str]
    body: Any

    def to_dict(self) -> dict[str, Any]:
        return {"status": self.status, "headers": dict(self.headers), "body": self.body}


class DifferentialTarget(Protocol):
    def request(self, method: str, path: str, *, query: Mapping[str, Any], body: Any | None) -> CapturedResponse: ...


class HTTPDifferentialTarget:
    def __init__(self, base_url: str, *, token: str, token_header: str = "PRIVATE-TOKEN", timeout: float = 30.0,
                 retry_statuses: Sequence[int] = (), retry_attempts: int = 1, retry_interval: float = 0.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.token_header = token_header
        self.timeout = timeout
        self.retry_statuses = frozenset(retry_statuses)
        self.retry_attempts = max(1, retry_attempts)
        self.retry_interval = retry_interval

    def request(self, method: str, path: str, *, query: Mapping[str, Any], body: Any | None) -> CapturedResponse:
        target = f"{self.base_url}{path}"
        if query:
            target += "?" + urlencode(query, doseq=True)
        multipart = isinstance(body, Mapping) and "__multipart__" in body
        if multipart:
            payload, multipart_type = _encode_multipart(body["__multipart__"])
        else:
            payload = json.dumps(body).encode() if body is not None else None
        headers = {self.token_header: self.token, "Accept": "application/json"}
        if payload is not None:
            headers["Content-Type"] = multipart_type if multipart else "application/json"
        for attempt in range(self.retry_attempts):
            request = Request(target, data=payload, headers=headers, method=method)
            try:
                response = urlopen(request, timeout=self.timeout)
            except HTTPError as exc:
                response = exc
            raw = response.read()
            content_type = response.headers.get("Content-Type", "").partition(";")[0]
            parsed: Any = None
            if raw:
                parsed = json.loads(raw) if content_type == "application/json" else raw.decode("utf-8", errors="replace")
            captured = CapturedResponse(response.status, {key.casefold(): value for key, value in response.headers.items()}, parsed)
            if response.status not in self.retry_statuses or attempt + 1 == self.retry_attempts:
                return captured
            time.sleep(self.retry_interval)
        raise AssertionError("unreachable retry loop")


class GraphQLDifferentialTarget(HTTPDifferentialTarget):
    """Canonical GraphQL-over-HTTP target; GraphQL errors remain body data."""

    def execute(self, query: str, variables: Mapping[str, Any]) -> CapturedResponse:
        return self.request(
            "POST", "", query={}, body={"query": query, "variables": dict(variables)}
        )


class LocalOperationTarget:
    """Adapt the audited local operation boundary to a differential target."""

    def __init__(self, system: Any, environment_id: str, instance_id: str, *, actor: str) -> None:
        self.system = system
        self.environment_id = environment_id
        self.instance_id = instance_id
        self.actor = actor

    def execute(self, operation: str, arguments: Mapping[str, Any]) -> CapturedResponse:
        try:
            result = self.system.invoke_service_operation(
                self.environment_id,
                self.instance_id,
                actor=self.actor,
                transport="differential",
                operation=operation,
                arguments=arguments,
            )
            return CapturedResponse(200, {}, result)
        except ServiceOperationError as exc:
            return CapturedResponse(
                exc.status_code,
                {},
                {"type": exc.error, "message": exc.message},
            )


def run_dual_scenario(
    scenario: Mapping[str, Any],
    real_target: DifferentialTarget,
    replica_target: LocalOperationTarget,
    *,
    variables: Mapping[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Run canonical provider requests and local operations with separate IDs."""
    seed = {**scenario.get("variables", {}), **dict(variables or {})}
    real_context = dict(seed)
    replica_context = dict(seed)
    real_records: list[dict[str, Any]] = []
    replica_records: list[dict[str, Any]] = []
    for step in scenario["steps"]:
        real_request = _render(step["real"], real_context)
        allowed = step.get("expect", {}).get("real_status", [200])
        allowed = [allowed] if isinstance(allowed, int) else allowed
        poll = step.get("poll", {})
        attempts = int(poll.get("attempts", 1))
        interval = float(poll.get("interval_seconds", 0))
        for attempt in range(attempts):
            real_response = real_target.request(
                real_request["method"], real_request["path"],
                query=real_request.get("query", {}), body=(
                    {"__multipart__": real_request["multipart"]}
                    if "multipart" in real_request else real_request.get("json")
                ),
            )
            if real_response.status not in allowed:
                if attempt + 1 < attempts:
                    time.sleep(interval)
                    continue
                break
            try:
                for expression in step.get("capture", {}).get("real", {}).values():
                    _json_path(real_response.body, expression)
                break
            except (KeyError, IndexError, TypeError):
                if attempt + 1 == attempts:
                    raise
                time.sleep(interval)
        if real_response.status not in allowed:
            raise AssertionError(
                f"{step['id']}: unexpected real status {real_response.status}: "
                f"{real_response.body!r}"
            )
        replica_call = _render(step["replica"], replica_context)
        replica_response = replica_target.execute(
            replica_call["operation"], replica_call.get("arguments", {})
        )
        expected_local = step.get("expect", {}).get("replica_status", 200)
        if replica_response.status != expected_local:
            raise AssertionError(
                f"{step['id']}: unexpected replica status {replica_response.status}: "
                f"{replica_response.body!r}"
            )
        captures = step.get("capture", {})
        for name, expression in captures.get("real", {}).items():
            real_context[name] = _json_path(real_response.body, expression)
        for name, expression in captures.get("replica", {}).items():
            replica_context[name] = _json_path(replica_response.body, expression)
        compare = step.get("compare", {})
        if compare.get("body", True):
            real_body = _select(real_response.body, compare.get("real_body"))
            replica_body = _select(replica_response.body, compare.get("replica_body"))
        else:
            real_body = replica_body = None
        comparison_response = CapturedResponse(
            real_response.status if compare.get("compare_status", False) else 200,
            real_response.headers,
            real_body,
        )
        local_comparison_response = CapturedResponse(
            replica_response.status if compare.get("compare_status", False) else 200,
            replica_response.headers,
            replica_body,
        )
        shared = {"id": step["id"], "compare": compare}
        real_records.append({**shared, "request": real_request, "response": comparison_response.to_dict()})
        replica_records.append({**shared, "request": replica_call, "response": local_comparison_response.to_dict()})
    return real_records, replica_records


def _select(value: Any, expression: str | None) -> Any:
    return _json_path(value, expression) if expression else value


def _encode_multipart(spec: Mapping[str, Any]) -> tuple[bytes, str]:
    boundary = "mcp-system-differential-boundary"
    chunks: list[bytes] = []
    for name, value in spec.get("fields", {}).items():
        chunks.extend([
            f"--{boundary}\r\n".encode(),
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(),
            str(value).encode(), b"\r\n",
        ])
    for name, value in spec.get("files", {}).items():
        chunks.extend([
            f"--{boundary}\r\n".encode(),
            f'Content-Disposition: form-data; name="{name}"; filename="{name.rsplit("/", 1)[-1]}"\r\n'.encode(),
            b"Content-Type: application/octet-stream\r\n\r\n",
            str(value).encode(), b"\r\n",
        ])
    chunks.append(f"--{boundary}--\r\n".encode())
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def run_scenario(scenario: Mapping[str, Any], target: DifferentialTarget, *, variables: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
    context: dict[str, Any] = {**scenario.get("variables", {}), **dict(variables or {})}
    records: list[dict[str, Any]] = []
    for step in scenario["steps"]:
        rendered = _render(step["request"], context)
        response = target.request(rendered["method"], rendered["path"], query=rendered.get("query", {}), body=rendered.get("json"))
        expected = step.get("expect", {})
        allowed = expected.get("status", [200])
        allowed = [allowed] if isinstance(allowed, int) else allowed
        if response.status not in allowed:
            raise AssertionError(f"{step['id']}: expected status {allowed}, got {response.status}: {response.body!r}")
        for variable, expression in step.get("capture", {}).items():
            context[variable] = _json_path(response.body, expression)
        records.append({"id": step["id"], "request": rendered, "response": response.to_dict(), "compare": step.get("compare", {})})
    return records


def compare_runs(real: Sequence[Mapping[str, Any]], replica: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    if [item["id"] for item in real] != [item["id"] for item in replica]:
        raise AssertionError("real and replica cassettes contain different steps")
    errors: list[str] = []
    warnings: list[str] = []
    for real_step, replica_step in zip(real, replica):
        step_id = real_step["id"]
        left, right = real_step["response"], replica_step["response"]
        if left["status"] != right["status"]:
            errors.append(f"{step_id}: status real={left['status']} replica={right['status']}")
        compare = replica_step.get("compare", {})
        if compare.get("shape", True):
            ignored = set(compare.get("ignore_shape_fields", ()))
            _compare_subset_shape(left.get("body"), right.get("body"), f"{step_id}.$", ignored, errors, warnings)
        for expression in compare.get("semantic_paths", ()):
            real_value = _json_path(left.get("body"), expression)
            replica_value = _json_path(right.get("body"), expression)
            if real_value != replica_value:
                errors.append(f"{step_id}:{expression} real={real_value!r} replica={replica_value!r}")
        for header in compare.get("headers", ()):
            key = header.casefold()
            if left.get("headers", {}).get(key) != right.get("headers", {}).get(key):
                errors.append(f"{step_id}:header {key} real={left.get('headers', {}).get(key)!r} replica={right.get('headers', {}).get(key)!r}")
    return {"passed": not errors, "errors": errors, "warnings": warnings}


def write_cassette(path: Path, records: Sequence[Mapping[str, Any]], *, metadata: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"metadata": dict(metadata), "steps": list(records)}, indent=2, sort_keys=True) + "\n")


def read_cassette(path: Path) -> list[dict[str, Any]]:
    return json.loads(path.read_text())["steps"]


_VARIABLE = re.compile(r"\$\{([A-Za-z0-9_.-]+)\}")


def _render(value: Any, context: Mapping[str, Any]) -> Any:
    if isinstance(value, str):
        full = _VARIABLE.fullmatch(value)
        if full:
            return context[full.group(1)]
        return _VARIABLE.sub(lambda match: str(context[match.group(1)]), value)
    if isinstance(value, list):
        return [_render(item, context) for item in value]
    if isinstance(value, dict):
        return {key: _render(item, context) for key, item in value.items()}
    return value


def _json_path(value: Any, expression: str) -> Any:
    if not expression.startswith("$."):
        raise ValueError(f"unsupported JSON path: {expression}")
    current = value
    for component in expression[2:].split("."):
        if isinstance(current, list):
            current = current[int(component)]
        else:
            current = current[component]
    return current


def _compare_subset_shape(real: Any, replica: Any, path: str, ignored: set[str], errors: list[str], warnings: list[str]) -> None:
    if isinstance(replica, dict):
        if not isinstance(real, dict):
            errors.append(f"{path}: real {type(real).__name__}, replica object")
            return
        for key, value in replica.items():
            if key in ignored:
                continue
            if key not in real:
                errors.append(f"{path}.{key}: field invented by replica")
            else:
                _compare_subset_shape(real[key], value, f"{path}.{key}", ignored, errors, warnings)
        extras = sorted(set(real) - set(replica) - ignored)
        if extras:
            warnings.append(f"{path}: real-only fields: {', '.join(extras)}")
        return
    if isinstance(replica, list):
        if not isinstance(real, list):
            errors.append(f"{path}: real {type(real).__name__}, replica array")
            return
        for index, value in enumerate(replica[: min(len(real), len(replica))]):
            _compare_subset_shape(real[index], value, f"{path}[{index}]", ignored, errors, warnings)
        return
    if replica is not None and real is not None and type(replica) is not type(real):
        errors.append(f"{path}: type real={type(real).__name__} replica={type(replica).__name__}")
