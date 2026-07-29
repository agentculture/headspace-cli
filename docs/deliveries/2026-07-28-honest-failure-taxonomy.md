# Delivery Summary — honest failure taxonomy

plan: `honest-failure-taxonomy` · run: `complete` · date: `2026-07-28`
baseline: `devague summary skeleton`

## Intent

Live-testing headspace against a real Docker engine surfaced two defects in the
failure taxonomy. A command the image could not execute was reported as
`infrastructure_failure` (exit 7) with the hint *"the execution engine failed,
not the job — check the engine is running and reachable, then retry"* — telling
an autonomous consumer to retry a deterministic failure forever — while leaking
a container id and the engine's internal `http+docker://` URL into the caller's
context. Separately, a job killed for exceeding its memory ceiling was
indistinguishable from an ordinary failing computation: both exit 6, both
reporting only *"the command completed with exit status 137"*, with warnings and
attention empty.

Both were gaps in `docs/traceability.md` requirement 10, already recorded there
as **Partial**. This run executed the nine-task plan that closed them, fanned
out across four waves by `/assign-to-workforce`.

## Planned Work

Quoted verbatim from the `devague summary` skeleton:

- `t1` — Widen the failure taxonomy: add `resource_exhausted` and exit code 8 across all four vocabulary surfaces in one commit
- `t2` — Classify a command the image cannot execute as the caller's error, not a broken engine
- `t3` — Detect an OOM kill from State.OOMKilled and report it as `resource_exhausted`
- `t4` — Make the result package name the memory ceiling and label the sampled peak a floor
- `t5` — Teach the fake provider to script both new failures
- `t6` — Extend the conformance suite so both backends are held to the new taxonomy
- `t7` — Pin both defects with the live-test reproductions that found them
- `t8` — Sync every surface that publishes the taxonomy
- `t9` — Release the contract change honestly: version bump and a CHANGELOG that names what moved

## Actual Delivery

| Plan task | Status | What actually landed |
|-----------|--------|----------------------|
| `t1` | delivered | `STATUS_RESOURCE_EXHAUSTED` + `EXIT_RESOURCE_EXHAUSTED = 8` appended to all four surfaces; a test iterates `STATUSES` asserting each has a row in `_STATUS_EXIT_CODES` and `EXIT_CATEGORIES`, so a partial edit fails the suite. Merge `22b21e8` |
| `t2` | delivered | Exec-init 400s classified as the caller's failure with `exit_status` 126/127; every other 400 re-raised unchanged. Engine transport envelope, URL and container id redacted. Merge `4f2c921` |
| `t3` | delivered | `State.OOMKilled` read off the reload `_await_exit` already performs; `_status(timed_out, oom_killed, exit_status)` evaluates timeout first. Merge `9f5d75e` |
| `t4` | delivered | Ceiling named in a key finding, remedy in an attention entry, `max_memory_basis` derived at render time from package status. Also absorbed `d2` and fixed the vocabulary assertion. Merge `4a3a9fb` |
| `t5` | delivered | `JobPlan.not_executable()` / `.oom_killed()` plus POSIX constants, no docker import. Merge `1a5e50e` |
| `t6` | delivered | `ProviderCase` gained six fields; both bindings supply them; a meta-test proves the suite fails a provider that blames the engine. Merge `47bfda3` |
| `t7` | delivered | Four out-of-process regression tests behind the engine-present skip guard. Merge `84e9d95` |
| `t8` | delivered | README exit table, `learn` (text + JSON), traceability requirement 10. Merge `3ae7f97` |
| `t9` | delivered | 0.8.1 → 0.9.0, CHANGELOG naming both case movements and the information-disclosure fix. Merge `bb3f634` |

## Mid-work Decisions

Four deviations were recorded during the run. **All four are `--origin llm` and
remain `proposed` — none has been approved by the human owner of gate 2.** They
are reported here as pending, not as decisions, matching what
`devague summary` itself renders ("pending approval (not yet a decision)").
Every one is a correction to a *task brief the operator authored*, not to the
spec or the product.

- `d1` — proposed that `t1` also update the vocabulary assertion in
  `tests/test_result.py`. **Superseded, never needed.** On re-reading, the
  `/assign-to-workforce` TDD gate is specified as *"the task's tests"*, not the
  full suite, and the plan's existing `t4 → t1` dependency already assigned that
  fix to `t4`. The plan handled it; no departure was required. Recorded here
  because the operator initially halted the run on it.
- `d2` — `t2`'s acceptance criterion 5 (key finding names profile + `argv[0]`)
  moved into `t4`'s scope. `key_findings` is built in `core/workspace.py`, which
  `t2` did not own, and the provider deliberately never learns profile *names*.
  Implemented by `t4`.
- `d3` — `t2`'s criterion 6 narrowed: the *raw* engine message is absent from
  every rendered section, while the engine's redacted diagnosis appears bounded
  in the evidence excerpt and in full via `inspect --logs`. The criterion's two
  halves were in tension as written, because `core/workspace.py` renders
  `outcome.output` *as* the excerpt. The narrowed reading is what confirmed
  honesty condition `h26` describes.
- `d4` — `t7`'s criterion 4 ("four distinct exit codes") is unsatisfiable as
  written: an unrunnable command and an ordinary failing computation both exit
  6, which is precisely what the `t2` fix establishes rather than a gap in it.
  `t7` asserted the exact codes `[6, 8, 6, 7]` plus mutual distinguishability by
  `(process exit, command's own exit_status)` signature. The faulty criterion was
  the operator's.

Decisions not covered by any deviation record:

- Three agents independently mutation-tested their own suites without being
  asked (`t2`, `t4`, `t6`), confirming the new tests fail when the fix is
  inverted.
- `t2` closed a spoofing hole the brief did not anticipate: it parses
  `"<argv0>": <reason>` and matches only within `reason`, so a caller passing a
  path like `/tmp/no such file or directory` cannot force a 127 verdict.
- `t2` chose 126 as the default for an unrecognised exec-init wording — "found
  but not executable" never sends an agent to re-spell a name that was correct.
- `t4` derived `max_memory_basis` at render time rather than storing it, making
  it structurally impossible for a `resource_exhausted` package to present its
  sampled number as a maximum. It also worded the remedy around `--memory-bytes`
  being a `create` flag, since a workspace runs under its creation contract.
- `t4` hedged the 126/127 finding with "conventionally": a shell *inside* the
  workspace relaying its own "command not found" yields the same status, and
  `_job_findings` sees only a number.
- `t5`, told to write TDD tests but owning only `fake.py`, consulted the exported
  plan, found `tests/test_provider_fake.py` belongs to `t6`, and created an
  unclaimed new file rather than risk a collision.
- The operator corrected `t8`'s traceability note after `t3` merged (it honestly
  recorded `resource_exhausted` as having no producer, which `t3` then made
  false), and in doing so cited three test names from memory that were all
  wrong; caught by mechanical verification before commit.

## Drift From Plan

| Plan item | Reason for divergence | Classification |
|-----------|-----------------------|----------------|
| `t1` (`d1`) | proposed expansion of file ownership; superseded — the plan's `t4 → t1` dependency already covered it, so nothing actually diverged | acceptable |
| `t2` (`d2`) | key-finding placement moved to `t4`: `key_findings` lives in a file `t2` does not own, and the provider never learns profile names | acceptable |
| `t2` (`d3`) | criterion 6 narrowed to raw-message-absent + diagnosis-retrievable, because the captured-output path *is* the rendered excerpt | acceptable |
| `t7` (`d4`) | criterion 4 asserted as `[6, 8, 6, 7]` plus signature distinguishability, because two of the four outcomes legitimately share exit 6 | acceptable |

No task was dropped, blocked, or partially delivered. All nine merged with the
per-task TDD gate green before and after.

## Evidence

- tests: full suite `uv run pytest -n auto -q` — **681 passed**, 0 failed
- tests: `tests/test_docker_classification.py` + `tests/test_fake_failure_modes.py` — 47 passed
- tests: `tests/test_integration_docker.py -k regression` — 4 passed
- tests: `tests/test_provider_docker.py` + `tests/test_provider_fake.py` (conformance, both bindings) — 190 passed
- fail-first: the four `t7` regression tests run against pre-fix `35498b0` — **4 failed, 0 errored**, each on its predicted assertion, reproducing the original exit-7 payload verbatim
- lint: `flake8`, `black --check`, `isort --check-only` — clean; `bandit -r headspace/` — no issues identified
- lint: `markdownlint-cli2` on `README.md`, `CHANGELOG.md`, `docs/traceability.md` — 0 errors
- live: `live-verify.sh` against real Docker 29.1.3 — **0 failures** across 26 assertions
- commits: `300983b..bb3f634` (20 commits, 9 task merges)
- issues: `#7` (the two defects deliberately scoped out of this frame)

## Delivery Claims

| Claim | Confidence | Evidence |
|-------|------------|----------|
| A command the image cannot execute exits 6, not 7 | high | live-verify D1 (4 variants) · test `tests/test_integration_docker.py::test_regression_a_command_the_image_cannot_run_exits_six_with_a_full_result_package` · merge `4f2c921` |
| The job's own status is 127 (not found) vs 126 (not executable) | high | live-verify D1 · merge `4f2c921` · fail-first at `35498b0` |
| No container id, engine URL or transport envelope reaches the caller | high | live-verify D1 leak assertions · the `FORBIDDEN` triple at `tests/test_docker_classification.py:158` (container id, `http+docker`, `400 Client Error`) |
| An unmatched engine 400 still reports `infrastructure_failure` | high | `tests/test_docker_classification.py::test_run_still_calls_any_other_engine_400_an_infrastructure_failure` |
| An OOM kill exits 8 as `resource_exhausted` | high | live-verify D2 · test `tests/test_docker_classification.py::test_an_oom_kill_is_reported_as_resource_exhausted` · merge `9f5d75e` |
| A deliberate `SystemExit(137)` is **not** reported as a budget breach | high | live-verify D2b · test `tests/test_docker_classification.py::test_an_honest_exit_137_without_oomkilled_is_an_ordinary_failure` |
| Wall-clock timeout keeps precedence over an OOM verdict | high | live-verify D2c · test `tests/test_docker_classification.py::test_a_wall_clock_kill_reports_timeout_even_when_oomkilled_is_true` |
| The result names the ceiling and an actionable remedy | high | live-verify D2 · merge `4a3a9fb` |
| `max_memory_bytes` is labelled a sampled floor, never replaced by the ceiling | high | merge `4a3a9fb` — `max_memory_basis` derived at render time from package status |
| Both backends are held to the new taxonomy | high | 190 conformance tests across both bindings · merge `47bfda3` |
| Codes 0–7 keep their exact prior meanings | high | `tests/test_workspace.py::test_exit_code_mapping_for_codes_0_through_7_is_unchanged` |
| The regression tests genuinely catch the defects | high | fail-first at `35498b0`: 4 failed, 0 errored |
| `resource_exhausted` is reliably detected on hosts outside the verified matrix | unverified | evidence is cgroup v2 / systemd / rootful Linux Docker 29.1.3 only; cgroup v1, rootless engines and Docker Desktop untested (parked as `v3`) |
| The `error during container init: exec:` marker is stable across engine versions | unverified | held across four probed wordings on 29.1.3; still a string match against runc's internal message (parked as `v4`) |

## Remaining Work / Follow-up

- **`d1`–`d4` await human ruling.** All four are `proposed`; none is approved.
  `d1` is superseded and could reasonably be rejected. Approve or reject via
  `devague deviate --confirm <dN>` / `--reject <dN>`.
- **Issue `#7`** — the two defects deliberately excluded from this frame (the
  vanished-workspace volume leak, and `inspect` reporting `status: success` /
  "ready" for a workspace its own warning says the engine no longer holds). Filed
  with reproductions and root causes; not scheduled.
- **Parked `v3`** — `State.OOMKilled` reliability outside cgroup v2 / systemd /
  rootful Linux. Non-blocking: a kill the flag does not report degrades to exit
  6, which is the pre-change behaviour, never a false `resource_exhausted`.
- **Parked `v4`** — the exec-init marker is a string match against runc's
  internal wording. Fail-safe: an unmatched case keeps today's exit 7.
- **Follow-up `v2`** — extending the same treatment to the other budgets a job
  can breach (pids, storage). Storage is measured-not-enforced on the default
  volume driver, so there may be nothing to detect yet.
- **A stale root-owned directory** at `../worktrees/agent-t3` (dated 2026-07-05,
  unrelated to this run) blocked worktree creation and was sidestepped, not
  removed. It will block a future `agent-t3` worktree at that path.
