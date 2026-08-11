"""Injectable runtime dependencies used to make tests and replicas deterministic."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Protocol
from uuid import uuid4


class Clock(Protocol):
    def now(self) -> datetime: ...


class IdGenerator(Protocol):
    def new_environment_id(self) -> str: ...


class OperationIdGenerator(Protocol):
    def new_operation_id(self) -> str: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class UUIDGenerator:
    def new_environment_id(self) -> str:
        return uuid4().hex

    def new_operation_id(self) -> str:
        return uuid4().hex
