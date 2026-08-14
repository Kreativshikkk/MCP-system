"""Domain errors raised by MCPSystem Core."""


class MCPSystemError(Exception):
    """Base class for expected MCPSystem failures."""


class ServiceOperationError(MCPSystemError):
    """Provider-domain failure safe to expose as a correctable tool error."""

    status_code = 500
    error = "internal_error"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class ConfigurationError(MCPSystemError):
    """The requested environment or plugin configuration is invalid."""


class EnvironmentNotFoundError(MCPSystemError):
    """The requested environment does not exist."""


class EnvironmentNotReadyError(MCPSystemError):
    """The environment exists but cannot serve requests."""


class EnvironmentFrozenError(MCPSystemError):
    """A write was attempted while the environment was frozen for snapshot."""

    status_code = 409
    error = "environment_frozen"


class PluginNotFoundError(MCPSystemError):
    """No plugin with the requested id and version is registered."""


class ServiceNotFoundError(MCPSystemError):
    """The requested service instance is not part of the environment."""


class TemplateNotFoundError(MCPSystemError):
    """The requested immutable template does not exist."""


class TemplateNotReadyError(MCPSystemError):
    """The template exists but cannot be cloned."""


class SnapshotNotFoundError(MCPSystemError):
    """The requested immutable environment snapshot does not exist."""
