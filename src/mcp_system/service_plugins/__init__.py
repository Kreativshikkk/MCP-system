"""Built-in local service replica plugins."""

from .github import GitHubPlugin
from .gitlab import GitLabPlugin
from .jira import JiraPlugin
from .bitbucket import BitbucketPlugin
from .linear import LinearPlugin
from .youtrack import YouTrackPlugin
from ..plugins import PluginRegistry, ServicePlugin

def builtin_plugins() -> tuple[ServicePlugin, ...]:
    """Return fresh instances of every plugin shipped with MCPSystem."""
    return (GitHubPlugin(), GitLabPlugin(), JiraPlugin(), BitbucketPlugin(), LinearPlugin(), YouTrackPlugin())


def builtin_plugin_registry() -> PluginRegistry:
    registry = PluginRegistry()
    for plugin in builtin_plugins():
        registry.register(plugin)
    return registry


__all__ = ["BitbucketPlugin", "GitHubPlugin", "GitLabPlugin", "JiraPlugin", "LinearPlugin", "YouTrackPlugin", "builtin_plugins", "builtin_plugin_registry"]
