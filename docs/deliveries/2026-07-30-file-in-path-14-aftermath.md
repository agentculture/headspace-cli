# Delivery Summary — file-in-path-14 aftermath

plan: `file-in-path-14-aftermath` · run: `complete` · date: `2026-07-30`
baseline: `devague summary skeleton`

## Intent

> The aftermath of the file-in-path-14 workforce run is settled: the five
> recorded deviations are adjudicated by the operator, a Docker job ended by
> stop --apply is recorded cancelled end-to-end so c43 is fully met, and
> headspace's supported import surface is declared where an external consumer
> can read it.

Three issues, three closures. **#15** closed by adjudication before any code
was written (the previous run's `d1`–`d5` approved). **#16** — a job an
operator deliberately ended being recorded `failure`/exit 6 — closed by a
positive cross-process signal, because the engine's own numbers provably
cannot separate a stopped job from `python -c "raise SystemExit(137)"`.
**#18** — an external agent unable to tell whether the Python package was a
supported import surface — closed by `headspace.api` plus the README section
that declares it.

## Planned Work

Quoted verbatim from the `devague summary` skeleton:

- `t1` — Docker sentinel discriminator: stop writes a namespaced sentinel bearing the signalled `job_id` into the workspace volume before SIGTERM; run classifies the ended job cancelled (single file: headspace/providers/docker.py)
- `t2` — Live Docker cancelled test + retire the test-side #16 caveat (files: tests/`test_integration_docker.py`, tests/`test_stop_verb.py` docstring)
- `t3` — headspace.api facade: the declared import surface (new files: headspace/api.py, tests/`test_api.py`)
- `t4` — README closure: Python API section + retire the exit-5 caveat (single file: README.md)
- `t5` — Delivery verification + release: success-signal checklist, version bump, changelog, PR closure of #16/#18 (files: pyproject.toml, CHANGELOG.md)
- `t6` — Two-phase evidence upgrade (dep t1, single file: headspace/providers/docker.py + its unit tests): intent marker exec'd pre-signal, signalled marker via the anchor post-signal, run honors only a signalled marker naming its job; full marker lifecycle
- `t7` — Adversarial and edge live-test wave (dep t2, t6; files: tests/ live suite): tamper, SIGKILL, intent-only, coincident stop+timeout, `job_id` leak probe, symlink probe

## Actual Delivery

| Plan task | Status | What actually landed |
|-----------|--------|----------------------|
| `t1` | delivered | `8cd315e`, merged `adf4cfc`. Sentinel written inside `stop`'s existing engine boundary; `_status` gains a `cancelled` branch at the documented precedence; `run` drops the 137 because `JobOutcome.__post_init__` refuses a cancelled outcome carrying one. `test_stop_verb`'s three-surface inertness proofs passed unedited. Superseded five commits later by `t6`, by design. |
| `t2` | delivered | `2e6fe33`, merged `a7a53db`. The live test renamed to `test_stop_apply_ends_a_live_job_and_the_blocked_run_records_it_cancelled`, four assertions flipped, module-docstring bullet corrected, and `test_stop_verb.py`'s "One gap these tests deliberately do not paper over" section retired — with that file's code byte-identical, verified by diff. |
| `t3` | delivered | `a6b46e6`, merged `6806b73`. `headspace/api.py` (305 lines) + `tests/test_api.py` (14 tests), including one that walks `inspect.signature` on both sides so the facade cannot drift from the `Orchestrator`, and subprocess proofs that neither an engine probe nor the Docker SDK is touched at import. |
| `t4` | delivered | `388bb6e`, merged `cd6b503`, plus `974940c`. README `## Python API` section; exit-code row 5's caveat removed with the other eight rows byte-identical; issue #18 answered ([comment](https://github.com/agentculture/headspace-cli/issues/18#issuecomment-5131990977)). |
| `t5` | delivered | `6cd2fee`. Four success signals run with output quoted below; 0.10.0 → **0.11.0** with a changelog entry that states the exit-code change is *not* purely additive for a consumer branching on exit code. |
| `t6` | delivered | `b081ded`, merged `46147e4`. Two markers replacing one; `run` classifies from the countersignal alone; both cleared on every path including exit 0; directory/FIFO/device refusal (exit 22) closing a defect the orchestrator found in merged `t1` code. Added a bounded settle-wait beyond the brief — see `d4`. |
| `t7` | delivered | `76e7449`, merged into `HEAD`. Seven live probes (+936 lines). Two of its findings became code changes outside every task's scope (`d8`, `d9`, commit `77c9e96`) and one became issue #20. |

## Mid-work Decisions

- `d1` — serialize `t6` before `t2` instead of running them as one wave; execution and merge order becomes `t1`+`t3` → `t6` → `t2` → `t4`+`t7` → `t5`. No task content, acceptance criterion, or coverage target changes. — *`t2` would have authored its live assertions against `t1`'s single-phase sentinel while `t6` replaced that mechanism underneath. The observable contract is preserved by c23, so the test should survive — but "should survive" is a bet on a security-sensitive seam, and the operator chose the order that removes the bet at the cost of one wave.* Approved at gate 2.
- `d2` — every task agent develops with `-m 'not integration'`; the main agent runs the live-engine gate serially at each merge point. — *This repo's live-engine tests use deterministic object names and are single-writer against one daemon, so two agents running live suites concurrently produce false reds. Wave-level file-disjointness says nothing about a shared external resource.* Approved at gate 2.
- `d3` *(proposed)* — the `not integration` lane is **not** daemon-free: `tests/test_provider_docker.py` applies pytest marks per class rather than as a module-level `pytestmark`, so ~108 of its tests reach the live daemon even under `-m 'not integration'`. `d2`'s isolation premise is therefore weaker than stated. Found by the `t1` agent contradicting the orchestrator's own brief, and verified.
- `d4` *(proposed)* — `run` holds the door open for the countersignal for up to `CANCELLATION_SETTLE_SECONDS = 2.0`, entered only when an intent marker names the job that just settled. — *`container.stop()` blocks until the container is dead, so the countersignal necessarily lands after `run` wakes; the brief as written would have recorded almost every completed cancellation as a failure.*
- `d5` *(proposed)* — `t6`'s acceptance criterion 2 named the wrong mechanism: "a crashed run's leftovers are cleaned by reconciliation" does not hold. `Orchestrator.reconcile` is journal-intent driven and its only cleanup reach is `_reap_staging`, scoped to the copy-in prefix. The property holds via **the next `run` in that workspace**, which clears both names unconditionally.
- `d6` *(proposed)* — the plan's coverage of the #16 caveat stopped at README and `tests/`, but `headspace/explain/catalog.py` and `docs/traceability.md` still told a reader the gap was open. Both fixed in `9ef868b`.
- `d7` *(proposed)* — `t7`'s acceptance criterion 1 is false, and the code is stronger than it assumed: a job whose SIGTERM handler deletes the markers was recorded `cancelled` 14 of 14 rounds, because there is no instant at which a job and its own countersignal both exist.
- `d8` *(proposed)* — closed a fabrication route no task covered: `headspace put <ws> <src> .headspace-cancelled` landed a regular file at the countersignal's path and reported success. `RESERVED_ROOT_NAMES` now refuses it pre-socket.
- `d9` *(proposed)* — the module docstring's **"Unforgeable"** was an overclaim; the property is conditional on the job id's entropy. Restated at the point of use and filed as #20.
- Not covered by any deviation record: the orchestrator found, by its own audit of merged `t1` code, that `mv -f file dir` **relocates** rather than failing, so a job planting a *directory* at the marker path defeated the write while `stop` still exited 5. Verified empirically, routed to the in-flight `t6` agent rather than filed, and closed there with a recoverability test.
- Not covered by any deviation record: `/ask-colleague` was attempted twice for the diverse-mind review and failed both times (`drive ended without calling finish`, a malformed `<tool_call>run_command` in place of a tool call). The gateway is reachable and the model streams text, so this is a tool-protocol failure in the local backend, not a prompt problem. **The diverse-mind pass did not happen as designed**; it was replaced by the orchestrator's own audit (which found the directory defect) and `t7`'s adversarial wave (which found two more). Recorded rather than quietly dropped.

## Drift From Plan

| Plan item | Reason for divergence | Classification |
|-----------|-----------------------|----------------|
| `t6` (`d1`) | `t2` would have authored live assertions against a mechanism `t6` was replacing underneath; the operator chose the order that removes the bet at the cost of one wave | `acceptable` |
| `t2` (`d2`) | live-engine tests are single-writer against one daemon; agents develop deselected, the main agent runs the live gate serially | `acceptable` |
| `t1` (`d3`) | the deselection lane does not actually avoid the daemon — per-class marks, ~108 tests still reach it | `needs-follow-up` |
| `t6` (`d4`) | the brief's two-writes-and-a-read shape is live-broken without a bounded settle-wait | `acceptable` |
| `t6` (`d5`) | acceptance criterion 2 named reconciliation; the mechanism that actually holds is the next `run` | `acceptable` |
| `t4` (`d6`) | two further surfaces still stated the gap was open; no confirmed task covered either | `needs-follow-up` |
| `t7` (`d7`) | acceptance criterion 1's first clause is false — the tamper cannot reach the countersignal at all | `acceptable` |
| `t7` (`d8`) | a copy-in could pre-plant a forged countersignal; closed in `77c9e96`, outside every task's stated scope | `acceptable` |
| `t7` (`d9`) | "Unforgeable" was stronger than what holds; restated and filed as #20 | `needs-follow-up` |

## Evidence

- tests: `tests/test_integration_docker.py::test_stop_apply_ends_a_live_job_and_the_blocked_run_records_it_cancelled` — **pass** (11.37s, live Docker 29.1.3)
- tests: `pytest tests/test_integration_docker.py` — **30 passed** (132.29s), live
- tests: `pytest -m 'not integration' -n 8` — **998 passed** (baseline before this run: 976)
- tests: `DOCKER_HOST=unix:///nonexistent/docker.sock pytest tests/test_integration_docker.py` — **30 skipped**, no failure, no hang
- tests: `pytest tests/test_stop_verb.py` — **10 passed**, file's code byte-identical to pre-run
- lint: `black --check` / `isort --check-only` / `flake8` / `bandit -r headspace` — all clean
- docs: `markdownlint-cli2 README.md CHANGELOG.md docs/traceability.md` — 0 errors
- the red bar (h4): the repo's own tripwire fired unedited against the fixed engine — `AssertionError: the interrupted run exited 5; issue #16 says 6 today, and 5 once cancellation is distinguishable. assert 5 == 6` — `1 failed, 22 passed`
- commits: `4736c92..6cd2fee` (16 commits)
- PRs / issues: closes #16, #18; #15 closed by adjudication 2026-07-30; opened #19 (gate-2 deviations), #20 (the residual)

### Success signals (c16 / h13), run with output

```text
1. live cancelled node   -> 1 passed in 11.37s
2. import probe          -> __all__ = ['create','run','put','export','destroy']
                            docker SDK imported at import time: False
3. previous run's ledger -> d1..d5 all APPROVED (plan file-in-path-14)
4. no #16 caveat left    -> README.md: clean; headspace/ + docs/: clean;
                            tests/: one hit, past tense ("used to be a known gap")
```

## Delivery Claims

| Claim | Confidence | Evidence |
|-------|------------|----------|
| a job ended by `stop --apply` on Docker is recorded `cancelled` with no exit status, and its `run` exits 5 | high | test `tests/test_integration_docker.py::test_stop_apply_ends_a_live_job_and_the_blocked_run_records_it_cancelled` · live end-to-end re-run against Docker 29.1.3 |
| that classification cannot come from the engine's numbers | high | `test_an_honest_exit_137_without_oomkilled_is_an_ordinary_failure` · live: `python -c "raise SystemExit(137)"` still records `failure`/6 with 137 in key findings |
| partial or tampered evidence never fabricates a cancellation | high | `test_an_intent_marker_alone_never_fabricates_a_cancellation`, `test_the_wait_for_a_countersignal_ends`, `test_an_intent_marker_a_job_planted_for_itself_never_becomes_a_cancellation` |
| a job cannot learn its own `job_id` from any surface it can read | high | `test_a_job_never_learns_its_own_job_id_from_any_surface_it_can_read` (7 surfaces) · independent hand-verification by the orchestrator on job `job-ba54a53bb657` |
| a job that *is told* its `job_id` **can** forge a cancellation | high | `test_a_countersignal_a_job_planted_for_itself_does_forge_a_cancellation` — recorded as a limitation, filed as #20, not claimed fixed |
| a copy-in can no longer land at a reserved marker name | high | commit `77c9e96` · 7 tests incl. a control that ordinary lookalike names still work · live: refused with exit 1, `notes.txt` still succeeds |
| `headspace.api` is importable with no engine and imports no Docker SDK | high | `tests/test_api.py` subprocess probes · `DOCKER_HOST=unix:///nonexistent/... python -c "import headspace.api"` |
| the facade's signatures match the `Orchestrator`'s | high | `test_signatures_are_copied_from_the_real_orchestrator_methods_not_invented` |
| README's example runs verbatim with no engine | high | orchestrator extracted the block from the committed README and ran it: exit 0, three asserts passed, `report.json` written |
| an external consumer can settle #18 from README + `__all__` alone | medium | file `README.md` `## Python API` · issue comment — the bar is a human/agent judgement, so not `high` |
| the CLI contract (verbs, result package, exit taxonomy) is unchanged apart from the documented exit-5 case | high | 998-test lane green with pre-existing contract tests unedited · README's other eight exit rows byte-identical |
| the tamper probe's outcome is *guaranteed* `cancelled` | unverified | it depends on a ~180 ms `/system/df` race on this host; the test asserts a disjunction over the two honest outcomes and says so — see `d7` |
| a diverse-mind review was obtained | unverified | `/ask-colleague` failed twice; **not claimed done** |

## Remaining Work / Follow-up

- **#20 — a job told its own `job_id` can forge a cancellation.** Filed with three candidate shapes; salting the marker payload with a per-run nonce held only in `run`'s memory looks right and small. Not improvised here because the fix belongs with its own tests. Owner: next delivery.
- **`d3` — the deselection lane is not daemon-free.** `tests/test_provider_docker.py` should carry a module-level `pytestmark` (or its marks should be honest about reaching the engine) so `-m 'not integration'` means what every brief in this run assumed it meant. Owner: follow-up PR.
- **The tamper probe's timing residual (`d7`).** The *designed* protection — intent marker → settle wait — is exactly what the tamper disarms; what saves the classification on this host is an unrelated slow engine call. Direction is safe either way, but the protection is incidental. Worth either making it deliberate or documenting that the outcome is host-dependent.
- **Fake-provider parity** (carried from the plan's risk register): the fake pins the cancelled contract only via an authored `JobPlan`. Making `fake.stop` structurally flip an in-flight job's outcome would prove the semantics on both backends. Deliberate follow-up, not in this delivery.
- **Two unlabelled volumes** predating this session (`headspace-hs-integ-solo-ce58eac0c1`, `headspace-hs-posture-solo-749471d11c3d`, created 2026-07-29, `Labels: null`) are invisible to both the label-scoped fixture teardown and the module reaper. Not this run's, left in place — but an unlabelled volume wearing a headspace name suggests something creates one outside the provider's labelled path. Worth a look.
- **`d3`–`d9` await operator adjudication.** They are `proposed`; confirming or rejecting them is the user's decision alone, and the ledger stays honest until they do.
