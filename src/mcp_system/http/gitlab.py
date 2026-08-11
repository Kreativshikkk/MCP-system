"""Pinned core GitLab REST v4 HTTP interception surface."""

from __future__ import annotations

from dataclasses import dataclass, field
import hmac
import json
import math
import re
from typing import Any, Mapping, Sequence
from urllib.parse import unquote

from ..core import MCPSystem
from ..service_plugins.gitlab import GitLabOperationError
from .base import ActorResolver, FixedActorResolver, HTTPRequest, HTTPResponse

OPENAPI_REVISION = "eb75d05715acad3d0ca93f7fbc699e7736470297"
MAX_JSON_BODY = 1024 * 1024


@dataclass(frozen=True, slots=True)
class Route:
    method: str
    path: str
    operation: str
    body_fields: tuple[str, ...] = ()
    required_body: tuple[str, ...] = ()
    query_fields: tuple[str, ...] = ()
    required_query: tuple[str, ...] = ()
    aliases: Mapping[str, str] | None = None
    status: int = 200
    empty: bool = False
    text: bool = False
    paginated: bool = False
    pattern: re.Pattern[str] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        expression = re.sub(r"\{([a-z_]+)\}", r"(?P<\1>[^/]+)", self.path)
        object.__setattr__(self, "pattern", re.compile(f"^{expression}$"))


class GitLabHTTPRouter:
    def __init__(self, system: MCPSystem, environment_id: str, *,
                 instance_id: str = "gitlab", actor: str | None = None,
                 actor_resolver: ActorResolver | None = None) -> None:
        if (actor is None) == (actor_resolver is None):
            raise ValueError("provide exactly one of actor or actor_resolver")
        service = system.control_plane.get_service(environment_id, instance_id)
        if service is None or service.plugin_id != "gitlab":
            raise ValueError("GitLab HTTP middleware requires a GitLab service")
        self.system = system
        self.environment_id = environment_id
        self.instance_id = instance_id
        self.actor_resolver = actor_resolver or FixedActorResolver(actor or "")

    def dispatch(self, request: HTTPRequest) -> HTTPResponse:
        headers = {key.casefold(): value for key, value in request.headers.items()}
        actor = self.actor_resolver.resolve(headers)
        if actor is None:
            return self._json(401, {"message": "401 Unauthorized"})
        if len(request.body) > MAX_JSON_BODY:
            return self._json(413, {"message": "413 Request Entity Too Large"})
        path = request.path
        if path == "/api/v4":
            path = "/"
        elif path.startswith("/api/v4/"):
            path = path[7:]
        route, path_arguments = self._match(request.method.upper(), path)
        if route is None:
            return self._json(404, {"message": "404 Not Found"})
        try:
            body = self._parse_body(request, route)
            arguments = self._arguments(route, path_arguments, request.query, body)
            result = self.system.invoke_service_operation(
                self.environment_id, self.instance_id, actor=actor,
                transport="http", operation=route.operation, arguments=arguments,
            )
            if route.empty:
                return HTTPResponse(route.status, headers=self._headers())
            if route.text:
                return HTTPResponse(route.status, str(result).encode(), {**self._headers(), "Content-Type": "text/plain; charset=utf-8"})
            if route.paginated or isinstance(result, list):
                if not isinstance(result, list):
                    raise GitLabHTTPRequestError(500, "paginated operation did not return a list")
                values, pagination_headers = self._paginate(result, request.query, request.path)
                return self._json(route.status, values, extra_headers=pagination_headers)
            return self._json(route.status, result)
        except GitLabOperationError as exc:
            return self._json(exc.status_code, exc.to_dict())
        except GitLabHTTPRequestError as exc:
            return self._json(exc.status, {"message": exc.message})

    @staticmethod
    def _match(method: str, path: str) -> tuple[Route | None, dict[str, str]]:
        for route in ROUTES:
            if route.method == method and (match := route.pattern.fullmatch(path)):
                return route, {key: unquote(value) for key, value in match.groupdict().items()}
        return None, {}

    @staticmethod
    def _parse_body(request: HTTPRequest, route: Route) -> Mapping[str, Any]:
        if not request.body:
            if route.required_body:
                raise GitLabHTTPRequestError(400, f"{next(iter(route.required_body))} is missing")
            return {}
        content_type = next((value for key, value in request.headers.items() if key.casefold() == "content-type"), "").partition(";")[0].casefold()
        if content_type != "application/json":
            raise GitLabHTTPRequestError(415, "Content-Type must be application/json")
        try:
            body = json.loads(request.body)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise GitLabHTTPRequestError(400, "Invalid JSON body") from exc
        if not isinstance(body, Mapping):
            raise GitLabHTTPRequestError(400, "Request body must be an object")
        unknown = set(body) - set(route.body_fields)
        if unknown:
            raise GitLabHTTPRequestError(400, f"unknown parameters: {', '.join(sorted(unknown))}")
        missing = set(route.required_body) - set(body)
        if missing:
            raise GitLabHTTPRequestError(400, f"{next(iter(sorted(missing)))} is missing")
        return body

    @staticmethod
    def _arguments(route: Route, path: Mapping[str, str], query: Mapping[str, tuple[str, ...]], body: Mapping[str, Any]) -> dict[str, Any]:
        aliases = {"id": "project", "noteable_id": "merge_request_iid", **dict(route.aliases or {})}
        arguments: dict[str, Any] = {}
        missing_query = set(route.required_query) - set(query)
        if missing_query:
            raise GitLabHTTPRequestError(400, f"{next(iter(sorted(missing_query)))} is missing")
        for key, value in path.items():
            target = aliases.get(key, key)
            arguments[target] = _coerce(target, value)
        for key in route.query_fields:
            if values := query.get(key):
                arguments[aliases.get(key, key)] = _coerce(key, values[-1])
        for key, value in body.items():
            arguments[aliases.get(key, key)] = value
        return arguments

    @staticmethod
    def _headers() -> dict[str, str]:
        return {"Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store", "X-GitLab-OpenAPI-Revision": OPENAPI_REVISION}

    @classmethod
    def _json(cls, status: int, value: Any, *, extra_headers: Mapping[str, str] | None = None) -> HTTPResponse:
        return HTTPResponse(status, json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode(), {**cls._headers(), **dict(extra_headers or {})})

    @staticmethod
    def _paginate(values: list[Any], query: Mapping[str, tuple[str, ...]], path: str) -> tuple[list[Any], dict[str, str]]:
        page = _positive_query_integer(query, "page", default=1, maximum=None)
        per_page = _positive_query_integer(query, "per_page", default=20, maximum=100)
        total = len(values)
        total_pages = math.ceil(total / per_page) if total else 1
        start = (page - 1) * per_page
        selected = values[start:start + per_page] if page <= total_pages else []
        next_page = page + 1 if page < total_pages else None
        previous_page = page - 1 if page > 1 and page <= total_pages + 1 else None
        links: list[str] = []
        if next_page:
            links.append(f'<{path}?page={next_page}&per_page={per_page}>; rel="next"')
        if previous_page:
            links.append(f'<{path}?page={previous_page}&per_page={per_page}>; rel="prev"')
        return selected, {
            "X-Page": str(page), "X-Per-Page": str(per_page),
            "X-Total": str(total), "X-Total-Pages": str(total_pages),
            "X-Next-Page": str(next_page or ""),
            "X-Prev-Page": str(previous_page or ""),
            "Link": ", ".join(links),
        }


class GitLabHTTPRequestError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


class GitLabTokenActorResolver:
    """Resolve GitLab PRIVATE-TOKEN or OAuth bearer credentials to actors."""

    def __init__(self, actors_by_token: Mapping[str, str]) -> None:
        if not actors_by_token or any(not token or not actor for token, actor in actors_by_token.items()):
            raise ValueError("token-to-actor mapping must be non-empty")
        self.actors_by_token = dict(actors_by_token)

    def resolve(self, headers: Mapping[str, str]) -> str | None:
        supplied = headers.get("private-token")
        if supplied is None:
            scheme, separator, token = headers.get("authorization", "").partition(" ")
            supplied = token if separator and scheme.casefold() in {"bearer", "oauth2"} else None
        if supplied is None:
            return None
        for token, actor in self.actors_by_token.items():
            if hmac.compare_digest(supplied, token):
                return actor
        return None


def _coerce(name: str, value: str) -> str | int | bool:
    if name in {"issue_iid", "merge_request_iid", "note_id", "pipeline_id", "job_id"}:
        try:
            return int(value)
        except ValueError as exc:
            raise GitLabHTTPRequestError(404, "404 Not Found") from exc
    if name in {"recursive", "straight"}:
        return value.casefold() in {"1", "true", "yes"}
    return value


def _positive_query_integer(query: Mapping[str, tuple[str, ...]], name: str, *, default: int, maximum: int | None) -> int:
    values = query.get(name)
    if not values:
        return default
    try:
        value = int(values[-1])
    except ValueError as exc:
        raise GitLabHTTPRequestError(400, f"{name} does not have a valid value") from exc
    if value <= 0 or (maximum is not None and value > maximum):
        raise GitLabHTTPRequestError(400, f"{name} does not have a valid value")
    return value


P = "/projects/{id}"
I = P + "/issues/{issue_iid}"
MR = P + "/merge_requests/{merge_request_iid}"
ROUTES: Sequence[Route] = (
    Route("GET", "/user", "get_current_user"),
    Route("GET", "/users", "list_users", query_fields=("username", "search")),
    Route("GET", "/groups/{group}", "get_group"),
    Route("GET", "/groups/{group}/members", "list_group_members"),
    Route("GET", "/groups/{group}/projects", "list_projects"),
    Route("GET", P, "get_project"),
    Route("GET", P + "/labels", "list_labels"),
    Route("POST", P + "/labels", "create_label", ("name", "color", "description"), ("name", "color"), status=201),
    Route("PUT", P + "/labels", "update_label", ("name", "new_name", "color", "description"), ("name",)),
    # GitLab CE 19.2 returns 204 here although the pinned 19.3-pre OpenAPI
    # document still advertises 200.
    Route("DELETE", P + "/labels", "delete_label", ("name",), ("name",), status=204, empty=True),
    Route("GET", P + "/issues", "list_issues", query_fields=("state",)),
    Route("POST", P + "/issues", "create_issue", ("title", "description", "labels", "assignee_ids"), ("title",), status=201),
    Route("GET", I, "get_issue"),
    Route("PUT", I, "update_issue", ("title", "description", "state_event", "labels", "add_labels", "remove_labels", "assignee_ids")),
    Route("DELETE", I, "delete_issue", empty=True, status=204),
    Route("GET", I + "/notes", "list_issue_notes"),
    Route("POST", I + "/notes", "create_issue_note", ("body",), ("body",), status=201),
    Route("GET", I + "/notes/{note_id}", "get_issue_note"),
    Route("PUT", I + "/notes/{note_id}", "update_issue_note", ("body",), ("body",)),
    Route("DELETE", I + "/notes/{note_id}", "delete_issue_note"),
    Route("GET", P + "/repository/tree", "get_repository_tree", query_fields=("ref", "path", "recursive")),
    Route("GET", P + "/repository/compare", "compare_repository", query_fields=("from", "to", "straight"), required_query=("from", "to"), aliases={"from": "from_ref", "to": "to_ref"}),
    Route("GET", P + "/repository/files/{file_path}", "get_repository_file", query_fields=("ref",), required_query=("ref",)),
    Route("POST", P + "/repository/files/{file_path}", "create_repository_file", ("branch", "content", "commit_message", "encoding"), ("branch", "content", "commit_message"), status=201),
    Route("PUT", P + "/repository/files/{file_path}", "update_repository_file", ("branch", "content", "commit_message", "encoding"), ("branch", "content", "commit_message")),
    Route("DELETE", P + "/repository/files/{file_path}", "delete_repository_file", ("branch", "commit_message"), ("branch", "commit_message"), status=204),
    Route("GET", P + "/repository/commits", "list_commits", query_fields=("ref_name",)),
    Route("POST", P + "/repository/commits", "create_repository_commit", ("branch", "commit_message", "actions", "start_branch", "start_sha"), ("branch", "commit_message", "actions")),
    Route("GET", P + "/repository/commits/{sha}/diff", "get_commit_diff"),
    Route("GET", P + "/repository/commits/{sha}", "get_commit"),
    Route("GET", P + "/repository/branches", "list_branches"),
    Route("POST", P + "/repository/branches", "create_branch", ("branch", "ref"), ("branch", "ref"), status=201),
    Route("GET", P + "/repository/branches/{branch}", "get_branch"),
    Route("DELETE", P + "/repository/branches/{branch}", "delete_branch", empty=True, status=204),
    Route("GET", P + "/repository/tags", "list_tags"),
    Route("POST", P + "/repository/tags", "create_tag", ("tag_name", "ref", "message"), ("tag_name", "ref"), status=201),
    Route("GET", P + "/repository/tags/{tag_name}", "get_tag"),
    Route("DELETE", P + "/repository/tags/{tag_name}", "delete_tag", empty=True, status=204),
    Route("GET", P + "/merge_requests", "list_merge_requests", query_fields=("state",)),
    Route("POST", P + "/merge_requests", "create_merge_request", ("title", "source_branch", "target_branch", "description", "reviewer_ids"), ("title", "source_branch", "target_branch"), status=201),
    Route("GET", MR + "/changes", "get_merge_request_changes"),
    Route("GET", MR + "/approvals", "get_merge_request_approvals"),
    Route("POST", MR + "/approve", "approve_merge_request", status=201),
    Route("POST", MR + "/unapprove", "unapprove_merge_request", status=201),
    Route("PUT", MR + "/merge", "merge_merge_request", ("sha", "merge_commit_message")),
    Route("GET", MR + "/notes", "list_merge_request_notes", aliases={"noteable_id": "merge_request_iid"}),
    Route("POST", MR + "/notes", "create_merge_request_note", ("body",), ("body",), aliases={"noteable_id": "merge_request_iid"}, status=201),
    Route("GET", MR + "/notes/{note_id}", "get_merge_request_note"),
    Route("PUT", MR + "/notes/{note_id}", "update_merge_request_note", ("body",), ("body",)),
    Route("DELETE", MR + "/notes/{note_id}", "delete_merge_request_note"),
    Route("GET", MR + "/discussions", "list_merge_request_discussions"),
    Route("POST", MR + "/discussions", "create_merge_request_discussion", ("body",), ("body",), status=201),
    Route("PUT", MR + "/discussions/{discussion_id}", "resolve_merge_request_discussion", ("resolved",), ("resolved",)),
    Route("POST", MR + "/discussions/{discussion_id}/notes", "create_merge_request_discussion_note", ("body",), ("body",), status=201),
    Route("GET", MR + "/pipelines", "list_merge_request_pipelines"),
    Route("POST", MR + "/pipelines", "create_merge_request_pipeline", status=201),
    Route("GET", MR, "get_merge_request"),
    Route("PUT", MR, "update_merge_request", ("title", "description", "state_event")),
    Route("GET", P + "/pipelines/latest", "get_latest_pipeline", query_fields=("ref",)),
    Route("GET", P + "/pipelines", "list_pipelines"),
    Route("POST", P + "/pipeline", "create_pipeline", ("ref",), ("ref",), status=201),
    Route("GET", P + "/pipelines/{pipeline_id}/jobs", "list_pipeline_jobs"),
    Route("POST", P + "/pipelines/{pipeline_id}/retry", "retry_pipeline", status=201),
    Route("POST", P + "/pipelines/{pipeline_id}/cancel", "cancel_pipeline", status=201),
    Route("GET", P + "/pipelines/{pipeline_id}", "get_pipeline"),
    Route("GET", P + "/jobs", "list_jobs"),
    Route("GET", P + "/jobs/{job_id}/trace", "get_job_trace", text=True),
    Route("POST", P + "/jobs/{job_id}/retry", "retry_job", status=201),
    Route("POST", P + "/jobs/{job_id}/cancel", "cancel_job", status=201),
    Route("POST", P + "/jobs/{job_id}/play", "play_job"),
    Route("GET", P + "/jobs/{job_id}", "get_job"),
    Route("GET", P + "/repository/commits/{sha}/statuses", "list_commit_statuses"),
    Route("POST", P + "/statuses/{sha}", "set_commit_status", ("state", "name", "target_url", "description"), ("state",)),
    Route("GET", P + "/releases", "list_releases"),
    Route("POST", P + "/releases", "create_release", ("tag_name", "name", "description", "released_at", "ref"), ("tag_name",), status=201),
    Route("GET", P + "/releases/{tag_name}", "get_release"),
    Route("PUT", P + "/releases/{tag_name}", "update_release", ("name", "description", "released_at")),
    Route("DELETE", P + "/releases/{tag_name}", "delete_release"),
)
