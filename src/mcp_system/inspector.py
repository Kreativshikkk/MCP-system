"""Provider-neutral projections used only by the author-facing Inspector."""

from __future__ import annotations

from typing import Any, Mapping, Protocol

from .git_storage import GitServiceDataPlane
from .plugins import RelationalSession


class InspectorProjectionAdapter(Protocol):
    plugin_id: str
    plugin_version: str

    def project(
        self,
        session: RelationalSession,
        git_data_plane: GitServiceDataPlane | None,
    ) -> Mapping[str, Any]: ...


class InspectorProjectionRegistry:
    def __init__(self) -> None:
        self._adapters: dict[tuple[str, str], InspectorProjectionAdapter] = {}

    def register(self, adapter: InspectorProjectionAdapter) -> None:
        key = (adapter.plugin_id, adapter.plugin_version)
        if key in self._adapters:
            raise ValueError(
                f"Inspector adapter for {key[0]!r}@{key[1]!r} is already registered"
            )
        self._adapters[key] = adapter

    def resolve(
        self, plugin_id: str, plugin_version: str
    ) -> InspectorProjectionAdapter | None:
        return self._adapters.get((plugin_id, plugin_version))

    @classmethod
    def builtins(cls) -> "InspectorProjectionRegistry":
        from .service_plugins.github.inspector import GitHubInspectorAdapter
        from .service_plugins.gitlab.inspector import GitLabInspectorAdapter
        from .service_plugins.jira.inspector import JiraInspectorAdapter
        from .service_plugins.bitbucket.inspector import BitbucketInspectorAdapter
        from .service_plugins.linear.inspector import LinearInspectorAdapter
        from .service_plugins.youtrack.inspector import YouTrackInspectorAdapter

        registry = cls()
        registry.register(GitHubInspectorAdapter())
        registry.register(GitLabInspectorAdapter())
        registry.register(JiraInspectorAdapter())
        registry.register(BitbucketInspectorAdapter())
        registry.register(LinearInspectorAdapter())
        registry.register(YouTrackInspectorAdapter())
        return registry
