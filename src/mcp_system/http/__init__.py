"""Provider-compatible local HTTP interception middleware."""

from .base import (
    FixedActorResolver,
    HTTPRequest,
    HTTPResponse,
    TokenActorResolver,
)
from .github import GitHubHTTPRouter
from .gitlab import GitLabHTTPRouter, GitLabTokenActorResolver
from .inspector import InspectorHTTPRouter
from .server import MiddlewareHTTPServer, serve_http

__all__ = [
    "FixedActorResolver",
    "GitHubHTTPRouter",
    "GitLabHTTPRouter",
    "GitLabTokenActorResolver",
    "HTTPRequest",
    "HTTPResponse",
    "InspectorHTTPRouter",
    "MiddlewareHTTPServer",
    "TokenActorResolver",
    "serve_http",
]
