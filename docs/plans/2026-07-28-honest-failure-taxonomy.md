# Build Plan — honest failure taxonomy

slug: `honest-failure-taxonomy` · status: `exported` · from frame: `honest-failure-taxonomy`

> headspace tells an agent why a job really died: a command the image cannot execute is reported as the caller's error instead of a broken engine, and a job killed for exceeding its memory ceiling says so by name instead of hiding behind exit status 137

## Tasks

### t1 — Widen the failure taxonomy: add `resource_exhausted` and exit code 8 across all four vocabulary surfaces in one commit

- instruction: FILES YOU OWN: headspace/core/result.py, headspace/providers/base.py, headspace/core/workspace.py, headspace/cli/`_errors.py`, tests/`test_exit_codes.py`, tests/`test_workspace.py`. Append `STATUS_RESOURCE_EXHAUSTED` to STATUSES (result.py:97-106) and `JOB_STATUSES` (base.py:164), add the row to `_STATUS_EXIT_CODES` (workspace.py:273-281), and add `EXIT_RESOURCE_EXHAUSTED`=8 to `_errors.py` with its `EXIT_CATEGORIES` and `TAXONOMY_CODES` entries. NOTE result.py's comment says 'treat this tuple as frozen' — read it precisely: it forbids RENAMING a member, and appending is exactly what cli/`_errors.py`'s additive band permits. Do not renumber or re-point any code 0-7.
- covers: c18, h8, c10, h14, c11, h15, c19, h9
- acceptance:
  - STATUSES (core/result.py), `JOB_STATUSES` (providers/base.py), `_STATUS_EXIT_CODES` (core/workspace.py) and `EXIT_RESOURCE_EXHAUSTED`/`EXIT_CATEGORIES`/`TAXONOMY_CODES` (cli/`_errors.py`) all carry `resource_exhausted` -> 8, landed together
  - a test iterates STATUSES and asserts every member has a row in both `_STATUS_EXIT_CODES` and `EXIT_CATEGORIES`, so removing any one of the four edits fails the suite
  - a test compares the exit-code mapping for 0 through 7 against a literal expected dict and passes unchanged, proving no existing code was renumbered or re-pointed
  - `test_exit_code_for_status_uses_the_documented_taxonomy` parametrizes `resource_exhausted` alongside cancelled, `policy_denied` and `infrastructure_failure`, closing the gap traceability.md:30 records

### t2 — Classify a command the image cannot execute as the caller's error, not a broken engine

- instruction: FILES YOU OWN: headspace/providers/docker.py, tests/`test_docker_classification.py` (new file). In DockerProvider.run(), catch APIError around container.start() specifically, BEFORE it reaches `_engine`()'s DockerException handler. Match the substring 'error during container init: exec:' — verified common to all four wordings — then branch on the specific wording for 126 vs 127. Anything else re-raises UNCHANGED so it keeps today's exit-7 behaviour; that direction is deliberate, because reading a broken engine as a caller error would make an agent abandon work it should retry. Build the key finding from the profile name and argv\[0\]; never interpolate the engine's message into it. You do NOT depend on t1 — use the existing `STATUS_FAILURE` vocabulary.
- covers: c2, h1, c28, h21, c29, h22, c30, h23, c33, h26, c34, h27
- acceptance:
  - an APIError from container.start() whose message contains 'error during container init: exec:' yields a JobOutcome with status failure instead of propagating to `_ENGINE_FAILURES`
  - all four probed wordings are covered: 'executable file not found in $PATH', 'stat <path>: no such file or directory', 'is a directory: permission denied', and bare 'permission denied'
  - `exit_status` is 127 for the two not-found wordings and 126 for the two found-but-not-executable wordings
  - a 400 that is NOT an exec-step failure re-raises unchanged and still reports `infrastructure_failure` (exit 7), pinned by a test
  - the key finding names the profile and the caller's argv\[0\]; stdout and stderr contain no container id, no 'http+docker' substring and no '400 Client Error'
  - the full engine message remains retrievable for the job handle via the captured-output path, while absent from the rendered result sections

### t3 — Detect an OOM kill from State.OOMKilled and report it as `resource_exhausted`

- instruction: FILES YOU OWN: headspace/providers/docker.py, tests/`test_docker_classification.py`. YOU MUST BUILD ON t2's merged state — same file, sequenced deliberately. Read State\['OOMKilled'\] at docker.py:678 where ExitCode is already read; `_await_exit`'s reload() at :1109 already made it fresh, so add no engine call. Gate the new status on OOMKilled being true and NEVER on `exit_status` == 137: verified live that a deliberate SystemExit(137) gives OOMKilled=false with ExitCode=137, so 137 alone cannot distinguish the cases. In `_status`(), evaluate `timed_out` BEFORE the OOM branch so a wall-clock kill still reports timeout.
- depends on: t2, t1
- covers: c3, h2, c26, h11, c27, h12
- acceptance:
  - OOMKilled is read from container.attrs\['State'\] at the same point ExitCode is read, using only the reload `_await_exit` already performs — no additional engine call
  - status is gated on OOMKilled being true and never on `exit_status` == 137: a job deliberately running SystemExit(137) is still reported as an ordinary failure (exit 6)
  - a job killed by the wall-clock enforcer reports timeout (exit 4) even when OOMKilled is true, pinned by a test that sets both conditions rather than relying on branch order

### t4 — Make the result package name the memory ceiling and label the sampled peak a floor

- instruction: FILES YOU OWN: headspace/core/workspace.py, headspace/core/result.py, tests/`test_result.py`. Key the new finding and attention entry off the `resource_exhausted` status; get the ceiling from `requested_limit`(policy,'memory'). The floor label belongs in the RENDERER — do not change the measured value, and never substitute the ceiling for it. This mirrors the enforced-vs-measured honesty the storage limit already uses.
- depends on: t1
- covers: c4, h3, c5, h4
- acceptance:
  - a `resource_exhausted` result carries a key finding naming the enforced memory ceiling in bytes and an attention entry naming an actionable remedy, in both markdown and --json
  - `max_memory_bytes` is labelled a sampled floor in the rendering whenever the job was OOM-killed, and no code path substitutes the ceiling for the measured value

### t5 — Teach the fake provider to script both new failures

- instruction: FILES YOU OWN: headspace/providers/fake.py. Extend JobPlan with fields scripting a non-executable command and a memory kill, mirroring how it already scripts `infrastructure_failure` (fake.py:314) and timeout. The fake is a genuine second implementation, not a stub — it is what makes the suite runnable with no engine installed, so its behaviour must match docker's at the seam.
- depends on: t1
- covers: c7, h5
- acceptance:
  - JobPlan can script a non-executable command and a memory kill, mirroring how it already scripts `infrastructure_failure` and timeout
  - the fake's new behaviour is exercised on a host with no Docker engine installed

### t6 — Extend the conformance suite so both backends are held to the new taxonomy

- instruction: FILES YOU OWN: tests/conformance.py, tests/`test_provider_docker.py`, tests/`test_provider_fake.py`. Add ProviderCase fields (conformance.py:165-198) and the suite methods; BOTH bindings must supply every new field or they fail to construct. The module header says binders must not edit conformance.py — that rule is for binders; you are the suite author. Existing tests `test_run_reports_a_failing_job_without_calling_it_infrastructure` (:594) and `test_run_status_never_claims_infrastructure_or_policy` (:612) already assert this intent — extend that lineage rather than duplicating it.
- depends on: t3, t5
- covers: c8, h6
- acceptance:
  - ProviderCase gains fields for a non-executable command and a memory kill, and both bindings (`test_provider_docker.py`, `test_provider_fake.py`) supply every new field
  - the suite fails if a provider reports either failure as `infrastructure_failure`
  - the new cases pass against the docker binding and the fake binding, with the docker binding skipping cleanly when no engine is present

### t7 — Pin both defects with the live-test reproductions that found them

- instruction: FILES YOU OWN: tests/`test_integration_docker.py`. Write each regression test FIRST and confirm it fails against pre-fix HEAD — a test that passes before the fix proves nothing. Drive the CLI by subprocess and assert on the real process exit code and --json payload, since that is the surface a subprocess consumer reads. Keep both reproductions behind the existing engine-present skip guard.
- depends on: t3, t4, t1
- covers: c1, h13, c21, h16, c22, h17, c23, h18, c24, h19, c25, h20, c32, h25
- acceptance:
  - each regression test is written first and demonstrably fails against pre-fix HEAD before the fix lands
  - 'headspace run <ws> definitely-not-a-binary' exits 6 with a full result package naming the profile and the missing executable, asserted via subprocess on the real process exit code and the --json payload
  - a 512 MiB allocation under --memory-bytes 134217728 exits 8 with status `resource_exhausted`, naming the ceiling in a key finding and a remedy in a non-empty attention entry
  - one test drives four outcomes from one workspace — not executable, budget kill, ordinary failing computation, broken engine — and asserts four distinct exit codes
  - a test asserts the pairing of process exit 6 with in-package `exit_status` 127/126 together, so the two numbers cannot later be quietly collapsed
  - both reproductions live behind the existing engine-present skip guard in tests/`test_integration_docker.py`

### t8 — Sync every surface that publishes the taxonomy

- instruction: FILES YOU OWN: README.md, headspace/cli/`_commands`/learn.py, docs/traceability.md. README's exit table is at :236-241, learn.py's prompt at :74-79. traceability.md requirement 10 is at :30 and is ALREADY marked Partial — update its status and gap columns; these two defects were gaps in that row, so the row improves rather than gaining a new entry. Run markdownlint-cli2 on every markdown file you touch.
- depends on: t1
- covers: c9, h7
- acceptance:
  - README.md's exit-code table, learn.py's self-teaching prompt and traceability.md requirement 10 all name code 8 and the reclassified not-executable path
  - traceability.md requirement 10's gap column no longer lists these two defects
  - markdownlint-cli2 passes on every edited markdown file

### t9 — Release the contract change honestly: version bump and a CHANGELOG that names what moved

- instruction: FILES YOU OWN: pyproject.toml, CHANGELOG.md. Use the version-bump skill for a MINOR bump to 0.9.0 — a new exit code in a published contract is not a patch. The CHANGELOG must name both case MOVEMENTS under Changed (OOM kills 6->8, not-executable 7->6), not only 'Added: exit code 8': the code numbers are additive but two failure cases change which code they emit, and a reader could take 'additive' as a compatibility guarantee it does not make. Also name the removal of the container id and engine API URL as an information-disclosure fix.
- depends on: t2, t3, t4, t5, t6, t7, t8
- covers: c20, h10, c31, h24
- acceptance:
  - pyproject.toml declares 0.9.0 and the version-check CI job passes
  - the CHANGELOG entry names both case movements explicitly — OOM kills 6 -> 8 and not-executable 7 -> 6 — under a Changed heading, not only 'Added: exit code 8'
  - the CHANGELOG names the removal of the container id and engine API URL from error output as an information-disclosure fix

## Risks

- [unknown_nonblocking] t2 and t3 both edit DockerProvider.run(); they are sequenced by an explicit dependency for file-disjointness at merge, not because t3 needs t2's logic — a scheduler that ignores the dep and runs them in one wave will collide (task t3)
- [unknown_nonblocking] widening ProviderCase is a breaking change for any out-of-tree conformance binding, which would fail to construct until it supplies the new fields; in-tree there are only two bindings and both are updated by t6 (task t6)
