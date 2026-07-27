"""Lifecycle state enum and the explicit transition table (stable-contract).

Every headspace moves through a small, closed set of lifecycle states
(docs/headspace_cli_issue_requirements.docx section 6: requested,
provisioning, ready, running, completed, failed, cancelled, expired,
destroyed). This module is the single source of truth for those states and
for which moves between them are legal — the fake provider, the Docker
provider, and every CLI verb call :func:`validate_transition` before
touching stored state or an engine. Centralising the table here, instead of
scattering ad hoc ``if`` checks across providers, is what keeps "state
transitions observable, invalid transitions fail clearly" (section 6) true
regardless of backend.

Kept deliberately pure: no I/O, no provider imports, no filesystem access.
:mod:`headspace.core` may never import a provider or the docker SDK (see
``headspace/core/__init__.py``); this module holds up its end by importing
only the standard library plus :mod:`headspace.cli._errors`, which itself
imports nothing from the rest of the package — sanctioned for this task
specifically to reuse the one error contract, not a general core -> cli
allowance.

The state graph is a DAG by design: no state ever transitions back to an
earlier stage, and ``requested`` is the sole entry point (nothing legally
targets it). ``destroyed`` is the only state with no outgoing edges.
Crucially, ``running`` has no direct edge to ``destroyed`` — a running
workspace must be cancelled (or reach ``completed``/``failed``) before it
can be destroyed. That single omission encodes the product rule "destroy
while a job runs either cancels it first or refuses": callers get the
refusal for free, as a :class:`~headspace.cli._errors.CliError`, purely by
construction of the table below — no orchestration-layer special case
needed. Any later task adding a destroy path MUST NOT add a
``running -> destroyed`` edge to work around this; it should drive the
existing ``running -> cancelled -> destroyed`` path instead.
"""

from __future__ import annotations

from enum import Enum

from headspace.cli._errors import EXIT_USER_ERROR, CliError


class State(str, Enum):
    """A headspace's normalized lifecycle state.

    Subclasses ``str`` so a ``State`` serializes as its plain value under
    ``json.dump`` (used by :mod:`headspace.cli._output` for ``--json`` mode)
    without a custom encoder, and compares equal to its string value.
    """

    REQUESTED = "requested"
    PROVISIONING = "provisioning"
    READY = "ready"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    DESTROYED = "destroyed"


# The explicit transition table: current state -> set of legal next states.
# Every State key is present, even destroyed (mapped to an empty set) so
# lookups never need a .get() fallback. See the module docstring for the
# rationale behind each edge and each deliberate non-edge.
TRANSITIONS: dict[State, frozenset[State]] = {
    State.REQUESTED: frozenset({State.PROVISIONING, State.CANCELLED, State.FAILED, State.EXPIRED}),
    State.PROVISIONING: frozenset({State.READY, State.FAILED, State.CANCELLED}),
    State.READY: frozenset({State.RUNNING, State.CANCELLED, State.EXPIRED}),
    State.RUNNING: frozenset({State.COMPLETED, State.FAILED, State.CANCELLED}),
    State.COMPLETED: frozenset({State.DESTROYED, State.EXPIRED}),
    State.FAILED: frozenset({State.DESTROYED, State.EXPIRED}),
    State.CANCELLED: frozenset({State.DESTROYED, State.EXPIRED}),
    State.EXPIRED: frozenset({State.DESTROYED}),
    State.DESTROYED: frozenset(),
}


def legal_transitions(state: State) -> frozenset[State]:
    """Return the set of states `state` may legally move to next."""
    return TRANSITIONS[state]


def validate_transition(current: State, target: State) -> None:
    """Raise :class:`CliError` if ``current -> target`` is not a legal move.

    Pure: takes and inspects two :class:`State` values and either returns
    ``None`` or raises — it never accepts or mutates a stored workspace
    record. Callers own the actual state write and are expected to call
    this *before* performing it, so a rejected transition never partially
    lands (see ``tests/test_states.py::
    test_illegal_transition_leaves_stored_state_unchanged``).
    """
    if target in TRANSITIONS[current]:
        return

    legal = sorted(s.value for s in TRANSITIONS[current])
    if legal:
        remediation = f"from '{current.value}', the legal next states are: " + ", ".join(legal)
    else:
        remediation = f"'{current.value}' is a terminal state; no transition is legal"

    raise CliError(
        code=EXIT_USER_ERROR,
        message=f"illegal lifecycle transition: '{current.value}' -> '{target.value}'",
        remediation=remediation,
    )
