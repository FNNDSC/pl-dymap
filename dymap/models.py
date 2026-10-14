"""The records pl-dymap works with: the plan it writes, what CUBE tells it, and what it did.

Every record crosses a function boundary as a dataclass, and those written to the
output directory, the plan and the run record, carry ``toDict`` so that what is on
disk is exactly what the code held.

Key components:
    - PlanItem, Plan: what the plugin decided to create, before touching CUBE
    - PipelineRef, PipingRef, PipingParam, InstanceRef, WorkflowRef: what CUBE reports
    - Target: the pipeline and item piping a plan resolves to
    - ItemOutcome, RunRecord: what the plugin did with each item
    - CubeError: a refusal or failure from CUBE
    - Span: pflog's, re-exported

Dependencies:
    - pflog.models
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from pflog.models import Span

PLAN_SCHEMA: str = "dymap-plan/1"
RECORD_SCHEMA: str = "dymap-record/1"

OutcomeStatus = Literal["planned", "posted", "skipped", "failed"]


@dataclass
class PlanItem:
    """One child the plan calls for: an item of the input and the workflow it gets.

    Attributes:
        item: the item's path relative to the input, with ``/`` separators.
        title: the item's own name, the last component of its path: the title of the
            child workflow's root node, which is also how a rerun recognises a child
            already created.
        value: the value given to the item parameter of the item piping.

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
        pipeline: the name of the pipeline each item gets.
        itemPiping: the title of the piping that receives the item; empty means the
            pipeline's only piping.
        itemParam: the parameter of that piping set to the item.
        params: further parameter values, by piping title then parameter name.
        previousId: the data node the children attach to, from CUBE's environment;
            ``None`` when run outside CUBE.
        items: one entry per child.
        schema: the plan format's name and version.

    Example:
        >>> Plan(pipeline="p", itemPiping="", itemParam="dirFilter", params={}, previousId=None,
        ...      items=[]).toDict()["schema"]
        'dymap-plan/1'
    """

    pipeline: str
    itemPiping: str
    itemParam: str
    params: dict[str, dict[str, Any]]
    previousId: int | None
    items: list[PlanItem]
    schema: str = PLAN_SCHEMA

    def toDict(self) -> dict[str, Any]:
        """The plan as JSON-ready data.

        Example:
            >>> Plan("p", "", "dirFilter", {}, 7, [PlanItem("a", "a", "a")]).toDict()["items"][0]["item"]
            'a'
        """
        return asdict(self)


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
    value: Any


@dataclass
class InstanceRef:
    """A plugin instance as CUBE reports it.

    Attributes:
        id: the instance's id.
        title: its title.
        previousId: the instance above it.
        workflowId: the workflow it belongs to, ``None`` when it was run on its own.

    Example:
        >>> InstanceRef(id=9, title="sub-01", previousId=4, workflowId=2).workflowId
        2
    """

    id: int
    title: str
    previousId: int | None
    workflowId: int | None


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
class Target:
    """The pipeline and item piping a plan resolves to in CUBE.

    Attributes:
        pipeline: the pipeline.
        itemPiping: the piping that receives each item.
        pipingIds: every piping's id, by title, for ``--params``.

    Example:
        >>> Target(PipelineRef(3, "p"), PipingRef(7, "x", None), {"x": 7}).itemPiping.id
        7
    """

    pipeline: PipelineRef
    itemPiping: PipingRef
    pipingIds: dict[str, int]


@dataclass
class ItemOutcome:
    """What happened to one item of the plan.

    Attributes:
        item: the item's name.
        title: the root node's title.
        status: ``planned`` on a dry run, ``posted``, ``skipped`` when a child with
            this title from this pipeline already hangs off the data node, or
            ``failed``.
        workflowId: the child workflow, created or found.
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
class RunRecord:
    """What one run did, written to the output directory as ``created.json``.

    Attributes:
        cubeUrl: the CUBE the plugin spoke to.
        pipelineId: the pipeline's id there.
        pipingId: the item piping's id.
        previousId: the data node.
        outcomes: one per item, in plan order.
        error: why the run stopped before posting, if it did.
        schema: the record format's name and version.

    Example:
        >>> RunRecord(cubeUrl="", pipelineId=None, pipingId=None, previousId=None).status
        True
    """

    cubeUrl: str
    pipelineId: int | None
    pipingId: int | None
    previousId: int | None
    outcomes: list[ItemOutcome] = field(default_factory=list)
    error: str = ""
    schema: str = RECORD_SCHEMA

    @property
    def status(self) -> bool:
        """True when nothing failed and the run was not stopped.

        Example:
            >>> RunRecord("", 1, 1, 1, [ItemOutcome("a", "a", "failed")]).status
            False
        """
        return not self.error and all(outcome.status != "failed" for outcome in self.outcomes)

    def counts(self) -> dict[str, int]:
        """How many items ended in each status.

        Example:
            >>> RunRecord("", 1, 1, 1, [ItemOutcome("a", "a", "posted")]).counts()["posted"]
            1
        """
        tally: dict[str, int] = {"planned": 0, "posted": 0, "skipped": 0, "failed": 0}
        for outcome in self.outcomes:
            tally[outcome.status] += 1
        return tally

    def toDict(self) -> dict[str, Any]:
        """The record as JSON-ready data, with the counts.

        Example:
            >>> RunRecord("u", 1, 2, 3).toDict()["counts"]["failed"]
            0
        """
        data: dict[str, Any] = asdict(self)
        data["status"] = self.status
        data["counts"] = self.counts()
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
    "PLAN_SCHEMA",
    "RECORD_SCHEMA",
    "CubeError",
    "InstanceRef",
    "ItemOutcome",
    "OutcomeStatus",
    "PipelineRef",
    "PipingParam",
    "PipingRef",
    "Plan",
    "PlanItem",
    "RunRecord",
    "Span",
    "Target",
    "WorkflowRef",
]
