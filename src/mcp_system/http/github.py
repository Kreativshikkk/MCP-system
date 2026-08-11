"""GitHub REST-compatible router executing entirely inside MCPSystem."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import re
from typing import Any, Mapping, Sequence
from urllib.parse import unquote

from ..core import MCPSystem
from ..service_plugins.github import GitHubOperationError
from .base import ActorResolver, FixedActorResolver, HTTPRequest, HTTPResponse


API_VERSION = "2026-03-10"
MAX_JSON_BODY = 1024 * 1024


@dataclass(frozen=True, slots=True)
class Route:
    method: str
    path: str
    operation: str
    body_fields: tuple[str, ...] = ()
    required_body: tuple[str, ...] = ()
    query_fields: tuple[str, ...] = ()
    aliases: Mapping[str, str] | None = None
    status: int = 200
    empty_response: bool = False
    pattern: re.Pattern[str] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        expression = re.sub(
            r"\{([a-z_]+)\}", r"(?P<\1>[^/]+)", self.path
        )
        object.__setattr__(self, "pattern", re.compile(f"^{expression}$"))


class GitHubHTTPRouter:
    """Maps selected GitHub REST routes to one local service instance."""

    def __init__(
        self,
        system: MCPSystem,
        environment_id: str,
        *,
        instance_id: str = "github",
        actor: str | None = None,
        actor_resolver: ActorResolver | None = None,
    ) -> None:
        if (actor is None) == (actor_resolver is None):
            raise ValueError("provide exactly one of actor or actor_resolver")
        self.system = system
        self.environment_id = environment_id
        self.instance_id = instance_id
        self.actor_resolver = actor_resolver or FixedActorResolver(actor or "")
        service = system.control_plane.get_service(environment_id, instance_id)
        if service is None or service.plugin_id != "github":
            raise ValueError("GitHub HTTP middleware requires a GitHub service")

    def dispatch(self, request: HTTPRequest) -> HTTPResponse:
        headers = {key.casefold(): value for key, value in request.headers.items()}
        actor = self.actor_resolver.resolve(headers)
        if actor is None:
            return self._json_response(401, {"message": "Bad credentials"})
        if len(request.body) > MAX_JSON_BODY:
            return self._json_response(413, {"message": "Request body is too large"})

        path = request.path
        if path == "/api/v3":
            path = "/"
        elif path.startswith("/api/v3/"):
            path = path[7:]
        route, path_arguments = self._match(request.method.upper(), path)
        if route is None:
            return self._json_response(404, {"message": "Not Found"})
        try:
            body = self._parse_body(request, route)
            arguments = self._arguments(route, path_arguments, request.query, body)
            result = self.system.invoke_service_operation(
                self.environment_id,
                self.instance_id,
                actor=actor,
                transport="http",
                operation=route.operation,
                arguments=arguments,
            )
            if route.operation == "is_merged":
                return HTTPResponse(204 if result else 404, headers=self._headers())
            if route.empty_response:
                return HTTPResponse(route.status, headers=self._headers())
            return self._json_response(route.status, result)
        except GitHubOperationError as exc:
            return self._json_response(exc.status_code, exc.to_dict())
        except HTTPRequestError as exc:
            return self._json_response(exc.status, {"message": exc.message})

    @staticmethod
    def _match(method: str, path: str) -> tuple[Route | None, dict[str, str]]:
        for route in ROUTES:
            if route.method != method:
                continue
            match = route.pattern.fullmatch(path)  # type: ignore[attr-defined]
            if match:
                return route, {key: unquote(value) for key, value in match.groupdict().items()}
        return None, {}

    @staticmethod
    def _parse_body(request: HTTPRequest, route: Route) -> Mapping[str, Any]:
        if not request.body:
            if route.required_body:
                raise HTTPRequestError(
                    422,
                    f"Missing request fields: {', '.join(sorted(route.required_body))}",
                )
            return {}
        content_type = next(
            (value for key, value in request.headers.items() if key.casefold() == "content-type"),
            "",
        ).partition(";")[0].strip().casefold()
        if content_type not in ("application/json", "application/vnd.github+json"):
            raise HTTPRequestError(415, "Content-Type must be application/json")
        try:
            value = json.loads(request.body)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HTTPRequestError(400, "Invalid JSON body") from exc
        if not isinstance(value, Mapping):
            raise HTTPRequestError(422, "Request body must be an object")
        unknown = set(value) - set(route.body_fields)
        if unknown:
            raise HTTPRequestError(
                422, f"Unknown request fields: {', '.join(sorted(unknown))}"
            )
        missing = set(route.required_body) - set(value)
        if missing:
            raise HTTPRequestError(
                422, f"Missing request fields: {', '.join(sorted(missing))}"
            )
        return value

    @staticmethod
    def _arguments(
        route: Route,
        path: Mapping[str, str],
        query: Mapping[str, tuple[str, ...]],
        body: Mapping[str, Any],
    ) -> dict[str, Any]:
        aliases = {
            "org": "organization",
            "repo": "repository",
            "commit_id": "commit_sha",
            **dict(route.aliases or {}),
        }
        arguments: dict[str, Any] = {}
        for key, value in path.items():
            target = aliases.get(key, key)
            arguments[target] = _path_value(target, value)
        for key in route.query_fields:
            values = query.get(key)
            if values:
                arguments[aliases.get(key, key)] = values[-1]
        for key, value in body.items():
            arguments[aliases.get(key, key)] = value
        return arguments

    @staticmethod
    def _headers() -> dict[str, str]:
        return {
            "Content-Type": "application/json; charset=utf-8",
            "X-GitHub-Api-Version-Selected": API_VERSION,
            "Cache-Control": "no-store",
        }

    @classmethod
    def _json_response(cls, status: int, value: Any) -> HTTPResponse:
        return HTTPResponse(
            status,
            json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode(),
            cls._headers(),
        )


class HTTPRequestError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def _path_value(name: str, value: str) -> str | int:
    if name in ("issue_number", "pull_number", "run_id", "job_id"):
        try:
            number = int(value)
        except ValueError as exc:
            raise HTTPRequestError(404, "Not Found") from exc
        if number <= 0:
            raise HTTPRequestError(404, "Not Found")
        return number
    return value


ROUTES: Sequence[Route] = (
    Route("GET", "/user", "get_authenticated_user"),
    Route("GET", "/users/{username}", "get_user"),
    Route("GET", "/orgs/{org}", "get_organization"),
    Route("GET", "/orgs/{org}/members", "list_organization_members"),
    Route("GET", "/orgs/{org}/repos", "list_repositories"),
    Route("POST", "/orgs/{org}/repos", "create_repository", ("name", "description", "private"), ("name",), status=201),
    Route("GET", "/repos/{owner}/{repo}", "get_repository"),
    Route("PATCH", "/repos/{owner}/{repo}", "update_repository", ("name", "description", "private", "archived", "default_branch")),
    Route("GET", "/repos/{owner}/{repo}/branches", "list_branches"),
    Route("GET", "/repos/{owner}/{repo}/commits", "list_commits"),
    Route("GET", "/repos/{owner}/{repo}/labels", "list_labels"),
    Route("POST", "/repos/{owner}/{repo}/labels", "create_label", ("name", "color", "description"), ("name",), status=201),
    Route("GET", "/repos/{owner}/{repo}/issues", "list_issues", query_fields=("state",)),
    Route("POST", "/repos/{owner}/{repo}/issues", "create_issue", ("title", "body", "labels", "assignees"), ("title",), status=201),
    Route("GET", "/repos/{owner}/{repo}/issues/{issue_number}", "get_issue"),
    Route("PATCH", "/repos/{owner}/{repo}/issues/{issue_number}", "update_issue", ("title", "body", "state", "state_reason", "labels", "assignees")),
    Route("GET", "/repos/{owner}/{repo}/issues/{issue_number}/comments", "list_comments"),
    Route("POST", "/repos/{owner}/{repo}/issues/{issue_number}/comments", "create_comment", ("body",), ("body",), status=201),
    Route("GET", "/repos/{owner}/{repo}/issues/{issue_number}/labels", "list_issue_labels"),
    Route("POST", "/repos/{owner}/{repo}/issues/{issue_number}/labels", "add_issue_labels", ("labels",), ("labels",)),
    Route("PUT", "/repos/{owner}/{repo}/issues/{issue_number}/labels", "set_issue_labels", ("labels",), ("labels",)),
    Route("DELETE", "/repos/{owner}/{repo}/issues/{issue_number}/labels", "remove_all_issue_labels", empty_response=True, status=204),
    Route("DELETE", "/repos/{owner}/{repo}/issues/{issue_number}/labels/{name}", "remove_issue_label"),
    Route("POST", "/repos/{owner}/{repo}/issues/{issue_number}/assignees", "add_assignees", ("assignees",), ("assignees",), status=201),
    Route("DELETE", "/repos/{owner}/{repo}/issues/{issue_number}/assignees", "remove_assignees", ("assignees",), ("assignees",)),
    Route("GET", "/repos/{owner}/{repo}/pulls", "list_pull_requests", query_fields=("state",)),
    Route("POST", "/repos/{owner}/{repo}/pulls", "create_pull_request", ("title", "head", "base", "body", "draft"), ("title", "head", "base"), status=201),
    Route("GET", "/repos/{owner}/{repo}/pulls/{pull_number}", "get_pull_request"),
    Route("PATCH", "/repos/{owner}/{repo}/pulls/{pull_number}", "update_pull_request", ("title", "body", "state", "base")),
    Route("GET", "/repos/{owner}/{repo}/pulls/{pull_number}/merge", "is_merged", status=204),
    Route("PUT", "/repos/{owner}/{repo}/pulls/{pull_number}/merge", "merge_pull_request_api", ("commit_title", "commit_message", "sha", "merge_method")),
    Route("GET", "/repos/{owner}/{repo}/pulls/{pull_number}/requested_reviewers", "list_requested_reviewers"),
    Route("POST", "/repos/{owner}/{repo}/pulls/{pull_number}/requested_reviewers", "request_reviewers", ("reviewers",), ("reviewers",), status=201),
    Route("DELETE", "/repos/{owner}/{repo}/pulls/{pull_number}/requested_reviewers", "remove_requested_reviewers", ("reviewers",), ("reviewers",)),
    Route("GET", "/repos/{owner}/{repo}/pulls/{pull_number}/reviews", "list_reviews"),
    Route("POST", "/repos/{owner}/{repo}/pulls/{pull_number}/reviews", "create_review", ("event", "body", "commit_id"), ("event",), status=200),
    Route("GET", "/repos/{owner}/{repo}/pulls/{pull_number}/comments", "list_review_comments"),
    Route("POST", "/repos/{owner}/{repo}/pulls/{pull_number}/comments", "create_review_comment", ("body", "path", "commit_id", "line", "side"), ("body", "commit_id", "path"), status=201),
    Route("GET", "/repos/{owner}/{repo}/actions/runs", "list_workflow_runs"),
    Route("GET", "/repos/{owner}/{repo}/actions/runs/{run_id}/jobs", "list_workflow_jobs"),
    Route("GET", "/repos/{owner}/{repo}/actions/jobs/{job_id}", "get_workflow_job"),
    Route("GET", "/repos/{owner}/{repo}/releases", "list_releases"),
    Route("POST", "/repos/{owner}/{repo}/releases", "create_release", ("tag_name", "target_commitish", "name", "body", "draft", "prerelease"), ("tag_name",), status=201),
)
