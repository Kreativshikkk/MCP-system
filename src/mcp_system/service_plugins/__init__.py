"""Built-in local service replica plugins."""

from .github import GitHubPlugin
from .gitlab import GitLabPlugin
from .jira import JiraPlugin
from .bitbucket import BitbucketPlugin
from .linear import LinearPlugin
from .youtrack import YouTrackPlugin

__all__ = ["BitbucketPlugin", "GitHubPlugin", "GitLabPlugin", "JiraPlugin", "LinearPlugin", "YouTrackPlugin"]
