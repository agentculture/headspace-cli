"""stdout / stderr helpers with a strict split (stable-contract).

Rule: **results go to stdout, diagnostics and errors go to stderr.** Agents
parsing output can rely on this invariant. JSON mode routes structured
payloads to the same streams — never mixes them.
"""

from __future__ import annotations

import json
import sys
from typing import Any, TextIO

from headspace.cli._errors import TAXONOMY_CODES, CliError


def emit_result(data: Any, *, json_mode: bool, stream: TextIO | None = None) -> None:
    """Write a command result to stdout (or ``stream``)."""
    s = stream if stream is not None else sys.stdout
    if json_mode:
        json.dump(data, s, ensure_ascii=False)
        s.write("\n")
        return
    text = data if isinstance(data, str) else str(data)
    s.write(text)
    if not text.endswith("\n"):
        s.write("\n")


def emit_error(err: CliError, *, json_mode: bool, stream: TextIO | None = None) -> None:
    """Write a :class:`CliError` to stderr.

    Text mode renders as two lines when a remediation is present::

        error: <message>
        hint: <remediation>

    The ``hint:`` prefix is required by the agent-first error rubric.

    Errors in the failure-taxonomy band (:data:`TAXONOMY_CODES`) name their
    category inline — ``error: [policy_denied] <message>`` — so a caller
    reading plain stderr can tell *why* a job failed, not merely that it did,
    without switching to ``--json``. Codes 0/1/2 render unchanged. The tag
    belongs here rather than baked into ``CliError.message``: rendering is
    this module's job, and a caller reading ``err.message`` should get the
    message it was constructed with.
    """
    s = stream if stream is not None else sys.stderr
    if json_mode:
        json.dump(err.to_dict(), s, ensure_ascii=False)
        s.write("\n")
        return
    tag = f"[{err.category}] " if err.code in TAXONOMY_CODES else ""
    s.write(f"error: {tag}{err.message}\n")
    if err.remediation:
        s.write(f"hint: {err.remediation}\n")


def emit_diagnostic(message: str, *, stream: TextIO | None = None) -> None:
    """Write a human diagnostic (progress, summary) to stderr."""
    s = stream if stream is not None else sys.stderr
    s.write(message if message.endswith("\n") else message + "\n")
