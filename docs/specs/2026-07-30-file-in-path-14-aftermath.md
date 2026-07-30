# file-in-path-14 aftermath

> The aftermath of the file-in-path-14 workforce run is settled: the five recorded deviations are adjudicated by the operator, a Docker job ended by stop --apply is recorded cancelled end-to-end so c43 is fully met, and headspace's supported import surface is declared where an external consumer can read it.
> instruction: three closures, one per issue: #15 closes by adjudication (d1-d5 approved in the ledger — done); #16 closes by the sentinel discriminator (c12) landing with a live Docker test; #18 closes by the headspace.api facade (c11) landing with its README statement — each closure retires its recorded caveat

## Audience

- audience: agents that offload execution into headspace and the humans operating them — the workforce-run operator adjudicating drift (#15), an operator stopping a runaway job (#16), and external integrators like embodiment's muse role choosing a surface to depend on (#18)

## Before → After

- After: after: a Docker job ended by stop --apply is recorded cancelled with no exit status and its run invocation exits 5; headspace.api is importable with a declared `__all__` and documented in README; d1-d5 are approved in the ledger; issues #15, #16 and #18 are closed and the two recorded #16 caveats (README exit-5 row, `test_stop_verb` docstring) are retired

## Why it matters

- why it matters: an operator who stops a job is currently told the job failed (exit 6) — the exact misclassification the honest-failure taxonomy (#9) exists to prevent; an external consumer cannot tell which Python surface is supported and may silently depend on private internals; and until the five recorded deviations were adjudicated the run's accountability loop stayed open

## Requirements

- the supported consumption surface is written down where an external consumer can find it: today headspace/`__init__.py` advertises only `__version__` in `__all__`, headspace/core/`__init__.py` defines no `__all__`, and README.md documents only the CLI contract — a would-be library consumer (embodiment, #18) cannot tell CLI-first from library-supported from the outside
  - honesty: an external consumer can determine the supported surface from README plus headspace.api.`__all__` alone, without reading source or asking — the exact test embodiment failed in #18
- a Docker job ended by stop --apply is recorded status=cancelled with `exit_status` None, and its concurrent run invocation exits 5 — closing c43's second half: today DockerProvider.`_status` (docker.py:2747) has no branch that can produce `STATUS_CANCELLED`, so the record reads failure/137 and run exits 6 (verified live per #16)
  - honesty: verified live against a real Docker engine, not inferred: stop --apply during a genuinely running job yields recorded status cancelled with `exit_status` null and the concurrent run invocation exits 5
- the fix lands with a live Docker test asserting the stopped job's recorded outcome, and retires the two recorded-gap notices that currently point at #16: the gap paragraph in tests/`test_stop_verb.py`'s module docstring and the exit-code-5 row caveat in README.md
  - honesty: the new live test red-bars on today's code (it would have caught the gap) and greens with the fix; afterwards grep finds no #16 reference left in README.md or tests/`test_stop_verb.py`
- \#18 settles as a narrow declared API: ship an importable headspace.api facade over the supported operations (create/run/put/export/destroy) with a declared `__all__`, versioned under semver — headspace.core stays private behind it, and the facade is documented in README so a consumer can see the supported surface from the outside (q1)
  - instruction: add a headspace/api.py facade exposing the five supported operations (create, run, put, export, destroy) as thin typed functions over the existing orchestration layer, with a declared `__all__`; document it in a README 'Python API' section stating the semver promise; answer #18 citing the section; exact function signatures are decided in the plan against core/workspace.py's real orchestration entry points
  - honesty: importing headspace.api succeeds without a reachable engine and exposes exactly the names its `__all__` declares — no consumer needs to reach into headspace.core for the five supported operations
- \#16's discriminator is the sentinel shape: stop writes a sentinel inside the workspace volume via an exec before it signals, and run, on observing a non-zero exit, reads the sentinel through the engine and records the job cancelled with `exit_status` None — a positive signal set by stop, never a heuristic (q2)
  - instruction: stop, after finding a live job and before signalling, execs a namespaced sentinel carrying the `job_id` it signalled into the workspace volume; run, when its wait returns non-zero and the sentinel names the job it just ran, classifies cancelled with `exit_status` None and removes the sentinel; the forgery risk recorded on this claim is resolved in the plan (sentinel content / engine-side corroboration)
  - honesty: the sentinel is namespaced to headspace, is cleaned up by run after classification, and stop remains lock-free and store-free — the preview inertness proofs in `test_stop_verb` keep passing unchanged

## Honesty conditions

- the announcement is backed by its own record: deviate --list shows d1-d5 approved, the live Docker stop test passes, headspace.api imports cleanly, and issues #15/#16/#18 are closed with a comment citing the delivering change
- the existing CLI-contract test suite (verbs, result package sections, exit-code taxonomy) passes unchanged through both the #16 and #18 deliveries — no documented flag, package section, or exit-code slot changes meaning
- `test_stop_verb`'s three-surface inertness proofs (UntouchableProvider, LockRefusingStore, byte-identical state.json/journal.jsonl) pass unchanged after the sentinel lands — the discriminator added no lock, no store write
- the ledger shows d1-d5 approved only after the operator's explicit in-channel instruction (q3, 2026-07-30); no deviate --confirm ran before that instruction
- each named audience maps to a delivered artifact: the run operator to the approved ledger, the stopping operator to the live Docker cancelled test, embodiment to the README Python API section that answers #18
- each cited pain is sourced, not asserted: failure/exit 6 on a stopped job was verified live (#16), the undeclared surface was measured by embodiment's `__all__` probe (#18), and the five proposed deviations sat in the committed ledger until adjudication
- every clause of the after-state is independently checkable at delivery: the live cancelled test, the import probe, deviate --list, and a grep for the retired caveats — none accepted from memory
- each success signal is executable as written — a pytest node, a python -c import probe, a devague deviate --list read, and a grep — and all run green at delivery

## Success signals

- success signals: a live Docker test stops a running job and asserts the run invocation exits 5 with package status cancelled and `exit_status` null; python -c 'import headspace.api' exposes exactly the declared facade without reaching headspace.core; devague deviate --list shows d1-d5 approved; grep finds no #16 caveat left in README or `test_stop_verb.py`

## Scope / boundaries

- the CLI contract remains the supported surface regardless of the import-surface decision: the documented flags, the nine-section result package, and the 0-8 exit taxonomy (README 'Exit codes' table) must not regress — #18 explicitly does not claim the current layout is wrong
- stop stays engine-side only: it takes no workspace lock and writes nothing under ~/.headspace (docker.py stop docstring; `test_stop_verb.py` proves inertness against three surfaces at once) — so the cancellation discriminator cannot be a state file; it must be visible to both processes through the engine
- deviation adjudication is user-only: devague deviate --confirm/--reject refuse an agent caller by contract (CLI help marks them user-only; the delivery doc records 'An agent cannot confirm its own deviation') — this handling prepares the decision, the operator executes it

## Non-goals

- no guessing classifier: inferring cancellation from an exit-137-inside-the-grace-window heuristic is out — docker.py's own precedent (lines 250-258) is that 137 with OOMKilled:false is indistinguishable from a deliberate SystemExit(137), so `_status` reads State.OOMKilled and never infers from the number; the discriminator must be a positive signal set by stop

## Assumptions

- the orchestration half of c43 already exists and needs no change: workspace.py:417 maps `STATUS_CANCELLED` to `EXIT_CANCELLED` (5), workspace.py:1552 gives the stop verb's own package status cancelled, and JobOutcome (providers/base.py) already admits `STATUS_CANCELLED` while refusing it an `exit_status` (`_NO_EXIT_STATUSES`) — only the provider-side classification is missing
- the confirm move still targets the file-in-path-14 plan after this frame exists: deviate --plan defaults to the current plan, .devague/`current_plan` reads file-in-path-14, and creating this frame changed only .devague/current (the frame), not the plan pointer

## Scope exploration

- `s1` — `headspace/__init__.py`: the package advertises exactly one name: `__all__` = \['`__version__`'\]; nothing else is declared public, which is the measured gap #18 reports
  - seeds: `c2`
- `s2` — `headspace/core/__init__.py`: no `__all__` at any level of core; the docstring pins layering (core never imports a provider, spec claim c4) but says nothing about external consumption — workspace/policy/result are reachable yet undeclared
  - seeds: `c2`
- `s3` — `pyproject.toml`: packaging is CLI-first: name headspace-cli, a console script headspace = headspace.cli:main, docker>=7.1,<8 as the only runtime dependency; nothing marks a library surface, but nothing disclaims one either
  - seeds: `c2`, `c3`
- `s4` — `README.md`: documents the CLI as the contract — verbs, the nine-section result package, the 0-8 exit taxonomy — and contains no statement about the Python import surface at all; its exit-code-5 row already records the #16 gap with a link, so the fix must retire that caveat
  - seeds: `c3`, `c8`
- `s5` — `headspace/providers/docker.py::_status (2747-2765)`: classifies from `timed_out` and State.OOMKilled only, never from the exit number — 137/OOMKilled:false is documented as indistinguishable from SystemExit(137) — and has no branch producing `STATUS_CANCELLED`; this is the exact missing half of c43
  - seeds: `c4`, `c6`
- `s6` — `headspace/providers/docker.py::stop (2269-2358)`: engine-side only by contract: own engine connection, no store, no lock, no writes under ~/.headspace; finds the job by label, SIGTERM with `STOP_GRACE_SECONDS` then kill; already tolerates racing run's reap (suppresses NotFound) — any discriminator write must fit inside this boundary
  - seeds: `c5`
- `s7` — `headspace/providers/base.py (JobOutcome, StopOutcome, JOB_STATUSES)`: the seam already speaks cancelled: `STATUS_CANCELLED` sits in `JOB_STATUSES` and `_NO_EXIT_STATUSES` forces `exit_status` None for it (JobOutcome.`__post_init__` refuses a cancelled outcome carrying 137) — so the Docker fix must drop the exit status, not just rename the status
  - seeds: `c4`, `c7`
- `s8` — `headspace/core/workspace.py (417, 1530-1552)`: orchestration is done: `STATUS_CANCELLED` maps to `EXIT_CANCELLED` (5) in the exit table, and the stop verb's own package already reads cancelled when StopOutcome.stopped is true — verified on both backends per `test_stop_verb`
  - seeds: `c7`
- `s9` — `tests/test_stop_verb.py (module docstring)`: pins the job-record contract on the fake provider only and records the Docker gap explicitly ('no test pretends Docker already meets it'); the fake proves preview inertness against three surfaces at once — the gap paragraph is the second notice the fix retires
  - seeds: `c8`
- `s10` — `.devague/deliveries/file-in-path-14.json`: the ledger holds FIVE proposed deviations, not the two issue #15 titles: d1/d2/d4 classified acceptable, d3 and d5 needs-follow-up; d5 is issue #16's substance ('c43 ships half-met'), so confirming d5 and fixing #16 are separate acts — one adjudicates the record, the other closes the gap
  - seeds: `c9`
- `s11` — `docs/deliveries/2026-07-29-file-in-path-14.md`: the delivery doc says all five await 'devague deviate --confirm d1 d2 d3 d4 d5' and that an agent cannot confirm its own deviation; it also names d3's follow-up options (namespace live-engine object names per run, or document the integration suite as single-writer) — real work no open issue covers
  - seeds: `c9`
- `s12` — `devague CLI (deviate --help) + .devague/current_plan`: deviate --confirm/--reject are marked user-only and --plan defaults to the current plan; .devague/`current_plan` still reads file-in-path-14 after this frame was created (verified post-new), so the operator's confirm needs no --plan flag
  - seeds: `c9`, `c10`

## Hard questions

- risk: a job can write anywhere inside its own workspace volume, so a forgeable sentinel path would let a job that exits non-zero masquerade as operator-cancelled; the plan must make operator-stop and job-forgery distinguishable — e.g. the sentinel carries the `job_id` stop actually signalled, or is corroborated by engine-side evidence such as the exec record

## Open parks

- [unknown_nonblocking] d3's follow-up is real unclaimed work no open issue covers: two concurrent suites collide on one Docker daemon because integration tests use deterministic object names (headspace-<`workspace_id`>) — either namespace engine objects per run or document the integration suite as single-writer
