# honest failure taxonomy

> headspace tells an agent why a job really died: a command the image cannot execute is reported as the caller's error instead of a broken engine, and a job killed for exceeding its memory ceiling says so by name instead of hiding behind exit status 137
> instruction: Verify by re-running the two reproductions from the 2026-07-28 live test against --provider docker: (1) 'headspace run <ws> definitely-not-a-binary' must exit 6 with a full result package naming the profile and the missing executable, with no container id or http+docker:// URL anywhere in stdout or stderr; (2) a 512 MiB allocation under --memory-bytes 134217728 must exit 8 with status `resource_exhausted`, naming the ceiling in a key finding and the remedy in an attention entry. Both must also pass through the fake provider in the conformance suite.

## Audience

- agent runtimes that drive headspace as a subprocess and branch on its exit code without a human in the loop — the embodiment consumer this hand-over is for, specifically — and secondarily the humans reading the same result markdown
  - instruction: Verify through subprocess invocation of the CLI, asserting on the real process exit code and the --json payload.

## Before → After

- Before: a command the image cannot execute and a genuinely broken Docker daemon are the same exit 7 carrying the same 'check the engine is running and reachable, then retry' hint, and a job the kernel killed at its memory ceiling is indistinguishable from a job whose own logic failed: both are exit 6, 'the command completed with exit status 137', with empty warnings and empty attention
  - instruction: Write each regression test first and confirm it fails against current HEAD before implementing the fix.
- After: a caller can tell four things apart from the exit code alone — a command that was never executable, a job killed for exceeding a declared budget, an ordinary failing computation, and an engine that actually broke — without parsing prose
  - instruction: One integration test, one workspace, four commands, four asserted exit codes.

## Why it matters

- an autonomous consumer acts on the exit code, so a wrong code is not cosmetic: today's classification tells an agent to retry a deterministic failure forever, and gives it no signal to raise --memory-bytes instead of re-running an identical job that will be killed identically
  - instruction: Assert the attention entry is non-empty and names a flag the caller can actually change.

## Requirements

- docker run() must stop reporting a command the image cannot execute as a broken engine: container.start() (providers/docker.py:674) sits inside `_engine`(), and `_ENGINE_FAILURES` (providers/docker.py:326) catches DockerException — the base class of APIError — so Docker's 400 'executable file not found in $PATH' becomes exit 7 with a 'check the engine is running, then retry' hint, a deterministic failure the caller is told to retry forever
  - instruction: In DockerProvider.run(), catch APIError around container.start() specifically and match Docker's not-found signature ('executable file not found', and the OCI runtime init wording) BEFORE it reaches `_engine`()'s DockerException handler. Convert it to a JobOutcome with status failure and `exit_status` 127. Build the key finding from the profile name and the caller's argv\[0\] — never from the engine's message text, so no container id or API URL can reach the result.
  - honesty: running a command absent from the image exits 6 with a full nine-section result package whose key findings name both the profile and the executable it lacks, and neither stdout nor stderr contains a container id, an http+docker:// URL, or any other raw engine string
- docker run() must detect an OOM kill: `_await_exit` already calls container.reload() (providers/docker.py:1109), so container.attrs\['State'\] is fresh where `exit_status` is read at :678 and carries OOMKilled alongside ExitCode — the signal is available with zero extra engine calls
  - instruction: Read container.attrs\['State'\].get('OOMKilled') at docker.py:678 alongside the existing ExitCode read — `_await_exit`'s reload() already made it fresh — and thread it into `_status`() as a third input.
  - honesty: an OOM-killed job is detected from container.attrs\['State'\]\['OOMKilled'\] using only the reload `_await_exit` already performs — a test pins that run() issues no additional engine call for the detection
- a job killed for exceeding its memory ceiling must name that ceiling in the result package's warnings and attention sections, the way a timeout already names its budget — live testing produced only 'the command completed with exit status 137' with warnings and attention both empty, leaving an agent no signal to raise --memory-bytes rather than retry unchanged
  - instruction: In the result builder, key off the `resource_exhausted` status to emit a key finding naming `requested_limit`(policy,'memory') in bytes and an attention entry naming the remedy (raise --memory-bytes or reduce the working set).
  - honesty: an OOM-killed job's result names the enforced ceiling in bytes in a key finding AND carries an attention entry naming the remedy (raise --memory-bytes or reduce the working set), in both the markdown and --json renderings
- `max_memory_bytes` must not under-report the peak that caused a kill: the same live run reported 5320704 bytes for a process killed at the 134217728-byte ceiling, because `_Usage`.sample() polls on the `_await_exit` loop and misses the final spike — the number most relevant to the failure is the one the report gets most wrong
  - instruction: Apply the floor label in the `resource_usage` renderer when the status is `resource_exhausted`; the measured value itself stays untouched, so the honesty lives in the rendering rather than in the number.
  - honesty: whenever a job was OOM-killed, the `resource_usage` rendering labels `max_memory_bytes` as a sampled floor rather than a maximum, and no code path ever substitutes the ceiling for the measured value
- both new behaviours must land in the fake provider as well as docker: providers/fake.py:302 run() builds its JobOutcome from a scripted JobPlan, and tests/conformance.py is 'one behavioural contract, any backend' — a behaviour only docker implements is one the conformance suite structurally cannot cover, which is this repo's analogue of culture's all-backends rule
  - instruction: Extend JobPlan in providers/fake.py with fields that script a non-executable command and a memory kill, mirroring how it already scripts `infrastructure_failure` and timeout via the plan object.
  - honesty: the fake provider can script both a non-executable command and a memory kill, and every new conformance case passes against the docker AND fake bindings — with the fake case running on a host with no engine installed
- tests/conformance.py must grow cases for both failures, which means new ProviderCase fields (it already carries `failing_command`, `slow_command`, `flooding_command` and `break_engine` at :165-198) that every existing binding must supply — the docker and fake bindings both
  - instruction: Add ProviderCase fields (e.g. `missing_executable_command`, `memory_kill_command`, `memory_kill_bytes`) plus the two suite methods; both bindings — tests/`test_provider_docker.py` and tests/`test_provider_fake.py` — must supply every new field.
  - honesty: the conformance suite fails if a provider reports either failure as `infrastructure_failure`, and both existing bindings supply every new ProviderCase field
- the documented taxonomy must stay in sync wherever it is published: README.md:240-241 (the exit-code table), headspace/cli/`_commands`/learn.py:78-79 (the self-teaching prompt an agent actually reads), and docs/traceability.md:30 (requirement 10)
  - instruction: Update README.md:236-241, headspace/cli/`_commands`/learn.py:74-79, and docs/traceability.md requirement 10 (status and gap columns) in the same commit as the code change.
  - honesty: README's exit-code table, learn.py's self-teaching prompt, and traceability.md requirement 10 all name code 8 and the reclassified command-not-executable path, and requirement 10's gap column no longer lists these two defects
- the exit-8 decision requires four coordinated edits that must land together: STATUSES in core/result.py:97-106, `JOB_STATUSES` in providers/base.py:164, the `_STATUS_EXIT_CODES` map in core/workspace.py:273-281, and `EXIT_RESOURCE_EXHAUSTED` plus `EXIT_CATEGORIES` and `TAXONOMY_CODES` in cli/`_errors.py` — result.py's own comment warns the tuple is 'shared verbatim with the exit-code mapping', so a partial edit breaks the mapping silently
  - instruction: Land all four vocabulary edits in one commit, and add a test that iterates STATUSES asserting every member has a row in both `_STATUS_EXIT_CODES` and `EXIT_CATEGORIES` — so a partial edit fails the suite instead of breaking the mapping silently.
  - honesty: a test asserts that STATUSES, `JOB_STATUSES`, `_STATUS_EXIT_CODES` and `EXIT_CATEGORIES` all agree about `resource_exhausted`, so landing any one of the four edits without the others fails the suite rather than breaking the mapping silently
- four test modules pin the status/exit-code vocabulary and must be extended with the new row: tests/`test_exit_codes.py`, tests/`test_workspace.py`, tests/`test_cli_verbs.py` and tests/conformance.py — docs/traceability.md already records that `test_exit_code_for_status_uses_the_documented_taxonomy` parametrizes only success, failure and timeout, leaving the other rows unpinned, so the new code should arrive pinned rather than inherit that gap
  - instruction: Add `resource_exhausted` to the parametrize list in `test_exit_code_for_status_uses_the_documented_taxonomy`, and pin the cancelled, `policy_denied` and infrastructure rows while there, closing the gap traceability.md:30 records.
  - honesty: `test_exit_code_for_status_uses_the_documented_taxonomy` parametrizes `resource_exhausted` alongside its existing rows, closing rather than widening the incompleteness traceability.md:30 already records
- the change carries a version bump and a CHANGELOG entry per CLAUDE.md's version-bump-every-PR rule, which CI enforces; the current version is 0.8.1 and a new exit code in a published contract is a minor bump, not a patch
  - instruction: Use the version-bump skill for a minor bump to 0.9.0, with a CHANGELOG entry naming both defects and the new exit code.
  - honesty: pyproject.toml declares 0.9.0, CHANGELOG.md carries the matching entry, and the version-check CI job passes
- OOM detection must key on State.OOMKilled and never on exit status 137: verified live on this host that a genuine memory kill reports OOMKilled=true ExitCode=137, while an ordinary process exiting deliberately with SystemExit(137) reports OOMKilled=false ExitCode=137 — the exit status alone cannot tell a killed job from one that chose that code, so inferring `resource_exhausted` from 137 would misclassify an honest computational failure as a budget breach
  - instruction: Gate `resource_exhausted` on OOMKilled being true; never branch on `exit_status` == 137. Add the deliberate SystemExit(137) control case as a test so the ambiguity stays pinned.
  - honesty: a job that deliberately exits 137 without being killed is still reported as an ordinary failure (exit 6), and only a job carrying OOMKilled=true is reported as `resource_exhausted` (exit 8) — both cases are pinned by tests
- timeout keeps precedence over `resource_exhausted`: headspace's own wall-clock enforcer stops a job with container.kill() (docker.py:1120), which also yields exit 137, so a job that both outran its clock and pressed against its memory ceiling must still report timeout — the status must name what actually stopped the job, and headspace stopping it deliberately outranks the kernel's budget
  - instruction: In `_status`(), evaluate `timed_out` before the OOM branch so timeout wins, and add a test that sets both conditions at once rather than relying on branch order.
  - honesty: a job that is killed by the wall-clock enforcer reports timeout (exit 4) even when OOMKilled is true, and a test covers that ordering explicitly rather than leaving it to whichever branch runs first
- the not-executable matcher must cover all five observed engine wordings, not just the one the live test happened to hit: probing the docker SDK directly produced 'executable file not found in $PATH' (binary absent from PATH), 'stat <path>: no such file or directory' (absolute path absent), 'is a directory: permission denied' (a directory), and bare 'permission denied' (an existing non-executable file) — matching only the first wording would leave three of the four still misreported as `infrastructure_failure` with a retry hint
  - instruction: Match on the shared substring 'error during container init: exec:' and then branch on the specific wording to choose 126 vs 127; add all four probed variants as test cases.
  - honesty: all four probed variants are covered by tests and each is reported as a caller error rather than exit 7
- the synthesized job exit status must follow the shell convention properly: 127 when the command was not found (absent from PATH, or an absolute path that does not stat) and 126 when it was found but could not be executed (a directory, or a file without the execute bit) — the exported spec says 127 uniformly, which would report a permission problem as a lookup failure and mislead an agent into re-spelling a command that was actually there
  - instruction: Map 'not found in $PATH' and 'stat ...: no such file or directory' to 127; map 'permission denied' and 'is a directory' to 126.
  - honesty: a directory and a non-executable file each yield `exit_status` 126 while an absent binary yields 127, pinned separately by tests
- the matcher must fail safe toward `infrastructure_failure`: reclassify only on a confident match of the container-init exec step ('error during container init: exec:', the substring common to all four probed variants), and leave every other 400 on container.start() as exit 7 — because the opposite misclassification is the more dangerous one, telling an agent a genuinely broken engine is its own bad command and to stop retrying, and defaulting to 7 is exactly today's behaviour, so an unmatched case is a no-op rather than a regression
  - instruction: Structure the handler as: if not an exec-step 400, re-raise unchanged so it flows to the existing `_ENGINE_FAILURES` path. Add a test feeding a non-exec 400 through run().
  - honesty: a 400 that is not an exec-step failure still reports `infrastructure_failure`, pinned by a test that feeds a non-exec 400 through the same path
- a misclassification must stay diagnosable without putting the engine string back into the caller's context: the full engine message should remain retrievable through the existing bounded-evidence escape hatch (the same 'headspace inspect <handle> --logs' path a truncated result already names) rather than being discarded entirely — otherwise claim c12 buys context cleanliness at the cost of leaving a matcher misfire with no evidence trail at all
  - instruction: Keep the engine message on the job's captured-output path so 'headspace inspect <job> --logs' still returns it; assert it is absent from the rendered result sections.
  - honesty: the raw engine message is absent from the result package's rendered sections yet recoverable for the job handle, and a test asserts both halves
- the fix must not regress the security improvement it incidentally delivers: today's failure discloses a container id and the engine's internal http+docker://localhost/v1.52/... URL to whatever consumes the CLI's stderr, and suppressing that is an information-disclosure fix as well as a context-hygiene one, so it belongs in the CHANGELOG as such
  - instruction: Assert absence explicitly: the test should search the combined stdout+stderr for 'http+docker', for the container id, and for '400 Client Error' and require all three to be missing.
  - honesty: the CHANGELOG names the removal of the container id and engine URL from error output as an information-disclosure fix, and a test asserts neither appears in stdout or stderr for the not-executable path

## Honesty conditions

- both defects are demonstrated fixed by re-running the exact live-test reproductions that found them, against the real docker provider, not only against the fake
- a test asserts the exit-code mapping for 0 through 7 is unchanged — same codes, same category names — so the change can only add 8 and can never silently re-point an existing code
- no provider can return a status absent from `JOB_STATUSES` (JobOutcome.`__post_init__` already refuses it), and the new member is added in the same commit as its exit-code row so the two never disagree
- both reproductions are verified through the CLI's real process exit code and --json payload — the surface a subprocess consumer actually reads — not through in-process Python assertions alone
- each defect gets a regression test that demonstrably fails against today's code and passes after the fix, so the before-state is pinned rather than described
- one test drives all four outcomes — a non-executable command, a budget kill, an ordinary failing computation, and a broken engine — and asserts four distinct exit codes from one workspace
- a `resource_exhausted` result's attention entry names an actionable remedy, so an agent reading it has a next move other than re-running an identical job
- both reproductions run against --provider docker in the integration suite and skip cleanly on a host with no engine, so the success signal is checked against the real backend rather than only the fake
- the CHANGELOG entry names both movements (OOM 6->8, not-executable 7->6) explicitly, not only the addition of code 8
- a test asserts the pairing explicitly (process exit 6 alongside `exit_status` 127/126) so a future change cannot quietly collapse the two

## Success signals

- the two live-test reproductions inverted: 'headspace run <ws> definitely-not-a-binary' exits 6 with a full result package naming the profile and the missing executable and no Docker API string anywhere, and a 512 MiB allocation under --memory-bytes 134217728 exits 8 with status `resource_exhausted` naming the ceiling and the remedy
  - instruction: Put both reproductions in tests/`test_integration_docker.py` behind the existing engine-present skip guard.

## Scope / boundaries

- the exit-code band is a stable additive contract: headspace/cli/`_errors.py` states 'extend downward-compatibly, never renumber an existing code' — codes 0-7 keep their present meanings, and any new category takes a new number rather than repurposing one
  - instruction: Assert the 0-7 mapping is unchanged rather than describing it: a test comparing `EXIT_CATEGORIES` for 0-7 against a literal expected dict.
- `JOB_STATUSES` is a closed vocabulary (providers/base.py:165-169: success, `partial_success`, failure, cancelled, timeout) and base.py:668 states that `infrastructure_failure` and `policy_denied` are raised as ProviderError/PolicyError, never returned as an outcome — so a provider cannot simply invent a new job status without changing that contract deliberately
  - instruction: Add the new member to `JOB_STATUSES` in the same commit as its exit-code row; JobOutcome.`__post_init__` already refuses anything outside the tuple.
- the additive-band framing must not be read as 'no consumer behaviour changes': exit code 8 is a new number, but the OOM case MOVES from 6 to 8 and the not-executable case MOVES from 7 to 6, so a consumer with an explicit branch table sees cases leave the arms they used to land in — the CHANGELOG and README must say this plainly rather than describing the change as purely additive
  - instruction: Write the CHANGELOG entry under a 'Changed' heading naming both movements, not only 'Added: exit code 8'.
- a caller legitimately sees two different numbers for one event — headspace's process exit code (6, the taxonomy category) and the job's own `exit_status` inside the result package (127 or 126, the command's status) — and this is correct, not an inconsistency to be harmonised later: they answer different questions, and JobOutcome's invariants already require a non-zero `exit_status` for a failure status
  - instruction: One test asserting the pair together: process exit code 6 and result\['provenance'\] / `exit_status` 127 in the same assertion.

## Non-goals

- the raw engine string must not reach the caller's context: today the failure surfaces a full Docker API error carrying a container id and the internal http+docker://localhost/v1.52/... URL, which is precisely the execution-transcript pollution headspace exists to prevent — the fix reports the caller's mistake, it does not forward the engine's sentence
- two further defects found in the same live test stay out of this frame: destroy leaking the Docker volume of a workspace whose container vanished (core/workspace.py:817 skips provider.remove() entirely when descriptor is None, then `delete_workspace`() drops the only record), and inspect reporting status success / 'lifecycle state: ready' for a workspace its own warning says the engine no longer holds — both are real, both are separately framed
- the wall-clock timeout path is not touched: live testing confirmed it already does exactly what this frame asks of the memory path — exit 4 at 5.6s against a 5s ceiling, with the honest finding 'no exit status means the command was stopped, not that it succeeded' — so it is the model to copy, not a surface to change

## Scope exploration

- `s1` — `headspace/providers/docker.py:326 (_ENGINE_FAILURES) + :674 (container.start)`: the set's own docstring states its intent as 'a broken engine (exit 7), never a caller that asked for the wrong thing', but DockerException is APIError's base class, so every engine 400 — including a caller's bad command — is classified as `infrastructure_failure`
  - seeds: `c2`
- `s2` — `headspace/providers/docker.py:678 + :1097 (_await_exit reload loop)`: OOMKilled sits in the same freshly-reloaded State dict that ExitCode is already read from, so distinguishing a memory kill from an ordinary non-zero exit costs no additional engine round-trip
  - seeds: `c3`
- `s3` — `live run: 512 MiB allocation under --memory-bytes 134217728`: the kill is real (SIGKILL, exit 137) but wholly unexplained in the result: status failure, empty warnings, empty attention, and no mention of memory outside the `policy_summary` line
  - seeds: `c4`
- `s4` — `headspace/providers/docker.py _Usage.sample() polled from _await_exit`: sampled peak memory is a floor, not a maximum: on an OOM kill it understated the true peak by ~25x (5320704 bytes reported against the 134217728-byte ceiling the kernel enforced)
  - seeds: `c5`
- `s5` — `headspace/providers/fake.py:302 (run) + tests/conformance.py`: the fake is a genuine second implementation held to the same suite, so a docker-only fix would leave the new taxonomy untested at the seam; JobPlan needs to be able to script both a non-executable command and a memory kill
  - seeds: `c7`
- `s6` — `tests/conformance.py:165-198 (ProviderCase) + :594/:612 (existing intent tests)`: `test_run_reports_a_failing_job_without_calling_it_infrastructure` and `test_run_status_never_claims_infrastructure_or_policy` already codify exactly the intent the missing-executable path violates — the gap is a hole in an asserted contract, not an unasserted one
  - seeds: `c8`
- `s7` — `docs/traceability.md:30 (requirement 10)`: requirement 10 — 'a failed or timed-out job clearly separates computational failure, policy denial, cancellation, and infrastructure failure' — is ALREADY recorded as Partial, with cancellation named as a declared category with no producer; these two defects are two further gaps in that same tracked row, not a new requirement
  - seeds: `c9`
- `s8` — `headspace/cli/_errors.py (exit-code policy)`: the taxonomy is explicitly versioned as an additive band, so widening it is permitted but renumbering or re-pointing 6/7 is not
  - seeds: `c10`
- `s9` — `headspace/providers/base.py:161-173 + :625-696 (JobOutcome)`: JobOutcome.`__post_init__` enforces status/`exit_status` coherence (success requires exit 0, failure requires non-zero, and cancelled/timeout are exactly the statuses carrying no exit status), so any synthesized outcome for a non-executable command must satisfy those invariants
  - seeds: `c11`
- `s10` — `live run: headspace run livetest definitely-not-a-binary`: the emitted error is a 400 Client Error string containing container id 7f2362c4… and the engine's internal API URL, so this defect is a context-pollution defect as much as a taxonomy one
  - seeds: `c12`
- `s11` — `headspace/core/workspace.py:805-845 (destroy) + inspect result fields`: explored and deliberately excluded: the vanished-workspace volume leak and the inspect status/warning contradiction are distinct defects with distinct root causes, and folding them in would make one change answer to four unrelated acceptance criteria
  - seeds: `c13`
- `s12` — `live run: sleep 60 under --wall-clock-seconds 5`: timeout is already a first-class status with its own exit code and a finding that refuses to read silence as success; the memory path should reach the same standard, and the timeout path itself needs no change
  - seeds: `c14`
- `s13` — `README.md:236-241 (exit-code table) + cli/_commands/learn.py:74-79`: the published contract defines 6 as 'the job ran correctly and produced a failing result' and 7 as 'the engine or environment broke' — a command that was never executable fits NEITHER definition cleanly, which is why the right code is a decision (pending q1) rather than a lookup
- `s14` — `exit-code observability for a script consumer (live-verified)`: an exit-1 `user_error` path emits empty stdout with prose on stderr, while an exit-6 path emits a full nine-section result package — so q1 decides whether an embodiment consumer gets structured evidence or a bare line for a failure it will hit often
- `s15` — `headspace/cli/_errors.py TAXONOMY_CODES + core/workspace.py exit_code_for_status`: adding code 8 (pending q2) means widening the status->code mapping and every consumer's branch table; keeping exit 6 with additive warnings costs nothing at the contract boundary but leaves memory kills undetectable without parsing text
- `s16` — `core/result.py:87-106 (status vocabulary) + core/workspace.py:273-281 (_STATUS_EXIT_CODES) + cli/_errors.py`: the vocabulary comment says 'treat this tuple as frozen', but read precisely it forbids RENAMING a member ('renaming a member here silently breaks that mapping') — appending a new one is exactly the additive extension cli/`_errors.py`'s band permits, so the decision is compatible with both notes rather than in tension with them
  - seeds: `c18`
- `s17` — `tests/test_exit_codes.py, test_workspace.py, test_cli_verbs.py, conformance.py`: the vocabulary is asserted in four places and the existing parametrization is known-incomplete per traceability.md:30, so this change should close that recorded hole for `resource_exhausted` rather than widen it
  - seeds: `c19`
- `s18` — `CLAUDE.md version policy + pyproject.toml:3 (0.8.1)`: version-check CI blocks merge without a bump, and adding a taxonomy code is an additive contract change — minor, not patch
  - seeds: `c20`
- `s19` — `live probe: docker create --memory 64m + a control container exiting SystemExit(137)`: OOMKilled=true/ExitCode=137 for a real kill versus OOMKilled=false/ExitCode=137 for a deliberate exit — proves exit status 137 is ambiguous and OOMKilled is the only sound discriminator; verified on cgroup v2, systemd driver, rootful Docker 29.1.3
  - seeds: `c26`
- `s20` — `challenge pass / unstated-assumptions lens: docker SDK APIError wordings on container.start()`: the exported spec's c2 instruction was written from a single observed sample ('executable file not found in $PATH'); a direct SDK probe of four not-runnable command shapes produced four different wordings, so the single-wording match in the spec would have shipped a fix that missed three of them
  - seeds: `c28`
- `s21` — `challenge pass / failure-mode lens: POSIX 126 vs 127 against the four probed variants`: 126 (found but not executable) and 127 (not found) are distinct shell conventions and the probe shows headspace can tell the two cases apart from the engine wording, so collapsing both to 127 discards information the caller can act on
  - seeds: `c29`
- `s22` — `challenge pass / containment lens: which direction the matcher should err`: both misclassifications are harmful but asymmetrically — a user error read as engine failure retry-loops (today's bug), while an engine failure read as user error makes an agent abandon work it should retry; defaulting unmatched 400s to today's exit 7 makes the change strictly additive in behaviour
  - seeds: `c30`
- `s23` — `challenge pass / adjacent-systems lens: existing exit-code consumers vs claim c10`: c10 correctly states the code NUMBERS are additive, but two failure CASES change which code they emit; nothing in the frame said so before this pass, and a reader could take 'additive band' as a compatibility guarantee it does not make
  - seeds: `c31`
- `s24` — `challenge pass / overlooked-actors lens: process exit code vs in-package exit_status`: the two numbers coexist by design; recording it as a boundary so a later reader does not 'fix' the apparent disagreement
  - seeds: `c32`
- `s25` — `challenge pass / observability lens: c12 (no raw engine string) vs diagnosing a matcher misfire`: c12 and diagnosability pull against each other; the existing truncation escape hatch already resolves exactly this shape of tension elsewhere in the CLI, so reusing it costs no new mechanism
  - seeds: `c33`
- `s26` — `challenge pass / security lens: information disclosure in today's error path`: the live-test output leaked a container id and the engine's internal API URL into stderr; the reclassification removes both, which is worth naming explicitly rather than landing silently as a side effect
  - seeds: `c34`
- `s27` — `challenge pass / concurrency lens: docker.py run() + _await_exit + core/store.py locking`: clean pass — the change adds no shared mutable state and no new engine calls; OOMKilled is read from a reload the wall-clock loop already performs, and the workspace lock verified live as serialising jobs across processes is untouched
- `s28` — `challenge pass / lifecycle lens: declared artifacts on a job that was killed`: examined, no change proposed — a `resource_exhausted` job's declared-but-unproduced artifacts still trigger the existing destroy refusal, which is the correct existing behaviour and needs no special case for the new status
- `s29` — `challenge pass / reversibility lens: publishing exit code 8`: examined — no rollback mechanism is warranted for a CLI exit code, but the change is one-way in practice once a consumer branches on 8; recorded as residual risk rather than mitigated

## Decisions

- a command the image cannot execute is `computation_failed` (exit 6) carrying a full result package, with a synthesized `exit_status` of 127 — the shell convention for command-not-found — and a key finding naming the profile and the executable it lacks; README's wording for code 6 is reworded to cover a command that was never executable, which is a clarification within the additive band, not a renumbering
- an OOM kill widens the taxonomy: a new exit code 8 = `resource_exhausted` with a matching new member in the `JOB_STATUSES` vocabulary, so a caller can branch on a memory kill from the exit code alone exactly as it already can for timeout — accepted as the larger change because the alternative leaves the agent parsing prose, which the taxonomy exists to avoid
- `max_memory_bytes` keeps reporting the sampled value and is labelled a floor rather than a maximum; the enforced ceiling is named separately by the `resource_exhausted` finding and warning, so headspace never states a number it did not observe — the same enforced-vs-measured honesty it already applies to storage

## Open parks

- [unknown_nonblocking] whether State.OOMKilled is set as reliably outside the verified host matrix — this evidence is from cgroup v2 with the systemd driver on rootful Linux Docker 29.1.3; cgroup v1, rootless engines, and the Docker Desktop VM are unverified. Non-blocking because the fallback is safe: a kill the flag does not report degrades to an ordinary failure (exit 6), which is today's behaviour, never a false `resource_exhausted`
- [unknown_nonblocking] once the embodiment consumer branches on exit 8, withdrawing or renumbering it becomes a breaking change for that consumer — the window in which this contract is cheap to revise closes at that hand-over, not at merge
- [unknown_nonblocking] whether runc's 'error during container init: exec:' wording is stable across engine and runtime versions — it held across all four probed variants on docker 29.1.3 / runc, but it is still a string match against another project's internal message, and no structured API field distinguishes an exec-step 400 from any other
- [follow_up] whether the same treatment should later extend to the other budgets a job can breach — pids and storage — which today have no kill-detection story at all; storage is measured-not-enforced on the default volume driver, so there may be nothing to detect yet
