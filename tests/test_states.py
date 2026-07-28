"""Tests for the lifecycle state machine (headspace.core.states).

Written first, TDD-style, against the transition table this task designs:
docs/headspace_cli_issue_requirements.docx section 6 names the nine states
and a seven-stage narrative (Request, Create, Prepare, Execute, Inspect,
Export, Retain-or-destroy) but does not enumerate legal edges — so this file
*is* the transition table's specification. ``EXPECTED_TRANSITIONS`` below is
the ground truth; headspace/core/states.py is implemented to match it, not
the other way around, so the matrix test below is not tautological.

Design notes a reviewer should check against the spec narrative:

* The graph flows forward with exactly one cycle: ``ready <-> running``.
  Added by deviation d4 — without it, ``ready -> running`` is a one-way door
  and a workspace can serve only one job, which contradicts "run multiple
  jobs sharing state" (doc section 11) and left ``running`` unreachable in
  practice. See :mod:`headspace.core.states` for the full rationale.
* ``destroyed`` is the only terminal state (no outgoing edges).
* ``running`` has no direct edge to ``destroyed``: a running workspace must
  be cancelled (or reach completed/failed) first. This encodes the spec's
  "destroy while a job runs either cancels it first or refuses" rule as a
  structural property of the table, not an ad hoc check in a provider. The
  d4 cycle deliberately does *not* weaken this: it makes the refusal apply
  to a state workspaces really occupy.
* ``requested`` is the sole entry point: nothing transitions back to it.
"""

from __future__ import annotations

import copy
import itertools

import pytest

from headspace.cli._errors import CliError
from headspace.core.states import TRANSITIONS, State, validate_transition

# Ground truth for the transition matrix, independent of the implementation
# under test. See the module docstring above for the rationale behind each
# edge (and each deliberate non-edge, e.g. running -> destroyed).
EXPECTED_TRANSITIONS: dict[State, frozenset[State]] = {
    State.REQUESTED: frozenset({State.PROVISIONING, State.CANCELLED, State.FAILED, State.EXPIRED}),
    State.PROVISIONING: frozenset({State.READY, State.FAILED, State.CANCELLED}),
    State.READY: frozenset({State.RUNNING, State.CANCELLED, State.EXPIRED}),
    State.RUNNING: frozenset({State.READY, State.COMPLETED, State.FAILED, State.CANCELLED}),
    State.COMPLETED: frozenset({State.DESTROYED, State.EXPIRED}),
    State.FAILED: frozenset({State.DESTROYED, State.EXPIRED}),
    State.CANCELLED: frozenset({State.DESTROYED, State.EXPIRED}),
    State.EXPIRED: frozenset({State.DESTROYED}),
    State.DESTROYED: frozenset(),
}


def test_nine_states_exist() -> None:
    """The spec names exactly nine normalized lifecycle states."""
    names = {s.value for s in State}
    assert names == {
        "requested",
        "provisioning",
        "ready",
        "running",
        "completed",
        "failed",
        "cancelled",
        "expired",
        "destroyed",
    }
    assert len(State) == 9


def test_implementation_transition_table_matches_expected() -> None:
    """headspace.core.states.TRANSITIONS is exactly the designed table."""
    assert TRANSITIONS == EXPECTED_TRANSITIONS


def test_every_state_covered_by_expected_table() -> None:
    """Every state has an (possibly empty) entry in the ground truth table."""
    assert set(EXPECTED_TRANSITIONS) == set(State)


# --- criterion 1: full matrix, legal and illegal, every state reachable ---


@pytest.mark.parametrize(
    "current,target",
    list(itertools.product(State, State)),
    ids=lambda s: s.value if isinstance(s, State) else str(s),
)
def test_full_transition_matrix(current: State, target: State) -> None:
    """Every one of the 81 (state, state) pairs behaves per the ground truth."""
    is_legal = target in EXPECTED_TRANSITIONS[current]
    if is_legal:
        assert validate_transition(current, target) is None
    else:
        with pytest.raises(CliError) as exc_info:
            validate_transition(current, target)
        err = exc_info.value
        assert err.code == 1
        assert err.remediation, "illegal transition must carry a remediation hint"
        assert current.value in err.message
        assert target.value in err.message


def test_every_state_reachable_from_requested() -> None:
    """Walking the table from `requested` reaches all nine states.

    `requested` is the entry point and is seeded directly rather than
    required to be reached via an edge — nothing legally transitions back
    to it (see test_nothing_transitions_back_to_requested below).
    """
    seen = {State.REQUESTED}
    frontier = [State.REQUESTED]
    while frontier:
        current = frontier.pop()
        for nxt in TRANSITIONS[current]:
            if nxt not in seen:
                seen.add(nxt)
                frontier.append(nxt)

    unreachable = set(State) - seen
    assert not unreachable, f"orphaned states, unreachable from requested: {unreachable}"
    assert seen == set(State)


def test_nothing_transitions_back_to_requested() -> None:
    """requested is a pure entry point: no state may legally move back to it."""
    for state, targets in TRANSITIONS.items():
        assert State.REQUESTED not in targets, f"{state.value} illegally targets requested"


def test_destroyed_is_terminal() -> None:
    """destroyed has no legal outgoing transitions at all."""
    assert TRANSITIONS[State.DESTROYED] == frozenset()


def test_running_returns_to_ready_so_one_workspace_can_host_many_jobs() -> None:
    """Deviation d4: the table's one deliberate cycle, and why it is there.

    A headspace is a *session* — the spec asks for multiple jobs sharing one
    workspace's state. Without this edge, ``ready -> running`` spends the
    workspace's remaining lifecycle on its first command, since nothing after
    ``running`` leads back; the orchestration layer's only escape was to never
    enter ``running``, which made a state the spec names (section 6)
    unreachable. With it, ``running`` is a state a workspace genuinely
    occupies for the duration of each job, and the destroy-during-run refusal
    below applies to a real state rather than a synthesized one.
    """
    assert State.READY in TRANSITIONS[State.RUNNING]
    assert State.RUNNING in TRANSITIONS[State.READY]
    assert validate_transition(State.RUNNING, State.READY) is None


def test_ready_and_running_form_the_tables_only_cycle() -> None:
    """Every other edge still moves strictly forward; the cycle is deliberate, not drift."""
    cycles = {
        (state, target)
        for state, targets in TRANSITIONS.items()
        for target in targets
        if state in TRANSITIONS[target]
    }
    assert cycles == {(State.READY, State.RUNNING), (State.RUNNING, State.READY)}


def test_running_has_no_direct_edge_to_destroyed() -> None:
    """A running workspace cannot be destroyed directly.

    Encodes the spec rule that destroying a running workspace must cancel
    it first (or let it complete/fail) rather than tearing it down mid-job.
    Later tasks (the orchestration layer) rely on this omission to reject a
    running -> destroyed request instead of having to special-case it.
    """
    assert State.DESTROYED not in TRANSITIONS[State.RUNNING]


# --- criterion 2: illegal transitions raise CliError, remediation hint,
# ---              and never mutate stored (or table) state -----------------


def test_illegal_transition_raises_cli_error_with_remediation_hint() -> None:
    with pytest.raises(CliError) as exc_info:
        validate_transition(State.DESTROYED, State.RUNNING)
    err = exc_info.value
    assert isinstance(err, CliError)
    assert err.code == 1
    assert err.remediation != ""
    assert "destroyed" in err.remediation or "terminal" in err.remediation


def test_illegal_transition_uses_documented_user_error_exit_code() -> None:
    """Exit code 1 = user error, per headspace/cli/_errors.py's policy.

    This task must not invent a new code; 3+ is reserved for a sibling task.
    """
    with pytest.raises(CliError) as exc_info:
        validate_transition(State.READY, State.DESTROYED)
    assert exc_info.value.code == 1


def test_legal_transition_returns_none_and_raises_nothing() -> None:
    assert validate_transition(State.REQUESTED, State.PROVISIONING) is None


def test_illegal_transition_leaves_stored_state_unchanged() -> None:
    """validate_transition is inspect-and-raise only; it never assigns.

    Simulates how a real caller (fake provider, Docker provider, CLI verb)
    is expected to use it: call validate_transition *before* writing the new
    state, so a rejected transition leaves the stored value exactly as it
    was — no partial writes, no rollback needed.
    """

    class FakeWorkspaceRecord:
        def __init__(self, state: State) -> None:
            self.state = state

        def attempt_transition(self, target: State) -> None:
            validate_transition(self.state, target)
            self.state = target  # only ever reached on a legal transition

    record = FakeWorkspaceRecord(State.READY)
    with pytest.raises(CliError):
        record.attempt_transition(State.DESTROYED)  # ready -> destroyed is illegal
    assert record.state is State.READY


def test_validate_transition_does_not_mutate_the_transition_table() -> None:
    """The module-level table itself must not be touched by validation calls."""
    before = copy.deepcopy(TRANSITIONS)

    # A mix of legal and illegal calls, deliberately exercising both branches.
    validate_transition(State.REQUESTED, State.PROVISIONING)
    with pytest.raises(CliError):
        validate_transition(State.REQUESTED, State.RUNNING)
    with pytest.raises(CliError):
        validate_transition(State.DESTROYED, State.REQUESTED)
    validate_transition(State.RUNNING, State.COMPLETED)

    assert TRANSITIONS == before
