"""Acting on the plan: one child per item, created under the data node, each at most once.

A child is a workflow of a pipeline or an instance of a plugin. Before creating
anything the plan is checked against CUBE: the pipeline or plugin must exist by exact
name, the item piping must be one of the pipeline's pipings, and the item parameter
and every ``--params`` entry must be parameters of what they name. CUBE itself ignores
a parameter a piping does not have, so a misspelt item parameter would give every
child the whole input; the check refuses it instead.

Each item is then looked for among the data node's children: a child with the item's
title that belongs to a workflow of this pipeline, or runs this plugin, means the item
was done by an earlier run, and is skipped. Otherwise one child is created. A failure
on one item is recorded and the rest go on; nothing waits for the children to run.

Key components:
    - PlanRejected: the plan does not fit what it names in CUBE
    - target_resolve: the pipeline and item piping, or the plugin, checked
    - nodesInfo_build: the per-piping values for one item's workflow
    - item_post: one item, created or skipped
    - plan_post: the whole plan

Dependencies:
    - dymap.cube, dymap.models
"""

from __future__ import annotations

from collections.abc import Callable

from dymap.cube import CubeApi
from dymap.models import (
    CubeError,
    InstanceRef,
    ItemOutcome,
    NodeInfo,
    OutcomeStatus,
    ParamSetting,
    PipelineRef,
    PipingRef,
    Plan,
    PlanItem,
    PluginRef,
    RunRecord,
    Target,
    WorkflowRef,
)

VERSION_SEPARATOR: str = "@"


class PlanRejected(Exception):
    """The plan does not fit what it names in CUBE, so nothing is created.

    Example:
        >>> str(PlanRejected("no pipeline"))
        'no pipeline'
    """


def unknownParams_check(where: str, names: set[str], settings: list[ParamSetting]) -> None:
    """Refuse settings whose names ``names`` lacks.

    Raises:
        PlanRejected: one or more unknown names.

    Example:
        >>> unknownParams_check("plugin 'pl-x'", {"a"}, [ParamSetting("", "a", 1)])
    """
    unknown: list[str] = sorted({setting.name for setting in settings} - names)
    if unknown:
        raise PlanRejected(f"--params names parameters {where} lacks: {', '.join(unknown)}")


def pipelineTarget_resolve(plan: Plan, client: CubeApi) -> Target:
    """The pipeline and item piping the plan names, checked against CUBE.

    Raises:
        PlanRejected: no pipeline of that exact name; no piping of the item piping's
            title, or several pipings and none named; the item parameter, or a
            ``--params`` entry, names a parameter its piping does not have.

    Example:
        target = pipelineTarget_resolve(plan, CubeClient(url, token))
    """
    pipeline: PipelineRef | None = client.pipeline_find(plan.pipeline)
    if pipeline is None:
        raise PlanRejected(f"no pipeline named {plan.pipeline!r}")
    target: Target = Target(pipeline=pipeline, pipings=client.pipings_list(pipeline.id))
    if plan.itemPiping:
        target.itemPiping = target.piping_find(plan.itemPiping)
        if target.itemPiping is None:
            titles: str = ", ".join(repr(piping.title) for piping in target.pipings)
            raise PlanRejected(
                f"pipeline {plan.pipeline!r} has no piping {plan.itemPiping!r}; it has {titles}"
            )
    elif len(target.pipings) == 1:
        target.itemPiping = target.pipings[0]
    else:
        raise PlanRejected(
            f"pipeline {plan.pipeline!r} has {len(target.pipings)} pipings; name one with --itemPiping"
        )

    paramNames: dict[int, set[str]] = {}
    for param in client.params_list(pipeline.id):
        paramNames.setdefault(param.pipingId, set()).add(param.name)
    if plan.itemParam not in paramNames.get(target.itemPiping.id, set()):
        raise PlanRejected(f"piping {target.itemPiping.title!r} has no parameter {plan.itemParam!r}")
    for title in dict.fromkeys(setting.piping for setting in plan.params):
        piping: PipingRef | None = target.piping_find(title)
        if piping is None:
            raise PlanRejected(f"--params names piping {title!r}, which pipeline {plan.pipeline!r} lacks")
        unknownParams_check(
            f"piping {title!r}",
            paramNames.get(piping.id, set()),
            [setting for setting in plan.params if setting.piping == title],
        )
    return target


def pluginTarget_resolve(plan: Plan, client: CubeApi) -> Target:
    """The plugin the plan names, as ``name`` or ``name@version``, checked against CUBE.

    Without a version the newest registered version is taken, and the plan's record
    names the one used.

    Raises:
        PlanRejected: no plugin of that exact name, or not that version; the item
            parameter, or a ``--params`` entry, names a parameter the plugin lacks.

    Example:
        target = pluginTarget_resolve(plan, CubeClient(url, token))
    """
    name, _, version = plan.plugin.partition(VERSION_SEPARATOR)
    plugins: list[PluginRef] = client.plugins_find(name)
    if version:
        plugins = [plugin for plugin in plugins if plugin.version == version]
    if not plugins:
        raise PlanRejected(f"no plugin named {name!r}" + (f" at version {version}" if version else ""))
    plugin: PluginRef = max(
        plugins, key=lambda found: [int(part) if part.isdigit() else -1 for part in found.version.split(".")]
    )
    names: set[str] = {param.name for param in client.pluginParams_list(plugin.id)}
    if plan.itemParam not in names:
        raise PlanRejected(f"plugin {plugin.name!r} has no parameter {plan.itemParam!r}")
    unknownParams_check(f"plugin {plugin.name!r}", names, plan.params)
    return Target(plugin=plugin)


def target_resolve(plan: Plan, client: CubeApi) -> Target:
    """What the plan names, a pipeline or a plugin, checked against CUBE.

    Raises:
        PlanRejected: it does not fit; see the two resolvers.

    Example:
        target = target_resolve(plan, CubeClient(url, token))
    """
    if plan.kind == "plugin":
        return pluginTarget_resolve(plan, client)
    return pipelineTarget_resolve(plan, client)


def nodesInfo_build(plan: Plan, target: Target, item: PlanItem) -> list[NodeInfo]:
    """The per-piping values for one item's workflow.

    The item piping gets the item's title and value; pipings named in ``--params``
    get those values; the rest take the pipeline's defaults.

    Example:
        >>> plan = Plan("p", "", "", "dirFilter", [], 4, [])
        >>> target = Target(PipelineRef(3, "p"), PipingRef(7, "x", None), [PipingRef(7, "x", None)])
        >>> node = nodesInfo_build(plan, target, PlanItem("a", "a", "re:^a$"))[0]
        >>> node.title, node.settings[0].value
        ('a', 're:^a$')
    """
    assert target.itemPiping is not None
    nodes: list[NodeInfo] = []
    for piping in target.pipings:
        settings: list[ParamSetting] = [setting for setting in plan.params if setting.piping == piping.title]
        if piping.id == target.itemPiping.id:
            settings = [setting for setting in settings if setting.name != plan.itemParam]
            settings.append(ParamSetting(piping=piping.title, name=plan.itemParam, value=item.value))
            nodes.append(NodeInfo(pipingId=piping.id, title=item.title, settings=settings))
        elif settings:
            nodes.append(NodeInfo(pipingId=piping.id, settings=settings))
    return nodes


def instanceSettings_build(plan: Plan, item: PlanItem) -> list[ParamSetting]:
    """The parameter values for one item's plugin instance: ``--params``, then the item's value.

    Example:
        >>> plan = Plan("", "pl-x", "", "dirFilter", [ParamSetting("", "verbosity", 2)], 4, [])
        >>> [setting.name for setting in instanceSettings_build(plan, PlanItem("a", "a", "re:^a$"))]
        ['verbosity', 'dirFilter']
    """
    settings: list[ParamSetting] = [setting for setting in plan.params if setting.name != plan.itemParam]
    settings.append(ParamSetting(piping="", name=plan.itemParam, value=item.value))
    return settings


def existing_find(item: PlanItem, previousId: int, target: Target, client: CubeApi) -> InstanceRef | None:
    """A child of the data node that an earlier run made for this item, or ``None``.

    A child counts when its title is the item's and it belongs to a workflow of this
    pipeline, or, for a plugin, runs this plugin on its own, outside any workflow; a
    child of the same title made from something else is someone else's.

    Example:
        found = existing_find(item, 4985, target, client)
    """
    for instance in client.children_find(previousId, item.title):
        if target.plugin is not None:
            if instance.pluginId == target.plugin.id and instance.workflowId is None:
                return instance
        elif (
            target.pipeline is not None
            and instance.workflowId is not None
            and client.workflow_get(instance.workflowId).pipelineId == target.pipeline.id
        ):
            return instance
    return None


def outcome_make(
    item: PlanItem,
    status: OutcomeStatus,
    client: CubeApi,
    instance: InstanceRef | None,
    workflowId: int | None,
) -> ItemOutcome:
    """The outcome for an item whose child, created or found, is a workflow or one instance.

    For a workflow the outcome lists every instance of the workflow.

    Example:
        outcome = outcome_make(item, "skipped", client, found, found.workflowId)
    """
    if workflowId is None:
        instanceIds: list[int] = [instance.id] if instance is not None else []
        return ItemOutcome(item=item.item, title=item.title, status=status, instanceIds=instanceIds)
    instances: list[InstanceRef] = client.workflowInstances_list(workflowId)
    return ItemOutcome(
        item=item.item,
        title=item.title,
        status=status,
        workflowId=workflowId,
        instanceIds=sorted(found.id for found in instances),
    )


def item_post(item: PlanItem, plan: Plan, previousId: int, target: Target, client: CubeApi) -> ItemOutcome:
    """One item: skipped when an earlier run made it, otherwise its child created.

    Args:
        item: the plan's entry.
        plan: the plan.
        previousId: the data node.
        target: the resolved pipeline or plugin.
        client: CUBE.

    Returns:
        What happened; a CUBE error is recorded, not raised.

    Example:
        outcome = item_post(plan.items[0], plan, 4985, target, client)
    """
    try:
        found: InstanceRef | None = existing_find(item, previousId, target, client)
        if found is not None:
            return outcome_make(item, "skipped", client, found, found.workflowId)
        if target.plugin is not None:
            created: InstanceRef = client.instance_post(
                target.plugin.id, previousId, item.title, instanceSettings_build(plan, item)
            )
            return outcome_make(item, "posted", client, created, None)
        assert target.pipeline is not None
        workflow: WorkflowRef = client.workflow_post(
            target.pipeline.id,
            previousId,
            f"{plan.pipeline}: {item.item}"[:100],
            nodesInfo_build(plan, target, item),
        )
        return outcome_make(item, "posted", client, None, workflow.id)
    except CubeError as err:
        return ItemOutcome(item=item.item, title=item.title, status="failed", error=str(err))


def plan_post(
    plan: Plan,
    client: CubeApi,
    report: Callable[[ItemOutcome], None] | None = None,
) -> RunRecord:
    """The whole plan: checked against CUBE, then one child per item not already made.

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
    record: RunRecord = RunRecord(cubeUrl=client.url, previousId=plan.previousId)
    if plan.previousId is None:
        record.error = "no data node: CHRIS_PREV_PLG_INST_ID is not set"
        return record
    try:
        target: Target = target_resolve(plan, client)
    except (PlanRejected, CubeError) as err:
        record.error = str(err)
        return record
    record.pipelineId = target.pipeline.id if target.pipeline is not None else None
    record.pipingId = target.itemPiping.id if target.itemPiping is not None else None
    record.pluginId = target.plugin.id if target.plugin is not None else None
    for item in plan.items:
        outcome: ItemOutcome = item_post(item, plan, plan.previousId, target, client)
        record.outcomes.append(outcome)
        if report is not None:
            report(outcome)
    return record
