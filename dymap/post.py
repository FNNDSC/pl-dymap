"""Acting on the plan: one workflow per item, created under the data node, each at most once.

Before creating anything the plan is checked against CUBE: the pipeline must exist by
exact name, the item piping must be one of its pipings, and the item parameter and
every ``--params`` entry must be parameters of the pipings they name. CUBE itself
ignores a parameter a piping does not have, so a misspelt item parameter would give
every child the whole input; the check refuses it instead.

Each item is then looked for among the data node's children: a child with the item's
title that belongs to a workflow of this pipeline means the item was done by an earlier
run, and is skipped. Otherwise one workflow is created. A failure on one item is
recorded and the rest go on; nothing waits for the children to run.

Key components:
    - PlanRejected: the plan does not fit the pipeline it names
    - target_resolve: the pipeline and item piping, checked
    - nodesInfo_build: the per-piping values for one item
    - item_post: one item, created or skipped
    - plan_post: the whole plan

Dependencies:
    - dymap.cube, dymap.models
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from dymap.cube import CubeApi
from dymap.models import (
    CubeError,
    InstanceRef,
    ItemOutcome,
    PipelineRef,
    PipingRef,
    Plan,
    PlanItem,
    RunRecord,
    Target,
)


class PlanRejected(Exception):
    """The plan does not fit the pipeline it names, so nothing is created.

    Example:
        >>> str(PlanRejected("no pipeline"))
        'no pipeline'
    """


def target_resolve(plan: Plan, client: CubeApi) -> Target:
    """The pipeline and item piping the plan names, checked against CUBE.

    Args:
        plan: the plan.
        client: CUBE.

    Returns:
        The target.

    Raises:
        PlanRejected: no pipeline of that exact name; no piping of the item piping's
            title, or several pipings and none named; the item parameter, or a
            ``--params`` entry, names a parameter its piping does not have.

    Example:
        target = target_resolve(plan, CubeClient(url, token))
    """
    pipeline: PipelineRef | None = client.pipeline_find(plan.pipeline)
    if pipeline is None:
        raise PlanRejected(f"no pipeline named {plan.pipeline!r}")
    pipings: list[PipingRef] = client.pipings_list(pipeline.id)
    if plan.itemPiping:
        named: list[PipingRef] = [piping for piping in pipings if piping.title == plan.itemPiping]
        if not named:
            titles: str = ", ".join(repr(piping.title) for piping in pipings)
            raise PlanRejected(
                f"pipeline {plan.pipeline!r} has no piping {plan.itemPiping!r}; it has {titles}"
            )
        itemPiping: PipingRef = named[0]
    elif len(pipings) == 1:
        itemPiping = pipings[0]
    else:
        raise PlanRejected(
            f"pipeline {plan.pipeline!r} has {len(pipings)} pipings; name one with --itemPiping"
        )

    pipingIds: dict[str, int] = {piping.title: piping.id for piping in pipings}
    paramNames: dict[int, set[str]] = {}
    for param in client.params_list(pipeline.id):
        paramNames.setdefault(param.pipingId, set()).add(param.name)
    if plan.itemParam not in paramNames.get(itemPiping.id, set()):
        raise PlanRejected(f"piping {itemPiping.title!r} has no parameter {plan.itemParam!r}")
    for title, values in plan.params.items():
        if title not in pipingIds:
            raise PlanRejected(f"--params names piping {title!r}, which pipeline {plan.pipeline!r} lacks")
        unknown: list[str] = sorted(set(values) - paramNames.get(pipingIds[title], set()))
        if unknown:
            raise PlanRejected(f"--params names parameters piping {title!r} lacks: {', '.join(unknown)}")
    return Target(pipeline=pipeline, itemPiping=itemPiping, pipingIds=pipingIds)


def nodesInfo_build(plan: Plan, target: Target, item: PlanItem) -> list[dict[str, Any]]:
    """The per-piping values for one item's workflow.

    The item piping gets the item's title and value; pipings named in ``--params``
    get those values; the rest take the pipeline's defaults.

    Example:
        >>> plan = Plan("p", "", "dirFilter", {}, 4, [])
        >>> target = Target(PipelineRef(3, "p"), PipingRef(7, "x", None), {"x": 7})
        >>> nodesInfo_build(plan, target, PlanItem("a", "a", "re:^a$"))[0]["plugin_parameter_defaults"]
        [{'name': 'dirFilter', 'default': 're:^a$'}]
    """
    nodes: dict[int, dict[str, Any]] = {}
    for title, values in plan.params.items():
        pipingId: int = target.pipingIds[title]
        nodes[pipingId] = {
            "piping_id": pipingId,
            "plugin_parameter_defaults": [{"name": name, "default": value} for name, value in values.items()],
        }
    itemNode: dict[str, Any] = nodes.setdefault(
        target.itemPiping.id, {"piping_id": target.itemPiping.id, "plugin_parameter_defaults": []}
    )
    itemNode["title"] = item.title
    itemNode["plugin_parameter_defaults"] = [
        entry for entry in itemNode["plugin_parameter_defaults"] if entry["name"] != plan.itemParam
    ] + [{"name": plan.itemParam, "default": item.value}]
    return list(nodes.values())


def existing_find(item: PlanItem, previousId: int, target: Target, client: CubeApi) -> InstanceRef | None:
    """A child of the data node that an earlier run made for this item, or ``None``.

    A child counts when its title is the item's and it belongs to a workflow of this
    pipeline; a child of the same title from another pipeline is someone else's.

    Example:
        found = existing_find(item, 4985, target, client)
    """
    for instance in client.children_find(previousId, item.title):
        if instance.workflowId is not None and client.workflow_get(instance.workflowId).pipelineId == (
            target.pipeline.id
        ):
            return instance
    return None


def item_post(item: PlanItem, plan: Plan, previousId: int, target: Target, client: CubeApi) -> ItemOutcome:
    """One item: skipped when an earlier run made it, otherwise its workflow created.

    Args:
        item: the plan's entry.
        plan: the plan.
        previousId: the data node.
        target: the resolved pipeline.
        client: CUBE.

    Returns:
        What happened; a CUBE error is recorded, not raised.

    Example:
        outcome = item_post(plan.items[0], plan, 4985, target, client)
    """
    try:
        found: InstanceRef | None = existing_find(item, previousId, target, client)
        if found is not None and found.workflowId is not None:
            instances: list[InstanceRef] = client.workflowInstances_list(found.workflowId)
            return ItemOutcome(
                item=item.item,
                title=item.title,
                status="skipped",
                workflowId=found.workflowId,
                instanceIds=sorted(instance.id for instance in instances),
            )
        workflow = client.workflow_post(
            target.pipeline.id,
            previousId,
            f"{plan.pipeline}: {item.item}"[:100],
            nodesInfo_build(plan, target, item),
        )
        created: list[InstanceRef] = client.workflowInstances_list(workflow.id)
        return ItemOutcome(
            item=item.item,
            title=item.title,
            status="posted",
            workflowId=workflow.id,
            instanceIds=sorted(instance.id for instance in created),
        )
    except CubeError as err:
        return ItemOutcome(item=item.item, title=item.title, status="failed", error=str(err))


def plan_post(
    plan: Plan,
    client: CubeApi,
    report: Callable[[ItemOutcome], None] | None = None,
) -> RunRecord:
    """The whole plan: checked against CUBE, then one workflow per item not already made.

    Args:
        plan: the plan, with the data node's id.
        client: CUBE.
        report: called with each item's outcome as it happens, for the console.

    Returns:
        The run's record; a rejected plan or an unreachable CUBE is recorded as the
        run's error, with no outcomes.

    Example:
        record = plan_post(plan, CubeClient(url, token), report=print)
    """
    record: RunRecord = RunRecord(
        cubeUrl=client.url, pipelineId=None, pipingId=None, previousId=plan.previousId
    )
    if plan.previousId is None:
        record.error = "no data node: CHRIS_PREV_PLG_INST_ID is not set"
        return record
    try:
        target: Target = target_resolve(plan, client)
    except (PlanRejected, CubeError) as err:
        record.error = str(err)
        return record
    record.pipelineId = target.pipeline.id
    record.pipingId = target.itemPiping.id
    for item in plan.items:
        outcome: ItemOutcome = item_post(item, plan, plan.previousId, target, client)
        record.outcomes.append(outcome)
        if report is not None:
            report(outcome)
    return record
