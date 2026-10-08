from enum import IntEnum


class ExitCode(IntEnum):
    OK = 0
    PROBLEM = 1
    USAGE = 2
    INCOMPLETE = 3


class SupportOpsError(Exception):
    exit_code: ExitCode = ExitCode.INCOMPLETE

    def __init__(self, message: str, *, hint: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint


class ConfigError(SupportOpsError):
    exit_code = ExitCode.USAGE
