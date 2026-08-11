"""GitHub service plugin bootstrap and manifest."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from ...errors import ConfigurationError
from ...plugins import Migration, PluginManifest, RelationalSession
from .schema import github_migrations


_DEFAULT_CREATED_AT = "2026-01-01T00:00:00+00:00"

DEFAULT_LABELS: tuple[tuple[str, str, str], ...] = (
    ("bug", "d73a4a", "Something isn't working"),
    ("documentation", "0075ca", "Improvements or additions to documentation"),
    ("duplicate", "cfd3d7", "This issue or pull request already exists"),
    ("enhancement", "a2eeef", "New feature or request"),
    ("good first issue", "7057ff", "Good for newcomers"),
    ("help wanted", "008672", "Extra attention is needed"),
    ("invalid", "e4e669", "This doesn't seem right"),
    ("question", "d876e3", "Further information is requested"),
    ("wontfix", "ffffff", "This will not be worked on"),
)


@dataclass(frozen=True, slots=True)
class GitHubPlugin:
    manifest: PluginManifest = PluginManifest(
        plugin_id="github",
        version="0.1.0",
        display_name="GitHub REST API replica",
        capabilities=(
            "organizations",
            "users",
            "repositories",
            "git_data_plane",
            "commits",
            "branches",
            "issues",
            "labels",
            "comments",
            "pull_requests",
            "reviews",
            "actions",
            "releases",
        ),
        contract_source="https://github.com/github/rest-api-description",
        contract_revision="5e28810649ba41b5483753ba74f976f83856a504",
        api_version="2026-03-10",
    )

    def migrations(self, storage_kind: str) -> Sequence[Migration]:
        return github_migrations(storage_kind)

    def validate_bootstrap(self, config: Mapping[str, Any]) -> None:
        _require_keys(
            config,
            {"organization", "users", "repositories", "created_at"},
            "github bootstrap",
        )
        organization = _mapping(config.get("organization"), "organization")
        _require_keys(organization, {"login", "name"}, "organization")
        _non_empty_string(organization.get("login"), "organization.login")
        if "name" in organization:
            _optional_string(organization["name"], "organization.name")

        users = _list(config.get("users"), "users")
        if not users:
            raise ConfigurationError("github bootstrap requires at least one user")
        user_logins: set[str] = set()
        admin_count = 0
        for index, raw_user in enumerate(users):
            context = f"users[{index}]"
            user = _mapping(raw_user, context)
            _require_keys(user, {"login", "name", "email", "role", "type"}, context)
            login = _non_empty_string(user.get("login"), f"{context}.login")
            normalized = login.casefold()
            if normalized in user_logins:
                raise ConfigurationError(f"duplicate GitHub user login: {login}")
            user_logins.add(normalized)
            role = user.get("role", "member")
            if role not in ("admin", "member"):
                raise ConfigurationError(f"{context}.role must be admin or member")
            admin_count += role == "admin"
            if user.get("type", "User") not in ("User", "Bot"):
                raise ConfigurationError(f"{context}.type must be User or Bot")
            for field in ("name", "email"):
                if field in user:
                    _optional_string(user[field], f"{context}.{field}")
        if admin_count == 0:
            raise ConfigurationError("github bootstrap requires an organization admin")

        repositories = _list(config.get("repositories", []), "repositories")
        repository_names: set[str] = set()
        for index, raw_repository in enumerate(repositories):
            context = f"repositories[{index}]"
            repository = _mapping(raw_repository, context)
            _require_keys(
                repository,
                {
                    "name",
                    "description",
                    "private",
                    "default_branch",
                    "default_labels",
                },
                context,
            )
            name = _non_empty_string(repository.get("name"), f"{context}.name")
            normalized = name.casefold()
            if normalized in repository_names:
                raise ConfigurationError(f"duplicate GitHub repository name: {name}")
            repository_names.add(normalized)
            if "description" in repository:
                _optional_string(repository["description"], f"{context}.description")
            _non_empty_string(
                repository.get("default_branch", "main"),
                f"{context}.default_branch",
            )
            for field in ("private", "default_labels"):
                if field in repository and not isinstance(repository[field], bool):
                    raise ConfigurationError(f"{context}.{field} must be a boolean")

        if "created_at" in config:
            _non_empty_string(config["created_at"], "created_at")

    def seed(self, session: RelationalSession, config: Mapping[str, Any]) -> None:
        self.validate_bootstrap(config)
        organization = config["organization"]
        users = config["users"]
        repositories = config.get("repositories", [])
        created_at = config.get("created_at", _DEFAULT_CREATED_AT)

        organization_login = organization["login"]
        session.execute(
            """
            INSERT INTO github_organizations(id, login, name, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (1, organization_login, organization.get("name"), created_at),
        )

        user_rows: list[tuple[Any, ...]] = []
        member_rows: list[tuple[Any, ...]] = []
        for user_id, user in enumerate(users, start=1):
            user_rows.append(
                (
                    user_id,
                    user["login"],
                    user.get("name"),
                    user.get("email"),
                    user.get("type", "User"),
                    False,
                    created_at,
                )
            )
            member_rows.append((1, user_id, user.get("role", "member"), "active"))
        session.executemany(
            """
            INSERT INTO github_users(
                id, login, name, email, user_type, site_admin, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            user_rows,
        )
        session.executemany(
            """
            INSERT INTO github_organization_members(
                organization_id, user_id, role, state
            ) VALUES (?, ?, ?, ?)
            """,
            member_rows,
        )

        label_id = 0
        for repository_id, repository in enumerate(repositories, start=1):
            repository_name = repository["name"]
            session.execute(
                """
                INSERT INTO github_repositories(
                    id, owner_organization_id, name, full_name, description,
                    private, archived, default_branch, next_issue_number,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    repository_id,
                    1,
                    repository_name,
                    f"{organization_login}/{repository_name}",
                    repository.get("description"),
                    repository.get("private", False),
                    False,
                    repository.get("default_branch", "main"),
                    1,
                    created_at,
                    created_at,
                ),
            )
            session.executemany(
                """
                INSERT INTO github_repository_collaborators(
                    repository_id, user_id, permission
                ) VALUES (?, ?, ?)
                """,
                [
                    (
                        repository_id,
                        user_id,
                        "admin" if user.get("role", "member") == "admin" else "push",
                    )
                    for user_id, user in enumerate(users, start=1)
                ],
            )
            if repository.get("default_labels", True):
                label_rows: list[tuple[Any, ...]] = []
                for name, color, description in DEFAULT_LABELS:
                    label_id += 1
                    label_rows.append(
                        (label_id, repository_id, name, color, description, True)
                    )
                session.executemany(
                    """
                    INSERT INTO github_labels(
                        id, repository_id, name, color, description, is_default
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    label_rows,
                )

    def create_operations(
        self,
        session: RelationalSession,
        *,
        actor: str,
        now: Any | None = None,
        git_data_plane: Any | None = None,
    ) -> Any:
        from .pull_requests import GitHubPullRequestOperations

        return GitHubPullRequestOperations(
            session,
            actor_login=actor,
            now=now,
            git_data_plane=git_data_plane,
        )


def _mapping(value: Any, context: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise ConfigurationError(f"{context} must be a table/object")
    return value


def _list(value: Any, context: str) -> list[Any]:
    if not isinstance(value, list):
        raise ConfigurationError(f"{context} must be an array")
    return value


def _non_empty_string(value: Any, context: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigurationError(f"{context} must be a non-empty string")
    return value


def _optional_string(value: Any, context: str) -> None:
    if value is not None and not isinstance(value, str):
        raise ConfigurationError(f"{context} must be a string or null")


def _require_keys(
    mapping: Mapping[str, Any], allowed: set[str], context: str
) -> None:
    unknown = set(mapping) - allowed
    if unknown:
        raise ConfigurationError(
            f"unknown keys in {context}: {', '.join(sorted(unknown))}"
        )
