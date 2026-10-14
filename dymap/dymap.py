"""One run of pl-dymap: plan, write the plan, post it, write what happened.

The body is callable from a test without the plugin machinery, with CUBE replaced by
a stand-in and the environment by a mapping. Both records go to the output directory,
``plan.json`` before anything is created and ``created.json`` after, so the decision
and its result are part of the feed: a refused plan or an unreachable CUBE leaves a
``created.json`` that says why.

Key components:
    - ENV_TOKEN, ENV_PREVIOUS, ENV_URL: the environment variables CUBE sets, or may
    - json_write: a record to the output directory
    - outcome_print: one item's line on the console
    - dymap_run: the run

Dependencies:
    - dymap.cube, dymap.plan, dymap.post, dymap.models
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from dymap.cube import CubeApi, CubeClient
from dymap.models import ItemOutcome, Plan, RunRecord
from dymap.plan import PlanRefused, params_parse, plan_build
from dymap.post import plan_post

ENV_TOKEN: str = "CHRIS_USER_TOKEN"
ENV_PREVIOUS: str = "CHRIS_PREV_PLG_INST_ID"
ENV_URL: str = "CHRIS_URL"
PLAN_FILE: str = "plan.json"
RECORD_FILE: str = "created.json"

ClientMake = Callable[[str, str], CubeApi]


def json_write(path: Path, data: dict[str, Any]) -> None:
    """Write a record as indented JSON.

    Example:
        >>> import tempfile; target = Path(tempfile.mkdtemp()) / "x.json"
        >>> json_write(target, {"a": 1}); json.loads(target.read_text())["a"]
        1
    """
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")


def outcome_print(outcome: ItemOutcome) -> None:
    """One item's line on the console.

    Example:
        >>> outcome_print(ItemOutcome(item="sub-01", title="sub-01", status="posted", workflowId=12))
        posted   sub-01  workflow 12
    """
    detail: str = f"workflow {outcome.workflowId}" if outcome.workflowId is not None else outcome.error
    print(f"{outcome.status:<8} {outcome.item}  {detail}".rstrip())


def usage_fail(message: str) -> int:
    """Report a usage error on standard error; the exit status for it.

    Example:
        >>> usage_fail("no pipeline")
        2
    """
    print(f"dymap: {message}", file=sys.stderr)
    return 2


def previousId_read(environ: Mapping[str, str]) -> int | None:
    """The data node's id from CUBE's environment, ``None`` when not set.

    Raises:
        ValueError: the variable is set but is not an integer.

    Example:
        >>> previousId_read({"CHRIS_PREV_PLG_INST_ID": "42"}), previousId_read({})
        (42, None)
    """
    text: str = environ.get(ENV_PREVIOUS, "").strip()
    return int(text) if text else None


def dymap_run(
    options: argparse.Namespace,
    inputdir: Path,
    outputdir: Path,
    clientMake: ClientMake = CubeClient,
    environ: Mapping[str, str] | None = None,
) -> int:
    """One run: plan, record the plan, post it unless a dry run, record what happened.

    Args:
        options: the parsed flags.
        inputdir: the data node's output, as ChRIS mounts it.
        outputdir: where ``plan.json`` and ``created.json`` go.
        clientMake: builds the CUBE client from a URL and a token.
        environ: the environment, ``os.environ`` when not given.

    Returns:
        0 when every item was posted, skipped, or, on a dry run, planned; 1 when the
        plan was refused or rejected, CUBE could not be reached, or any item failed;
        2 on a usage error.

    Example:
        status = dymap_run(parser.parse_args(["--pipeline", "p", "--dryRun"]), Path("in"), Path("out"))
    """
    env: Mapping[str, str] = os.environ if environ is None else environ
    if not options.pipeline:
        return usage_fail("--pipeline names the pipeline each item gets; it is required")
    if options.maxItems < 1:
        return usage_fail("--maxItems must be at least 1")
    if options.itemDepth < 1:
        return usage_fail("--itemDepth must be at least 1")
    try:
        params: dict[str, dict[str, Any]] = params_parse(options.params)
        previousId: int | None = previousId_read(env)
    except ValueError as err:
        return usage_fail(str(err))

    try:
        plan: Plan = plan_build(
            inputdir,
            options.pipeline,
            itemPiping=options.itemPiping,
            itemParam=options.itemParam,
            itemTemplate=options.itemTemplate,
            params=params,
            dirFilter=options.dirFilter,
            dirFilterLogic=options.dirFilterLogic,
            itemDepth=options.itemDepth,
            maxItems=options.maxItems,
            previousId=previousId,
        )
    except PlanRefused as err:
        json_write(outputdir / RECORD_FILE, RunRecord("", None, None, previousId, error=str(err)).toDict())
        print(f"dymap: plan refused: {err}", file=sys.stderr)
        return 1
    json_write(outputdir / PLAN_FILE, plan.toDict())

    report: Callable[[ItemOutcome], None] | None = outcome_print if options.verbosity > 0 else None
    if options.dryRun:
        record: RunRecord = RunRecord("", None, None, previousId)
        record.outcomes = [
            ItemOutcome(item=item.item, title=item.title, status="planned") for item in plan.items
        ]
        for outcome in record.outcomes:
            if report is not None:
                report(outcome)
        json_write(outputdir / RECORD_FILE, record.toDict())
        return 0

    url: str = options.CUBEurl or env.get(ENV_URL, "")
    if not url:
        json_write(
            outputdir / RECORD_FILE, RunRecord("", None, None, previousId, error="no CUBE URL").toDict()
        )
        return usage_fail(f"--CUBEurl names the CUBE API to post to; {ENV_URL} is not set either")
    token: str = env.get(ENV_TOKEN, "")
    if not token:
        record = RunRecord(url, None, None, previousId, error=f"{ENV_TOKEN} is not set")
        json_write(outputdir / RECORD_FILE, record.toDict())
        print(f"dymap: {record.error}; run inside ChRIS, or set it", file=sys.stderr)
        return 1

    record = plan_post(plan, clientMake(url, token), report=report)
    json_write(outputdir / RECORD_FILE, record.toDict())
    if record.error:
        print(f"dymap: {record.error}", file=sys.stderr)
    if options.verbosity > 0:
        counts: dict[str, int] = record.counts()
        print(f"{counts['posted']} posted, {counts['skipped']} skipped, {counts['failed']} failed")
    return 0 if record.status else 1
