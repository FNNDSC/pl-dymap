"""pl-dymap: a ChRIS DS plugin that maps a pipeline over the directories of its input.

For each item, by default each subdirectory of its input, it creates one workflow of a
named pipeline, attached to the same data node and given that directory alone, so
ChRIS runs the items in parallel, each in its own containers. It writes its plan before
it acts and its record after, and a rerun creates only what is missing.

Key components:
    - plan_build: the plan, from the input and the flags, without CUBE
    - plan_post: the plan acted on, one workflow per item, at most once
    - CubeClient: the CUBE calls a plan needs
    - the records, through dymap.models

Dependencies:
    - chris_plugin, pftree, pflog
"""

from __future__ import annotations

__version__: str = "1.0.0"

from dymap.cube import CubeApi, CubeClient
from dymap.models import CubeError, ItemOutcome, Plan, PlanItem, RunRecord, Span, Target
from dymap.plan import PlanRefused, plan_build
from dymap.post import PlanRejected, plan_post

__all__: list[str] = [
    "CubeApi",
    "CubeClient",
    "CubeError",
    "ItemOutcome",
    "Plan",
    "PlanItem",
    "PlanRefused",
    "PlanRejected",
    "RunRecord",
    "Span",
    "Target",
    "__version__",
    "plan_build",
    "plan_post",
]
