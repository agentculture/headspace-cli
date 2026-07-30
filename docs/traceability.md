# Traceability: MVP acceptance criteria to tests

This table maps every acceptance criterion in section 11 of
`docs/headspace_cli_issue_requirements.docx` to the test that discharges it.

**Read the verdicts literally.** `Full` means a test exercises the criterion as
written. `Partial` means part of the criterion is tested and part is not, and
the "What is missing" column says which part. `None` means nothing in the suite
covers it. A criterion is not marked `Full` because a test's *name* sounds
right — every citation below was opened and read, and two names that sounded
right turned out not to cover what they appeared to
([see Notes](#notes-on-tests-whose-names-overstate-their-coverage)).

Summary: **10 full, 5 partial, 0 uncovered** of the 15 criteria, plus the MVP
success test, which is full. Criterion 10 moved from `Partial` to `Full` when
issue #16 closed and cancellation gained a producer for the job's own recorded
outcome as well as for the `stop` verb; the superseded verdict is preserved in
that row rather than deleted.

## The criteria

| # | Criterion (section 11) | Verdict | Discharged by | What is missing |
|---|---|---|---|---|
| 1 | A caller can create a headspace with a declared purpose, owner, runtime profile, retention mode, and resource budget. | Partial | Profile: `tests/test_cli_verbs.py::test_create_reports_a_ready_workspace`, `tests/test_cli_verbs.py::test_create_rejects_an_unknown_profile`, `tests/test_profiles.py::test_every_registered_profile_is_digest_pinned`. Budget: `tests/test_cli_verbs.py::test_budget_flags_reach_the_stored_policy`, `tests/test_cli_verbs.py::test_every_budget_field_is_reachable_from_the_flag_surface`, `tests/test_integration_docker.py::test_h6_a_callers_declared_budget_lands_on_the_engines_own_workspace_container`. Both together: `tests/test_mvp_e2e.py::test_the_caller_gets_an_identity_and_a_lifecycle_state` | **Three of the five declarations do not exist.** `create` accepts `--profile`, `--network`, `--allow-host-path` and the budget flags — there is no workspace-level *purpose*, no *owner*, and no *retention mode* anywhere in `Orchestrator.create`, the stored record, or the CLI. (`purpose` exists only on an *artifact* declaration, `--declare NAME=PURPOSE`; `retention` only on an artifact record.) Untestable until the fields exist. |
| 2 | The system returns a stable headspace identity and an explicit lifecycle state. | Full | `tests/test_mvp_e2e.py::test_the_caller_gets_an_identity_and_a_lifecycle_state` (the same id answers all seven verbs of one delegation), `tests/test_cli_verbs.py::test_create_honours_an_explicit_workspace_id`, `tests/test_cli_verbs.py::test_create_rejects_a_duplicate_workspace_id`, `tests/test_states.py::test_nine_states_exist`, `tests/test_states.py::test_full_transition_matrix` | — |
| 3 | At least one isolated execution backend is supported without exposing backend-specific behaviour in the primary result contract. | Full | Provider seam, both backends: `tests/test_provider_docker.py::TestDockerProviderConformance::test_returned_structures_are_backend_neutral`, `tests/test_provider_fake.py::TestFakeProviderConformance::test_returned_structures_are_backend_neutral`. Rendered CLI output: `tests/test_mvp_e2e.py::test_no_default_output_requires_knowing_about_containers`, `tests/test_mvp_e2e.py::test_the_result_names_the_environment_without_naming_the_engine` | — The e2e test compares against the live engine's own container ids, image ids and volume names rather than a guessed vocabulary. The backend's *name* ("docker") is deliberately visible; its mechanics are not. |
| 4 | Multiple jobs can run within one session and share only the workspace state permitted by policy. | Full | Several jobs, one workspace: `tests/test_provider_docker.py::TestDockerProviderConformance::test_a_workspace_hosts_several_jobs_and_stays_ready`, `tests/test_states.py::test_running_returns_to_ready_so_one_workspace_can_host_many_jobs`, `tests/test_mvp_e2e.py::test_a_second_job_reads_the_first_job_s_state_and_answers_compactly` (job 2 reads the file job 1 wrote). Nothing else is shared: `tests/test_integration_docker.py::test_h20_a_job_run_through_the_orchestrator_has_no_socket_no_host_binds_no_network_and_only_the_workspace_volume_writable` | — Note that "permitted by policy" is only ever the closed case in the MVP: `--allow-host-path` fails closed on every host the Docker provider supports (`tests/test_policy.py::test_unsatisfiable_filesystem_scope_expansion_raises`), so an *expanded* filesystem policy is refused rather than shared. |
| 5 | Filesystem, network, execution time, memory, storage, and output limits are explicit and observable. | Full | Explicit: `tests/test_policy.py::test_policy_defaults_are_closed`, `tests/test_policy.py::test_effective_policy_labels_every_limit`, `tests/test_policy.py::test_cli_side_limits_are_always_enforced_by_headspace_itself`. Observable by the caller: `tests/test_mvp_e2e.py::test_every_declared_limit_is_visible_to_the_caller` (every limit class named in `create`, `run` and `inspect` output, with storage labelled `measured`) | — |
| 6 | The caller can stage declared inputs and later identify which inputs contributed to the result. | Full | Identification: `tests/test_mvp_e2e.py::test_the_result_names_the_inputs_that_produced_it` — `provenance.inputs` carries the job's argv, asserted in both jobs' results. **No pre-existing test covers this**; grepping `tests/` for `provenance.inputs` / `"inputs"` returns nothing | **Staging now exists** (0.10.0, issue #14). `headspace put` and `run --input NAME=HOST_PATH` copy a host file or directory in, over a new provider seam verb `write`; `headspace/core/inputs.py` expands a payload and digests it in one streaming pass. Identification is by **path and sha256**, recorded in an `inputs` ledger distinct from the artifact inventory and in `provenance.inputs` — never by content, which is the point: `tests/test_recording_surfaces.py` plants a marker in a copied-in file and greps `outcome_summary`, `provenance.inputs`, `journal.jsonl` and `state.json` for it, asserting the path and digest are present and the content is absent. Conformance holds both backends to it (`tests/conformance.py::test_write_lands_bytes_a_read_finds_afterward` and siblings). |
| 7 | A completed job returns a compact structured summary, normalized status, selected evidence, resource usage, and provenance. | Full | Shape: `tests/test_result.py::test_package_carries_exactly_the_nine_contract_sections`, `tests/test_result.py::test_status_vocabulary_is_the_shared_seven`, `tests/test_result.py::test_both_renderings_derive_from_one_structure`. Compactness, measured against a real 1.7 MB job: `tests/test_mvp_e2e.py::test_the_default_result_is_compact_against_what_the_job_emitted` (1,720,000 bytes emitted, 8,192 returned, 210x) | — |
| 8 | Large logs and intermediate files do not enter the default result; they remain inspectable through a separate path. | Partial | Do not enter: `tests/test_result.py::test_default_package_never_carries_raw_logs`, `tests/test_result.py::test_multi_megabyte_capture_fits_the_byte_bound`, `tests/test_mvp_e2e.py::test_the_raw_transcript_did_not_enter_the_default_result` (54 of 20,000 trace lines came back; the tail and the middle of the transcript are absent by name). Separate path: `tests/test_cli_verbs.py::test_inspect_logs_returns_the_output_the_marker_promises`, `tests/test_mvp_e2e.py::test_the_transcript_stays_retrievable_through_the_separate_path` | [Defect 1](#defect-1-the-truncation-marker-names-a-command-that-does-not-parse) (malformed pointer) is **FIXED** — `tests/test_mvp_e2e.py::test_the_truncation_marker_names_a_command_a_caller_can_run` now passes. Remaining gap: an *intermediate file* is only retrievable if it was declared — `export` refuses an undeclared name (`tests/test_cli_verbs.py::test_export_refuses_an_undeclared_artifact`) — so an undeclared file written by a job is inspectable through no path at all. |
| 9 | The caller can explicitly export named artifacts and receive their identity, integrity digest, type, and purpose. | Full | `tests/test_mvp_e2e.py::test_the_artifact_digest_survives_the_round_trip` (name, purpose, media type, digest, size and host reference all in the export result; digest agreed by the job inside the workspace, by `export`, by `--expect-sha256` and by the host). Unit level: `tests/test_artifacts.py::test_every_field_of_an_exported_record_is_populated`, `tests/test_artifacts.py::test_export_refuses_to_publish_on_digest_mismatch`, `tests/test_cli_verbs.py::test_export_publishes_and_verifies`, `tests/test_cli_verbs.py::test_export_checks_an_expected_digest` | — |
| 10 | A failed or timed-out job clearly separates computational failure, policy denial, cancellation, and infrastructure failure. | Full | Computational failure: `tests/test_cli_verbs.py::test_run_maps_a_failing_job_to_the_taxonomy`, `tests/test_provider_docker.py::TestDockerProviderConformance::test_run_reports_a_failing_job_without_calling_it_infrastructure`. Timeout: `tests/test_cli_verbs.py::test_run_maps_a_timeout_to_the_taxonomy`, `tests/test_provider_docker.py::TestDockerProviderConformance::test_a_job_over_its_wall_clock_budget_times_out`. Policy denial: `tests/test_policy.py::test_policy_error_is_a_cli_error_carrying_the_policy_denied_code`, `tests/test_integration_docker.py::test_h6_a_policy_the_host_cannot_enforce_fails_before_create_touches_the_engine`. Infrastructure: `tests/test_workspace.py::test_infrastructure_failure_is_never_reported_as_computational_failure`, `tests/test_provider_docker.py::TestDockerProviderConformance::test_engine_breakage_raises_infrastructure_failure`. A command the image cannot execute is computational failure, not infrastructure: `tests/test_docker_classification.py::test_run_reports_a_not_executable_command_as_the_callers_failure` (all four probed engine wordings, `exit_status` 126/127), `tests/test_docker_classification.py::test_the_package_reports_a_failed_computation_not_a_broken_engine`, `tests/test_docker_classification.py::test_the_cli_exits_six_and_leaks_nothing_on_a_command_the_image_cannot_run` (the real process exit code, no engine string leaked); the fail-safe boundary — an engine 400 that is *not* the exec step still stays `infrastructure_failure` — is pinned by `tests/test_docker_classification.py::test_run_still_calls_any_other_engine_400_an_infrastructure_failure`. `resource_exhausted` (8) is wired as a distinguishable member of the taxonomy, separate from `computation_failed`: `tests/test_exit_codes.py::test_resource_exhausted_is_exit_code_eight`, `tests/test_exit_codes.py::test_resource_exhausted_is_named_in_exit_categories_and_taxonomy_codes`, `tests/test_exit_codes.py::test_resource_exhausted_is_distinguishable_from_the_existing_band_in_text`, `tests/test_workspace.py::test_exit_code_for_status_uses_the_documented_taxonomy` (now parametrized over seven of the eight statuses — all but `partial_success`, which shares exit 0 with `success`), `tests/test_workspace.py::test_every_status_has_a_row_in_the_exit_code_mapping_and_its_category`. A real OOM kill produces it end to end: `tests/test_docker_classification.py::test_an_oom_kill_is_reported_as_resource_exhausted` and `tests/test_docker_classification.py::test_the_cli_exits_eight_on_a_job_the_engine_marks_oomkilled` (the real process exit code), with the 137 ambiguity pinned by `tests/test_docker_classification.py::test_an_honest_exit_137_without_oomkilled_is_an_ordinary_failure` and timeout precedence by `tests/test_docker_classification.py::test_a_wall_clock_kill_reports_timeout_even_when_oomkilled_is_true`. All four original categories nameable: `tests/test_exit_codes.py::test_category_names_match_declared_vocabulary`, `tests/test_exit_codes.py::test_categories_are_distinguishable_from_each_other_in_text`. Cancellation, both halves: the `stop` verb's own package on every backend (`tests/test_stop_verb.py`), and the *job's* separately recorded outcome, live against a real engine — `tests/test_integration_docker.py::test_stop_apply_ends_a_live_job_and_the_blocked_run_records_it_cancelled` (`cancelled`, `exit_status` `None`, the interrupted `run` exiting 5, driven from a genuinely separate process). The classification itself, and the reason it is sound rather than a guess about 137, is pinned by `tests/test_docker_classification.py` (the two-phase marker channel: countersignal-only classification, staleness rejection by `job_id`, clearing on every path including exit 0, and the documented precedence cancelled > timeout > `resource_exhausted`) and by `tests/test_provider_docker_control.py` (the write's symlink and non-regular-file refusals, and `stop`'s inertness across both marker writes) | **Nothing. Issue #16 closed and all four categories now separate for a job's own record, not only for the verb that ended it.** What made this hard is worth keeping: exit 137 with `OOMKilled: false` is byte-for-byte identical between a job the engine killed for `stop` and a job that called `sys.exit(137)`, so no reading of the engine's numbers could ever have separated them (pinned independently by `tests/test_docker_classification.py::test_an_honest_exit_137_without_oomkilled_is_an_ordinary_failure`). The fix is a positive cross-process signal — `stop` writes an intent marker into the workspace volume before it signals and a countersignal through the anchor after the signalling has run its course, and `run` classifies from the countersignal alone — because `run` holds the workspace lock for the job's whole duration, so `stop` cannot use headspace's own state to say anything. Every partial state of that channel classifies exactly as the pre-fix code did, so it can lose a cancellation's name but never invent one. Superseded verdicts follow, most recent first. **`Partial` (0.10.0): cancellation had a producer for the verb but not yet for the job's own record.** `headspace stop <ws> --apply` ended an in-flight job and reported `STATUS_CANCELLED` / exit 5 on every backend (`tests/test_stop_verb.py`), so the category was no longer unreachable. The remaining gap was narrower and was tracked as **issue #16**: the stopped job's *separately recorded* outcome is written by the concurrent `run` from what its backend reports, and `DockerProvider._status()` had no branch that could produce `STATUS_CANCELLED` — verified live, a job ended by `stop --apply` was recorded `failure` / exit 137 and its `run` exited 6. Closing it needed a cross-process discriminator, because exit 137 with `OOMKilled: false` is indistinguishable from a job that deliberately exited 137. Superseded text follows: `STATUS_CANCELLED` and `EXIT_CANCELLED` (5) exist and are distinguishable, but no code path returns a cancelled job outcome: there is no `cancel` verb and no signal handling in `Orchestrator.run`. `resource_exhausted` (8) *is* reachable: `DockerProvider._status()` takes `(timed_out, oom_killed, exit_status)` and reads `State.OOMKilled` off the reload `_await_exit` already performs, so a job the kernel kills at its memory ceiling produces it. Cancellation is now the taxonomy's only declared-but-unreachable category. (That last sentence is what issue #16 was, and it is no longer true of either half.) |
| 11 | The caller can inspect current state, job history, artifact inventory, policy, and retained evidence. | Full | `tests/test_mvp_e2e.py::test_one_inspect_shows_state_history_inventory_policy_and_evidence` — one `inspect` call asserted for all five: `lifecycle state: ready`, `2 job(s) recorded in this session`, both artifacts by name with their digests, the effective-policy line, and the retained excerpt of the last job. Also `tests/test_cli_verbs.py::test_inspect_reports_both_views`, `tests/test_workspace.py::test_inspect_reports_lifecycle_state_engine_facts_and_pending_work` | — |
| 12 | A headspace can be cancelled, expired, retained for a bounded period, or destroyed according to policy. | Partial | Destroyed: `tests/test_cli_verbs.py::test_destroy_removes_a_clean_workspace`, `tests/test_mvp_e2e.py::test_destroy_reports_what_it_removed_and_the_export_outlives_it`. Refused while a job is live: `tests/test_integration_docker.py::test_h18_destroy_refuses_while_a_job_is_genuinely_running_because_running_to_destroyed_is_absent`, `tests/test_states.py::test_running_has_no_direct_edge_to_destroyed`. The state machine for all four: `tests/test_states.py::test_full_transition_matrix`, `tests/test_states.py::test_every_state_covered_by_expected_table` | **Only destruction is reachable by a caller for the *workspace*; a *job* can now be stopped.** `headspace stop` (0.10.0) ends an in-flight job, and since issue #16 closed the job's own record says so too (see requirement 10) — but it acts on the job, not on the workspace's lifecycle state, so the rest of this row stands: `cancelled` is entered only as an internal hop on the teardown path (`REMOVAL_PATHS[READY] == (CANCELLED, DESTROYED)`, visible as `lifecycle path: cancelled -> destroyed`) or by crash reconciliation — there is no `cancel` verb. `expired` is in the enum and has an `expired -> destroyed` edge, but **nothing in `headspace/` ever sets it**. "Retained for a bounded period" has no implementation at all: no TTL, no expiry clock, no retention flag. Three of the four are state-machine capability, not product behaviour. |
| 13 | Destruction reports which workspace data was removed and which exported artifacts remain elsewhere. | Full | Removed: `tests/test_provider_docker.py::TestDockerProviderConformance::test_remove_reports_what_it_actually_removed` (removed/retained/unverified are a partition over a closed, backend-neutral vocabulary), `tests/test_mvp_e2e.py::test_destroy_reports_what_it_removed_and_the_export_outlives_it` (`removed: runtime, storage`, and the exported file still on disk and still correct afterwards) | [Defect 2](#defect-2-the-destruction-report-never-names-the-artifacts-that-survive) is **FIXED** — destroy now passes `_artifact_section(record)`, so the report names every exported artifact that outlives the workspace. `tests/test_mvp_e2e.py::test_destroy_names_the_exported_artifacts_that_remain_elsewhere` now passes. |
| 14 | The effective policy and any backend-imposed restrictions are visible to the caller. | Full | `tests/test_integration_docker.py::test_h15_the_effective_policy_view_in_the_result_package_labels_storage_measured` (against the real engine's real answer), `tests/test_policy.py::test_storage_is_measured_on_default_local_volume_driver`, `tests/test_policy.py::test_policy_error_never_weakens_the_requested_limit`, `tests/test_mvp_e2e.py::test_every_declared_limit_is_visible_to_the_caller` (the `(measured)` label and the accompanying warning are both required in the caller's own output) | — |
| 15 | An agent runtime can consume all primary operations and results without parsing human-formatted logs. | Full | `tests/test_cli_verbs.py::test_every_verb_declares_json`, `tests/test_cli_verbs.py::test_every_verb_emits_json_on_demand`, `tests/test_cli_verbs.py::test_json_and_markdown_carry_the_same_content`, `tests/test_exit_codes.py::test_category_recoverable_from_json_render`, `tests/test_result.py::test_last_resort_clip_leaves_json_structurally_complete`, `tests/test_result.py::test_markdown_and_json_carry_identical_fields`. Consumed structurally end to end: `tests/test_mvp_e2e.py` drives `inspect --logs --json` and reads the delegation's measurements out of the parsed payload rather than out of text | — |

## The MVP success test

> A reasoning model can delegate a non-trivial computation, keep the noisy
> working process outside its context, receive a compact evidence-bearing
> result, and recover the intended artifacts without understanding the
> underlying isolation technology.

| Clause | Verdict | Discharged by |
|---|---|---|
| delegate a non-trivial computation | Full | `tests/test_mvp_e2e.py::test_the_delegated_answer_matches_an_independent_recomputation` — 20,000 Collatz stopping times over a seeded LCG series, reduced to a total, an arg-max and an order-sensitive checksum, then checked against a second implementation on the host. |
| keep the noisy working process outside its context | Full | `tests/test_mvp_e2e.py::test_the_raw_transcript_did_not_enter_the_default_result` |
| receive a compact evidence-bearing result | Full | `tests/test_mvp_e2e.py::test_the_default_result_is_compact_against_what_the_job_emitted` (measured 210x), `tests/test_mvp_e2e.py::test_the_transcript_stays_retrievable_through_the_separate_path` |
| recover the intended artifacts | Full | `tests/test_mvp_e2e.py::test_the_artifact_digest_survives_the_round_trip`, `tests/test_mvp_e2e.py::test_destroy_reports_what_it_removed_and_the_export_outlives_it` |
| without understanding the underlying isolation technology | Full | `tests/test_mvp_e2e.py::test_no_default_output_requires_knowing_about_containers` — every step drives `headspace.cli.main`, and the audit compares the CLI's output against the live engine's own identifiers. |

## Defects found while writing this table

### Defect 1: the truncation marker names a command that does not parse

`Orchestrator.run` and `Orchestrator.inspect`
(`headspace/core/workspace.py`, the `Evidence(...)` construction in each) pass
an already-rendered `inspect_path(job_id)` as `Evidence.source`.
`headspace/core/result.py`'s `_excerpt_marker` then renders it *again*, so a
truncated result prints:

```text
...[truncated: 5128 of 1720000 bytes shown -- full text:
`headspace inspect headspace inspect job-338aa42faec5 --logs --logs`]
```

> **Resolved.** Fixed after this table was first written; the strict-xfail
> tripwire was removed once the test passed. Kept here because the reasoning
> is the record of why the fix was needed.

That command cannot be typed. The honesty clause the marker exists to serve —
compression may hide volume, never its own existence — is weakened to a
pointer that goes nowhere.

`tests/test_result.py` does not catch it because it builds
`Evidence(source="job-2")` with a *raw* reference, which is the contract
`inspect_path` expects; the double wrap only appears once the orchestrator
supplies the source. Fix: pass the bare `job_id`.

### Defect 2: the destruction report never names the artifacts that survive

> **Resolved.** Fixed after this table was first written; the strict-xfail
> tripwire was removed once the test passed. Kept here because the reasoning
> is the record of why the fix was needed.

`Orchestrator.destroy` builds its `ResultPackage` without an `artifacts=`
argument, so the Artifacts section of a destroy result always renders
`(none)` — even when the store's export ledger holds the name, digest and host
path of every artifact that outlives the workspace, and even though the very
next statement deletes that record. Fix: pass `_artifact_section(record)`, as
`run`, `inspect` and `export` already do.

## Notes on tests whose names overstate their coverage

Two citations were rejected after reading the test bodies. They are recorded
here because the next person to build this table will be tempted by the same
names. (A third, `tests/test_workspace.py::test_exit_code_for_status_uses_the_documented_taxonomy`,
used to belong here for parametrizing only `success`, `failure` and `timeout`;
it now parametrizes all seven statuses in `STATUSES`, so criterion 10 cites it
without qualification.)

1. `tests/test_provider_*.py::...::test_returned_structures_are_backend_neutral`
   walks the *provider seam's* dataclasses, not the CLI's rendered output. It
   is real evidence for criterion 3 at the seam, but on its own it says nothing
   about what a caller reads on stdout — which is why criterion 3 also cites
   `tests/test_mvp_e2e.py`.
2. `tests/test_cli_verbs.py::test_inspect_reports_both_views` asserts the state
   line and the job count only. It does not touch artifact inventory, policy or
   retained evidence, so it cannot carry criterion 11 by itself.

## How to re-run the evidence

```bash
uv run pytest -q                                   # the whole suite
uv run pytest -q tests/test_mvp_e2e.py             # the MVP success test alone
DOCKER_HOST=unix:///nonexistent/docker.sock uv run pytest -q   # no-engine lane
```

The Docker-backed suites skip cleanly with no reachable engine; they never
fail and never hang.
