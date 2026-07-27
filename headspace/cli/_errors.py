"""CliError and exit-code policy (stable-contract).

Every failure inside headspace-cli raises :class:`CliError`. The
top-level ``main()`` catches it, formats via :mod:`headspace.cli._output`,
and exits with :attr:`CliError.code`. This guarantees:

* no Python traceback leaks to stderr (the agent-first error contract);
* every error has a structured shape ``{code, message, remediation}``;
* the exit-code policy is centralised in one place.
"""

from __future__ import annotations

from dataclasses import dataclass

# Exit-code policy. Documented in ``headspace-cli learn`` output.
# 0      = success
# 1      = user-input error (bad flag, missing required arg, unknown path)
# 2      = environment / setup error (tool not installed, file unreadable)
# 3+     = failure taxonomy — WHY a job failed, not just THAT it failed.
#          Mirrors the normalized status vocabulary a job report uses
#          one-to-one (that vocabulary's ``success``/``partial_success`` are
#          result states, not error exits, so they have no code here):
#            3 = policy_denied          refused by policy before running
#            4 = timeout                wall-clock or budget limit hit
#            5 = cancelled              caller asked for it to stop
#            6 = computation_failed     ran correctly, produced a failing result
#            7 = infrastructure_failure engine/environment broke — NOT a
#                                       computational failure
# Additive band: extend downward-compatibly, never renumber an existing code.
EXIT_SUCCESS = 0
EXIT_USER_ERROR = 1
EXIT_ENV_ERROR = 2
EXIT_POLICY_DENIED = 3
EXIT_TIMEOUT = 4
EXIT_CANCELLED = 5
EXIT_COMPUTATION_FAILED = 6
EXIT_INFRASTRUCTURE_FAILURE = 7

# Exit code -> category name, so structured error output can NAME the
# category instead of making a caller memorize integers.
EXIT_CATEGORIES: dict[int, str] = {
    EXIT_SUCCESS: "success",
    EXIT_USER_ERROR: "user_error",
    EXIT_ENV_ERROR: "environment_error",
    EXIT_POLICY_DENIED: "policy_denied",
    EXIT_TIMEOUT: "timeout",
    EXIT_CANCELLED: "cancelled",
    EXIT_COMPUTATION_FAILED: "computation_failed",
    EXIT_INFRASTRUCTURE_FAILURE: "infrastructure_failure",
}

# Codes in the reserved 3+ band get their category tagged onto `message`
# automatically (see CliError.__post_init__) so the failure taxonomy is
# legible in plain-text error output, not only --json. 0/1/2 are excluded
# deliberately — their message shape must not change under existing callers.
_TAGGED_CODES = frozenset(
    {
        EXIT_POLICY_DENIED,
        EXIT_TIMEOUT,
        EXIT_CANCELLED,
        EXIT_COMPUTATION_FAILED,
        EXIT_INFRASTRUCTURE_FAILURE,
    }
)


def category_for_code(code: int) -> str:
    """Name the failure category for an exit code (``"unknown"`` if unmapped)."""
    return EXIT_CATEGORIES.get(code, "unknown")


@dataclass
class CliError(Exception):
    """Structured error raised within the CLI; carries a remediation hint for agents."""

    code: int
    message: str
    remediation: str = ""

    def __post_init__(self) -> None:
        if self.code in _TAGGED_CODES:
            tag = f"[{self.category}] "
            if not self.message.startswith(tag):
                self.message = tag + self.message
        super().__init__(self.message)

    @property
    def category(self) -> str:
        """The named failure category for :attr:`code` (see ``EXIT_CATEGORIES``)."""
        return category_for_code(self.code)

    def to_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "message": self.message,
            "remediation": self.remediation,
            "category": self.category,
        }
