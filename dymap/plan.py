"""Deciding what to create: the plan, from the input directory and the flags, without CUBE.

Each directory at the item depth below the input, by default its own subdirectories,
that passes the directory filter is an item.
Each item gets one child workflow, whose item piping receives a value built from the
item's name by a template, by default a regular-expression filter that matches that
directory and no other. The plan is a pure function of its arguments, so it is tested
on the metal and written to the output directory before anything is created.

Key components:
    - ItemTemplateError, PlanRefused: why a plan cannot be made
    - items_find: the item names in an input directory
    - itemValue_render: one item's value from the template
    - params_parse: the ``--params`` JSON
    - plan_build: the plan

Dependencies:
    - pftree.filters, dymap.models
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from pftree.filters import FilterLogic, FilterToken, dirPath_matches, tokens_parse

from dymap.models import Plan, PlanItem

ITEM_PLACEHOLDER: str = "{item}"
ITEM_REGEX_PLACEHOLDER: str = "{itemRegex}"
DEFAULT_ITEM_TEMPLATE: str = "re:^{itemRegex}$"
FILTER_SEPARATOR: str = ","


class PlanRefused(Exception):
    """The input cannot be planned as asked: too many items, or an item name the filter
    grammar cannot carry.

    Example:
        >>> str(PlanRefused("too many"))
        'too many'
    """


def items_find(inputDir: Path, dirFilter: str = "", dirFilterLogic: str = "OR", depth: int = 1) -> list[str]:
    """The items: the directories at ``depth`` below the input that pass the filter, as
    relative paths with ``/`` separators, sorted.

    Depth 1 is the input's own subdirectories. A data node often holds its subjects one
    level down, under a directory named for where the data came from, as pl-dircopy's
    link does; depth 2 reaches them without knowing that name. Hidden directories, whose
    names start with a period, are neither items nor walked into. The filter tests the
    item's relative path, as pftree's directory filters do.

    Args:
        inputDir: the plugin's input directory.
        dirFilter: a pftree directory filter, empty for every directory.
        dirFilterLogic: ``OR`` or ``AND``, how the filter's tokens combine.
        depth: how far below the input the items are, 1 or more.

    Returns:
        The relative paths, sorted.

    Example:
        >>> import tempfile; root = Path(tempfile.mkdtemp())
        >>> for name in ("link/sub-02", "link/sub-01", ".cache/x"): (root / name).mkdir(parents=True)
        >>> items_find(root), items_find(root, depth=2)
        (['link'], ['link/sub-01', 'link/sub-02'])
    """
    tokens: list[FilterToken] = tokens_parse(dirFilter) if dirFilter else []
    logic: FilterLogic = FilterLogic.parse(dirFilterLogic)
    level: list[str] = [""]
    for _ in range(depth):
        level = [
            f"{parent}/{entry.name}" if parent else entry.name
            for parent in level
            for entry in sorted((inputDir / parent).iterdir())
            if entry.is_dir() and not entry.name.startswith(".")
        ]
    found: list[str] = sorted(level)
    if not tokens:
        return found
    return [path for path in found if dirPath_matches(path, tokens, logic)]


def itemValue_render(template: str, item: str) -> str:
    """One item's value: the template with ``{item}`` and ``{itemRegex}`` replaced by its path.

    ``{itemRegex}`` is the name escaped for a regular expression, so the default
    template, ``re:^{itemRegex}$``, matches the item's directory exactly; a plain
    substring filter of ``sub-01`` would also match ``sub-010``.

    Args:
        template: the value template.
        item: the item's name.

    Returns:
        The value.

    Raises:
        PlanRefused: the template names no item, so every child would get the same
            value and process the same data.

    Example:
        >>> itemValue_render("re:^{itemRegex}$", "sub.01")
        're:^sub\\\\.01$'
    """
    if ITEM_PLACEHOLDER not in template and ITEM_REGEX_PLACEHOLDER not in template:
        raise PlanRefused(f"the item template {template!r} names neither {{item}} nor {{itemRegex}}")
    return template.replace(ITEM_REGEX_PLACEHOLDER, re.escape(item)).replace(ITEM_PLACEHOLDER, item)


def params_parse(text: str) -> dict[str, dict[str, Any]]:
    """The ``--params`` flag: further parameter values by piping title, then parameter name.

    Args:
        text: JSON, as ``{"recon": {"exec": "recon-all -all ..."}}``; empty for none.

    Returns:
        The mapping.

    Raises:
        ValueError: the text is not a JSON object of objects.

    Example:
        >>> params_parse('{"recon": {"verbosity": 2}}')["recon"]["verbosity"]
        2
    """
    if not text.strip():
        return {}
    parsed: Any = json.loads(text)
    if not isinstance(parsed, dict) or not all(isinstance(value, dict) for value in parsed.values()):
        raise ValueError("--params must be a JSON object mapping piping titles to objects of parameters")
    return parsed


def plan_build(
    inputDir: Path,
    pipeline: str,
    *,
    itemPiping: str = "",
    itemParam: str = "dirFilter",
    itemTemplate: str = DEFAULT_ITEM_TEMPLATE,
    params: dict[str, dict[str, Any]] | None = None,
    dirFilter: str = "",
    dirFilterLogic: str = "OR",
    itemDepth: int = 1,
    maxItems: int = 100,
    previousId: int | None = None,
) -> Plan:
    """The plan: one child workflow per item of the input.

    Args:
        inputDir: the plugin's input directory.
        pipeline: the pipeline each item gets.
        itemPiping: the piping that receives the item; empty for the pipeline's only one.
        itemParam: the parameter of that piping set to the item's value.
        itemTemplate: how the value is built from the item's name.
        params: further parameter values, by piping title then name.
        dirFilter, dirFilterLogic: which directories are items.
        itemDepth: how far below the input the items are.
        maxItems: the most items a plan may hold.
        previousId: the data node the children attach to.

    Returns:
        The plan.

    Raises:
        PlanRefused: no items; more items than ``maxItems``; two items with the same
            name, which would give two children one title; an item whose value
            would contain the filter grammar's comma, which would split it into two
            tokens; or a template that names no item.

    Example:
        >>> import tempfile; root = Path(tempfile.mkdtemp()); (root / "sub-01").mkdir()
        >>> plan_build(root, "FreeSurfer per subject").items[0].value
        're:^sub\\\\-01$'
    """
    paths: list[str] = items_find(inputDir, dirFilter, dirFilterLogic, itemDepth)
    if not paths:
        raise PlanRefused(f"no items: no directories at depth {itemDepth} pass the filter")
    if len(paths) > maxItems:
        raise PlanRefused(f"{len(paths)} items exceed --maxItems {maxItems}")
    titles: dict[str, str] = {}
    items: list[PlanItem] = []
    for path in paths:
        title: str = path.rsplit("/", 1)[-1]
        if title in titles:
            raise PlanRefused(f"items {titles[title]!r} and {path!r} share the name {title!r}")
        titles[title] = path
        value: str = itemValue_render(itemTemplate, path)
        if itemParam.endswith("Filter") and FILTER_SEPARATOR in value:
            raise PlanRefused(f"item {path!r} would give the filter {value!r}, which a comma splits in two")
        items.append(PlanItem(item=path, title=title, value=value))
    return Plan(
        pipeline=pipeline,
        itemPiping=itemPiping,
        itemParam=itemParam,
        params=params or {},
        previousId=previousId,
        items=items,
    )
