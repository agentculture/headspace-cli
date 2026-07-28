"""``headspace inspect`` — headspace's view of a workspace, and the engine's, side by side.

WHY ``--logs`` exists
---------------------
:mod:`headspace.core.result` bounds every package to 8 KiB and, whenever it
drops bytes, prints the command that gets them back::

    ...[truncated: 512 of 91234 bytes shown -- full text: `headspace inspect job-7 --logs`]

That marker is the context-return contract's honesty clause — compression may
hide volume, never its own existence — and a marker naming a flag that does not
exist would turn the clause into a lie the CLI ships in every truncated result.
So ``--logs`` is that flag, and it does exactly what the marker promises: it
returns the captured output the store holds, in full, unbounded.

Unbounded is the point, so it is opt-in and it is the only thing in this CLI
that is. ``--max-result-bytes`` does not apply: a caller who asked for the full
text and got a bounded excerpt has been told no while being told yes.

The handle is whatever the marker printed. That is a *job* id when a job
produced the output and a workspace id otherwise (see
:attr:`~headspace.core.result.Provenance.reference`), so this verb accepts both:
a workspace id yields every job it kept, a job id yields that one job. Anything
else is refused by name rather than answered with an empty list, because "no
such handle" and "that job produced no output" are different facts.

Reading rather than orchestrating
---------------------------------
``--logs`` goes to :class:`~headspace.core.store.Store` directly instead of
through the orchestrator. It is a pure read of what was already persisted:
there is no engine to ask (captured output is a store fact, not an engine one),
nothing to reconcile, and no result package to bound. The plain verb does go
through the orchestrator, and so reconciles, probes the engine, and reports
both views the way the other four verbs do.
"""

from __future__ import annotations

import argparse
import contextlib
from typing import Any

from headspace.cli._commands.create import add_common_flags, emit_package, orchestrator_for
from headspace.cli._errors import EXIT_USER_ERROR, CliError
from headspace.cli._output import emit_result
from headspace.core.store import Store


def collect_logs(store: Store, handle: str) -> dict[str, Any]:
    """Every job the store kept for ``handle``, which may name a workspace or a job.

    One structure, from which both renderings are derived — the same rule the
    result package keeps, applied to the one payload that is not a result
    package.
    """
    if store.exists(handle):
        return _payload(handle, handle, _jobs(store, handle))

    for workspace_id in store.list_workspaces():
        for job in _jobs(store, workspace_id):
            if str(job.get("job_id", "")) == handle:
                return _payload(handle, workspace_id, [job])

    raise CliError(
        code=EXIT_USER_ERROR,
        message=f"no workspace or job named {handle!r} is recorded in this store",
        remediation=(
            "pass the handle the truncation marker printed — a job id, or the workspace id "
            "when no job produced the output. A destroyed workspace takes its jobs with it"
        ),
    )


def _jobs(store: Store, workspace_id: str) -> list[dict[str, Any]]:
    """The job records of one workspace; an unreadable record contributes none.

    Suppressed rather than raised because this walk is a *search*: one corrupt
    record must not hide a job that is sitting readable in the next workspace.
    """
    with contextlib.suppress(CliError):
        return [dict(job) for job in store.read_state(workspace_id).state.get("jobs", [])]
    return []


def _payload(handle: str, workspace_id: str, jobs: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "handle": handle,
        "workspace_id": workspace_id,
        "jobs": [
            {
                "job_id": str(job.get("job_id", "")),
                "status": str(job.get("status", "")),
                "exit_status": job.get("exit_status"),
                "truncated": bool(job.get("truncated", False)),
                "output_bytes": int(dict(job.get("usage", {})).get("output_bytes", 0)),
                "output": str(job.get("output", "")),
            }
            for job in jobs
        ],
    }


def render_logs(payload: dict[str, Any]) -> str:
    """The text rendering of :func:`collect_logs`, walked from the same structure."""
    lines = [f"# captured output for {payload['workspace_id']}", ""]
    if not payload["jobs"]:
        lines.append("(no jobs are recorded for this handle)")
        return "\n".join(lines)
    for job in payload["jobs"]:
        lines.append(
            f"## job {job['job_id']} — {job['status']}, exit status {job['exit_status']}, "
            f"{job['output_bytes']} bytes produced"
        )
        if job["truncated"]:
            lines.append(
                "(the workspace's output budget cut this at capture time; these are the "
                "bytes headspace kept, and the rest was never stored)"
            )
        lines.append("")
        lines.append(job["output"])
        lines.append("")
    return "\n".join(lines).rstrip("\n")


def cmd_inspect(args: argparse.Namespace) -> int:
    json_mode = bool(getattr(args, "json", False))
    if args.logs:
        payload = collect_logs(Store(), args.workspace_id)
        emit_result(payload if json_mode else render_logs(payload), json_mode=json_mode)
        return 0
    return emit_package(orchestrator_for(args).inspect(args.workspace_id), args)


def register(sub: argparse._SubParsersAction) -> None:
    parser = sub.add_parser(
        "inspect",
        help="Report a workspace's lifecycle state, the engine's view of it, and what "
        "the session has cost.",
    )
    parser.add_argument(
        "workspace_id",
        metavar="HANDLE",
        help="Workspace id. With --logs, a job id is accepted too — whichever handle a "
        "truncation marker printed.",
    )
    parser.add_argument(
        "--logs",
        action="store_true",
        help="Return the captured output in full instead of a bounded result package. "
        "This is the path truncation markers name; --max-result-bytes does not apply.",
    )
    add_common_flags(parser)
    parser.set_defaults(func=cmd_inspect)
