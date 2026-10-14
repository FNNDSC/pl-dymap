"""One run of pl-dymap: plan, write the plan, post it, write what happened.

The body is callable from a test without the plugin machinery, with CUBE replaced by
a stand-in and the job's environment by a record. Both records go to the output
directory, ``plan.json`` before anything is created and ``created.json`` after, so the
decision and its result are part of the feed: a refused plan or an unreachable CUBE
leaves a ``created.json`` that says why.

Key components:
    - json_write: a record to the output directory
    - outcome_print: one item's line on the console
    - usage_fail: a usage error
    - dymap_run: the run

Dependencies:
    - dymap.cube, dymap.plan, dymap.post, dymap.models
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from pathlib import Path

from dymap.cube import CubeApi, CubeClient
from dymap.models import (
    ENV_TOKEN,
    ENV_URL,
    ItemOutcome,
    JobEnv,
    Options,
    OutcomeCounts,
    ParamSetting,
    Plan,
    RunRecord,
)
from dymap.plan import PlanRefused, params_parse, plan_build
from dymap.post import plan_post

PLAN_FILE: str = "plan.json"
RECORD_FILE: str = "created.json"

ClientMake = Callable[[str, str], CubeApi]


def json_write(path: Path, record: Plan | RunRecord) -> None:
    """Write a record as indented JSON.

    Example:
        >>> import tempfile; target = Path(tempfile.mkdtemp()) / "x.json"
        >>> json_write(target, RunRecord("", 4)); json.loads(target.read_text())["previousId"]
        4
    """
    path.write_text(json.dumps(record.toDict(), indent=2, sort_keys=True) + "\n")


def outcome_print(outcome: ItemOutcome) -> None:
    """One item's line on the console.

    Example:
        >>> outcome_print(ItemOutcome(item="sub-01", title="sub-01", status="posted", workflowId=12))
        posted   sub-01  workflow 12
        >>> outcome_print(ItemOutcome(item="sub-02", title="sub-02", status="posted", instanceIds=[40]))
        posted   sub-02  instance 40
    """
    if outcome.workflowId is not None:
        detail: str = f"workflow {outcome.workflowId}"
    elif outcome.instanceIds:
        detail = f"instance {outcome.instanceIds[0]}"
    else:
        detail = outcome.error
    print(f"{outcome.status:<8} {outcome.item}  {detail}".rstrip())


def usage_fail(message: str) -> int:
    """Report a usage error on standard error; the exit status for it.

    Example:
        >>> usage_fail("no pipeline")
        2
    """
    print(f"dymap: {message}", file=sys.stderr)
    return 2


def options_check(options: Options) -> str:
    """Why the flags cannot be used together, or empty when they can.

    Example:
        >>> options_check(Options(pipeline="p", plugin="pl-x"))
        'name a --pipeline or a --plugin for each item, not both'
    """
    if bool(options.pipeline) == bool(options.plugin):
        if options.pipeline:
            return "name a --pipeline or a --plugin for each item, not both"
        return "--pipeline or --plugin names what each item gets; one is required"
    if options.plugin and options.itemPiping:
        return "--itemPiping names a piping of a pipeline; a --plugin has none"
    if options.maxItems < 1:
        return "--maxItems must be at least 1"
    if options.itemDepth < 0:
        return "--itemDepth must be 0, for the default, or more"
    return ""


def dymap_run(
    options: Options,
    inputdir: Path,
    outputdir: Path,
    env: JobEnv | None = None,
    clientMake: ClientMake = CubeClient,
) -> int:
    """One run: plan, record the plan, post it unless a dry run, record what happened.

    Args:
        options: the flags.
        inputdir: the data node's output, as ChRIS mounts it.
        outputdir: where ``plan.json`` and ``created.json`` go.
        env: the job's environment; read from ``os.environ`` when not given.
        clientMake: builds the CUBE client from a URL and a token.

    Returns:
        0 when every item was posted, skipped, or, on a dry run, planned; 1 when the
        plan was refused or rejected, CUBE could not be reached, or any item failed;
        2 on a usage error.

    Example:
        status = dymap_run(Options(pipeline="p", dryRun=True), Path("in"), Path("out"))
    """
    problem: str = options_check(options)
    if problem:
        return usage_fail(problem)
    try:
        params: list[ParamSetting] = params_parse(options.params, "plugin" if options.plugin else "pipeline")
        job: JobEnv = JobEnv.fromEnviron() if env is None else env
    except ValueError as err:
        return usage_fail(str(err))

    try:
        plan: Plan = plan_build(inputdir, options, params, job.previousId)
    except PlanRefused as err:
        json_write(outputdir / RECORD_FILE, RunRecord("", job.previousId, error=str(err)))
        print(f"dymap: plan refused: {err}", file=sys.stderr)
        return 1
    json_write(outputdir / PLAN_FILE, plan)

    report: Callable[[ItemOutcome], None] | None = outcome_print if options.verbosity > 0 else None
    if options.dryRun:
        record: RunRecord = RunRecord("", job.previousId)
        record.outcomes = [
            ItemOutcome(item=item.item, title=item.title, status="planned") for item in plan.items
        ]
        for outcome in record.outcomes:
            if report is not None:
                report(outcome)
        json_write(outputdir / RECORD_FILE, record)
        return 0

    url: str = options.CUBEurl or job.url
    if not url:
        json_write(outputdir / RECORD_FILE, RunRecord("", job.previousId, error="no CUBE URL"))
        return usage_fail(f"--CUBEurl names the CUBE API to post to; {ENV_URL} is not set either")
    if not job.token:
        record = RunRecord(url, job.previousId, error=f"{ENV_TOKEN} is not set")
        json_write(outputdir / RECORD_FILE, record)
        print(f"dymap: {record.error}; run inside ChRIS, or set it", file=sys.stderr)
        return 1

    record = plan_post(plan, clientMake(url, job.token), report=report)
    json_write(outputdir / RECORD_FILE, record)
    if record.error:
        print(f"dymap: {record.error}", file=sys.stderr)
    if options.verbosity > 0:
        counts: OutcomeCounts = record.counts()
        print(f"{counts.posted} posted, {counts.skipped} skipped, {counts.failed} failed")
    return 0 if record.status else 1
