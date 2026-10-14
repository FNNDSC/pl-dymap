"""The records pl-dymap works with: its options, the plan it writes, what CUBE tells it, and what it did.

Every value that is not a primitive crosses a function boundary as a dataclass from
this module. Those written to the output directory, the plan and the run record, carry
``toDict``, so that what is on disk is exactly what the code held; CUBE's JSON enters
the client as a ``JsonRow`` and is turned into a record there, so that no other code
reads it.

Key components:
    - Options: the plugin's flags, from chris_plugin's namespace
    - JobEnv: what CUBE puts in the job's environment
    - ItemSelection: which directories of the input are items
    - ParamSetting: one parameter value for the children
    - PlanItem, Plan: what the plugin decided to create, before touching CUBE
    - JsonRow: one decoded JSON object from CUBE, with typed reads
    - PipelineRef, PipingRef, PipingParam, PluginRef, PluginParameter, InstanceRef,
      WorkflowRef: what CUBE reports
    - NodeInfo: one piping's values in a workflow request
    - Target: what a plan resolves to in CUBE
    - ItemOutcome, OutcomeCounts, RunRecord: what the plugin did with each item
    - CubeError: a refusal or failure from CUBE
    - Span: pflog's, re-exported

Dependencies:
    - pflog.models
"""

from __future__ import annotations

import argparse
import os
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from pflog.models import Span

PLAN_SCHEMA: str = "dymap-plan/2"
RECORD_SCHEMA: str = "dymap-record/2"
DEFAULT_ITEM_TEMPLATE: str = "re:^{itemRegex}$"
ENV_TOKEN: str = "CHRIS_USER_TOKEN"
ENV_PREVIOUS: str = "CHRIS_PREV_PLG_INST_ID"
ENV_URL: str = "CHRIS_URL"

OutcomeStatus = Literal["planned", "posted", "skipped", "failed"]
TargetKind = Literal["pipeline", "plugin"]
ParamValue = str | int | float | bool


@dataclass
class ItemSelection:
    """Which directories of the input are items.

    A directory is an item when it is at the chosen depth, its path passes the
    directory filter, and, when a file filter is set, it holds a file whose name
    passes that filter. The input itself is never an item, since no directory filter
    a child is given can select it.

    Attributes:
        dirFilter, dirFilterLogic: pftree directory filter on the path below the input.
        fileFilter, fileFilterLogic: pftree file filter on the names of the files a
            directory holds directly; empty for no condition on files.
        depth: how far below the input the items are; 0 means any depth when a file
            filter is set, and the input's own subdirectories when not.

    Example:
        >>> ItemSelection().depthEffective, ItemSelection(fileFilter="dcm").depthEffective
        (1, 0)
    """

    dirFilter: str = ""
    dirFilterLogic: str = "OR"
    fileFilter: str = ""
    fileFilterLogic: str = "OR"
    depth: int = 0

    @property
    def depthEffective(self) -> int:
        """The depth the walk uses: the chosen one, or the default for 0; 0 means any.

        Example:
            >>> ItemSelection(depth=2).depthEffective
            2
        """
        if self.depth:
            return self.depth
        return 0 if self.fileFilter else 1


@dataclass
class Options:
    """The plugin's flags.

    chris_plugin hands the body an ``argparse.Namespace``; the body turns it into
    this record at once, so that nothing below it reads attributes by name.

    Example:
        >>> Options(pipeline="p").itemParam
        'dirFilter'
    """

    pipeline: str = ""
    plugin: str = ""
    itemPiping: str = ""
    itemParam: str = "dirFilter"
    itemTemplate: str = DEFAULT_ITEM_TEMPLATE
    params: str = ""
    itemDirFilter: str = ""
    itemDirFilterLogic: str = "OR"
    itemFileFilter: str = ""
    itemFileFilterLogic: str = "OR"
    itemDepth: int = 0
    maxItems: int = 100
    dryRun: bool = False
    CUBEurl: str = ""
    verbosity: int = 1

    @classmethod
    def fromNamespace(cls, namespace: argparse.Namespace) -> Options:
        """The options from a parsed namespace; flags the record does not name are ignored.

        Example:
            >>> Options.fromNamespace(argparse.Namespace(plugin="pl-x", man=False)).plugin
            'pl-x'
        """
        names: set[str] = set(cls.__dataclass_fields__)
        return cls(**{name: value for name, value in vars(namespace).items() if name in names})

    @property
    def selection(self) -> ItemSelection:
        """Which directories are items, from the ``--item*Filter`` flags and ``--itemDepth``.

        Example:
            >>> Options(itemFileFilter="glob:*.mgz").selection.depthEffective
            0
        """
        return ItemSelection(
            dirFilter=self.itemDirFilter,
            dirFilterLogic=self.itemDirFilterLogic,
            fileFilter=self.itemFileFilter,
            fileFilterLogic=self.itemFileFilterLogic,
            depth=self.itemDepth,
        )


@dataclass
class JobEnv:
    """What CUBE puts in a plugin job's environment, as much as pl-dymap reads.

    The token is left out of ``repr`` and of every record.

    Attributes:
        token: ``CHRIS_USER_TOKEN``, the job owner's API token.
        previousId: ``CHRIS_PREV_PLG_INST_ID``, the data node; ``None`` outside CUBE.
        url: ``CHRIS_URL``, which CUBE does not set today, so usually empty.

    Example:
        >>> JobEnv(token="secret", previousId=4, url="")
        JobEnv(previousId=4, url='')
    """

    token: str = field(repr=False)
    previousId: int | None
    url: str

    @classmethod
    def fromEnviron(cls) -> JobEnv:
        """The job's environment, from ``os.environ``.

        Raises:
            ValueError: ``CHRIS_PREV_PLG_INST_ID`` is set but is not an integer.

        Example:
            env = JobEnv.fromEnviron()
        """
        previous: str = os.environ.get(ENV_PREVIOUS, "").strip()
        return cls(
            token=os.environ.get(ENV_TOKEN, ""),
            previousId=int(previous) if previous else None,
            url=os.environ.get(ENV_URL, ""),
        )


@dataclass
class ParamSetting:
    """One parameter value given to every child.

    Attributes:
        piping: the title of the piping it belongs to; empty when the children are
            plugin instances, which have no pipings.
        name: the parameter's name.
        value: its value.

    Example:
        >>> ParamSetting(piping="recon", name="verbosity", value=2).value
        2
    """

    piping: str
    name: str
    value: ParamValue


@dataclass
class PlanItem:
    """One child the plan calls for: an item of the input and the value it gets.

    Attributes:
        item: the item's path relative to the input, with ``/`` separators.
        title: the item's own name, the last component of its path: the title of the
            child plugin instance, or of the child workflow's root node, which is
            also how a rerun recognises a child already created.
        value: the value given to the item parameter.

    Example:
        >>> PlanItem(item="sub-01", title="sub-01", value="re:^sub\\\\-01$").title
        'sub-01'
    """

    item: str
    title: str
    value: str


@dataclass
class Plan:
    """What the plugin decided, from its input and its flags alone.

    The plan is computed without CUBE, written to the output directory before
    anything is created, and is the record of why the children exist.

    Attributes:
        pipeline: the name of the pipeline each item gets, or empty.
        plugin: the plugin each item gets, as ``name`` or ``name@version``, or empty;
            exactly one of the two is set.
        itemPiping: the title of the piping that receives the item; empty means the
            pipeline's only piping, and always empty for a plugin.
        itemParam: the parameter set to the item's value.
        params: further parameter values.
        previousId: the data node the children attach to, from CUBE's environment;
            ``None`` when run outside CUBE.
        items: one entry per child.
        schema: the plan format's name and version.

    Example:
        >>> Plan(pipeline="", plugin="pl-freesurfer", itemPiping="", itemParam="dirFilter", params=[],
        ...      previousId=None, items=[]).kind
        'plugin'
    """

    pipeline: str
    plugin: str
    itemPiping: str
    itemParam: str
    params: list[ParamSetting]
    previousId: int | None
    items: list[PlanItem]
    schema: str = PLAN_SCHEMA

    @property
    def kind(self) -> TargetKind:
        """Whether the children are workflows of a pipeline or instances of a plugin.

        Example:
            >>> Plan("p", "", "", "dirFilter", [], None, []).kind
            'pipeline'
        """
        return "plugin" if self.plugin else "pipeline"

    @property
    def targetName(self) -> str:
        """The pipeline's name or the plugin's, whichever the children are.

        Example:
            >>> Plan("", "pl-x@1.0.0", "", "dirFilter", [], None, []).targetName
            'pl-x@1.0.0'
        """
        return self.plugin or self.pipeline

    def toDict(self) -> dict[str, Any]:
        """The plan as JSON-ready data, with its kind.

        Example:
            >>> Plan("p", "", "", "dirFilter", [], 7, [PlanItem("a", "a", "a")]).toDict()["items"][0]["item"]
            'a'
        """
        data: dict[str, Any] = asdict(self)
        data["kind"] = self.kind
        return data


@dataclass
class JsonRow:
    """One JSON object, as CUBE sent it or as the client will send it, with typed reads.

    Example:
        >>> row = JsonRow({"id": "7", "previous_id": None, "title": "a"})
        >>> row.integer("id"), row.integerOrNone("previous_id"), row.text("title"), row.text("x")
        (7, None, 'a', '')
    """

    fields: dict[str, Any] = field(default_factory=dict)

    def integer(self, name: str) -> int:
        """A required integer field.

        Raises:
            KeyError: the field is missing.

        Example:
            >>> JsonRow({"id": 3}).integer("id")
            3
        """
        return int(self.fields[name])

    def integerOrNone(self, name: str) -> int | None:
        """An integer field CUBE may leave null or out.

        Example:
            >>> JsonRow({}).integerOrNone("workflow_id") is None
            True
        """
        value: Any = self.fields.get(name)
        return None if value is None else int(value)

    def text(self, name: str) -> str:
        """A string field, empty when missing or null.

        Example:
            >>> JsonRow({"name": None}).text("name")
            ''
        """
        value: Any = self.fields.get(name)
        return "" if value is None else str(value)

    def value(self, name: str) -> ParamValue | None:
        """A parameter value field, as CUBE gives it; ``None`` when not a primitive.

        Example:
            >>> JsonRow({"value": 2}).value("value")
            2
        """
        value: Any = self.fields.get(name)
        return value if isinstance(value, str | int | float | bool) else None

    def rows(self, name: str) -> list[JsonRow]:
        """A list-of-objects field, as rows.

        Example:
            >>> [row.integer("id") for row in JsonRow({"results": [{"id": 1}]}).rows("results")]
            [1]
        """
        return [JsonRow(item) for item in self.fields.get(name) or [] if isinstance(item, dict)]


@dataclass
class PipelineRef:
    """A pipeline as CUBE reports it.

    Example:
        >>> PipelineRef(id=3, name="FreeSurfer per subject").id
        3
    """

    id: int
    name: str


@dataclass
class PipingRef:
    """One piping of a pipeline: a plugin in the pipeline's tree.

    Attributes:
        id: the piping's id.
        title: its title, unique within the pipeline.
        previousId: the piping above it, ``None`` for the root.

    Example:
        >>> PipingRef(id=5, title="recon", previousId=None).previousId is None
        True
    """

    id: int
    title: str
    previousId: int | None


@dataclass
class PipingParam:
    """A parameter of one piping, with the pipeline's default for it.

    Example:
        >>> PipingParam(pipingId=5, name="dirFilter", value="").name
        'dirFilter'
    """

    pipingId: int
    name: str
    value: ParamValue | None


@dataclass
class PluginRef:
    """A plugin as CUBE reports it.

    Example:
        >>> PluginRef(id=9, name="pl-freesurfer", version="1.0.0").version
        '1.0.0'
    """

    id: int
    name: str
    version: str


@dataclass
class PluginParameter:
    """One parameter of a plugin.

    Example:
        >>> PluginParameter(name="dirFilter", type="string").type
        'string'
    """

    name: str
    type: str


@dataclass
class InstanceRef:
    """A plugin instance as CUBE reports it.

    Attributes:
        id: the instance's id.
        title: its title.
        previousId: the instance above it.
        workflowId: the workflow it belongs to, ``None`` when it was run on its own.
        pluginId: the plugin it runs.

    Example:
        >>> InstanceRef(id=9, title="sub-01", previousId=4, workflowId=2, pluginId=6).workflowId
        2
    """

    id: int
    title: str
    previousId: int | None
    workflowId: int | None
    pluginId: int | None = None


@dataclass
class WorkflowRef:
    """A workflow, one run of a pipeline, as CUBE reports it.

    Example:
        >>> WorkflowRef(id=2, pipelineId=3, title="sub-01").pipelineId
        3
    """

    id: int
    pipelineId: int
    title: str


@dataclass
class NodeInfo:
    """One piping's title and values in a request to create a workflow.

    Attributes:
        pipingId: the piping.
        title: the title its plugin instance gets; empty for the pipeline's own.
        settings: its parameter values; parameters left out take the pipeline's defaults.

    Example:
        >>> NodeInfo(7, "sub-01", [ParamSetting("x", "dirFilter", "re:^a$")]).toDict()["title"]
        'sub-01'
    """

    pipingId: int
    title: str = ""
    settings: list[ParamSetting] = field(default_factory=list)

    def toDict(self) -> dict[str, Any]:
        """The entry as CUBE's ``nodes_info`` takes it.

        Example:
            >>> NodeInfo(7).toDict()
            {'piping_id': 7, 'plugin_parameter_defaults': []}
        """
        data: dict[str, Any] = {
            "piping_id": self.pipingId,
            "plugin_parameter_defaults": [
                {"name": setting.name, "default": setting.value} for setting in self.settings
            ],
        }
        if self.title:
            data["title"] = self.title
        return data


@dataclass
class Target:
    """What a plan resolves to in CUBE: a pipeline and its item piping, or a plugin.

    Attributes:
        pipeline: the pipeline, for a pipeline plan.
        itemPiping: the piping that receives each item, for a pipeline plan.
        pipings: every piping of the pipeline, for ``--params``.
        plugin: the plugin, for a plugin plan.

    Example:
        >>> Target(plugin=PluginRef(9, "pl-x", "1.0.0")).plugin.id
        9
    """

    pipeline: PipelineRef | None = None
    itemPiping: PipingRef | None = None
    pipings: list[PipingRef] = field(default_factory=list)
    plugin: PluginRef | None = None

    def piping_find(self, title: str) -> PipingRef | None:
        """The piping with this title, or ``None``.

        Example:
            >>> Target(pipings=[PipingRef(7, "recon", None)]).piping_find("recon").id
            7
        """
        return next((piping for piping in self.pipings if piping.title == title), None)


@dataclass
class ItemOutcome:
    """What happened to one item of the plan.

    Attributes:
        item: the item's path.
        title: the child's title.
        status: ``planned`` on a dry run, ``posted``, ``skipped`` when an earlier run
            made this child, or ``failed``.
        workflowId: the child workflow, created or found; ``None`` for a plugin plan.
        instanceIds: its plugin instances, created or found.
        error: why it failed.

    Example:
        >>> ItemOutcome(item="a", title="a", status="skipped").status
        'skipped'
    """

    item: str
    title: str
    status: OutcomeStatus
    workflowId: int | None = None
    instanceIds: list[int] = field(default_factory=list)
    error: str = ""


@dataclass
class OutcomeCounts:
    """How many items ended in each status.

    Example:
        >>> OutcomeCounts(posted=2).posted
        2
    """

    planned: int = 0
    posted: int = 0
    skipped: int = 0
    failed: int = 0


@dataclass
class RunRecord:
    """What one run did, written to the output directory as ``created.json``.

    Attributes:
        cubeUrl: the CUBE the plugin spoke to.
        previousId: the data node.
        pipelineId: the pipeline's id there, for a pipeline plan.
        pipingId: the item piping's id, for a pipeline plan.
        pluginId: the plugin's id there, for a plugin plan.
        outcomes: one per item, in plan order.
        error: why the run stopped before posting, if it did.
        schema: the record format's name and version.

    Example:
        >>> RunRecord(cubeUrl="", previousId=None).status
        True
    """

    cubeUrl: str
    previousId: int | None
    pipelineId: int | None = None
    pipingId: int | None = None
    pluginId: int | None = None
    outcomes: list[ItemOutcome] = field(default_factory=list)
    error: str = ""
    schema: str = RECORD_SCHEMA

    @property
    def status(self) -> bool:
        """True when nothing failed and the run was not stopped.

        Example:
            >>> RunRecord("", 1, outcomes=[ItemOutcome("a", "a", "failed")]).status
            False
        """
        return not self.error and all(outcome.status != "failed" for outcome in self.outcomes)

    def counts(self) -> OutcomeCounts:
        """How many items ended in each status.

        Example:
            >>> RunRecord("", 1, outcomes=[ItemOutcome("a", "a", "posted")]).counts().posted
            1
        """
        tally: OutcomeCounts = OutcomeCounts()
        for outcome in self.outcomes:
            setattr(tally, outcome.status, getattr(tally, outcome.status) + 1)
        return tally

    def toDict(self) -> dict[str, Any]:
        """The record as JSON-ready data, with the status and the counts.

        Example:
            >>> RunRecord("u", 3).toDict()["counts"]["failed"]
            0
        """
        data: dict[str, Any] = asdict(self)
        data["status"] = self.status
        data["counts"] = asdict(self.counts())
        return data


class CubeError(Exception):
    """CUBE refused a request or could not be reached.

    Attributes:
        status: the HTTP status, 0 when no response came back.
        message: what CUBE said, or why there was no response.

    Example:
        >>> str(CubeError(404, "Not found."))
        'CUBE 404: Not found.'
    """

    def __init__(self, status: int, message: str) -> None:
        self.status: int = status
        self.message: str = message
        super().__init__(f"CUBE {status}: {message}")


__all__: list[str] = [
    "DEFAULT_ITEM_TEMPLATE",
    "ENV_PREVIOUS",
    "ENV_TOKEN",
    "ENV_URL",
    "PLAN_SCHEMA",
    "RECORD_SCHEMA",
    "CubeError",
    "InstanceRef",
    "ItemOutcome",
    "ItemSelection",
    "JobEnv",
    "JsonRow",
    "NodeInfo",
    "Options",
    "OutcomeCounts",
    "OutcomeStatus",
    "ParamSetting",
    "ParamValue",
    "PipelineRef",
    "PipingParam",
    "PipingRef",
    "Plan",
    "PlanItem",
    "PluginParameter",
    "PluginRef",
    "RunRecord",
    "Span",
    "Target",
    "TargetKind",
    "WorkflowRef",
]
