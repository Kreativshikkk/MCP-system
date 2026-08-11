"""Transport-neutral HTTP request, response, and authentication types."""

from __future__ import annotations

from dataclasses import dataclass, field
import hmac
from typing import Mapping, Protocol


@dataclass(frozen=True, slots=True)
class HTTPRequest:
    method: str
    path: str
    query: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    headers: Mapping[str, str] = field(default_factory=dict)
    body: bytes = b""


@dataclass(frozen=True, slots=True)
class HTTPResponse:
    status: int
    body: bytes = b""
    headers: Mapping[str, str] = field(default_factory=dict)


class ActorResolver(Protocol):
    def resolve(self, headers: Mapping[str, str]) -> str | None: ...


class FixedActorResolver:
    """Binds every request to one actor, optionally requiring a static token."""

    def __init__(self, actor: str, *, token: str | None = None) -> None:
        if not actor:
            raise ValueError("actor is required")
        if token == "":
            raise ValueError("token must not be empty")
        self.actor = actor
        self.token = token

    def resolve(self, headers: Mapping[str, str]) -> str | None:
        if self.token is None:
            return self.actor
        supplied = _bearer_token(headers.get("authorization", ""))
        if supplied is not None and hmac.compare_digest(supplied, self.token):
            return self.actor
        return None


class TokenActorResolver:
    """Maps opaque bearer tokens to actors without trusting request identity fields."""

    def __init__(self, actors_by_token: Mapping[str, str]) -> None:
        if not actors_by_token or any(not token or not actor for token, actor in actors_by_token.items()):
            raise ValueError("token-to-actor mapping must be non-empty")
        self.actors_by_token = dict(actors_by_token)

    def resolve(self, headers: Mapping[str, str]) -> str | None:
        supplied = _bearer_token(headers.get("authorization", ""))
        if supplied is None:
            return None
        for token, actor in self.actors_by_token.items():
            if hmac.compare_digest(supplied, token):
                return actor
        return None


def _bearer_token(value: str) -> str | None:
    scheme, separator, token = value.partition(" ")
    if not separator or scheme.casefold() not in ("bearer", "token") or not token:
        return None
    return token
