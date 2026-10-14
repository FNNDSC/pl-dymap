"""Deciding what to create: the plan, from the input directory and the flags, without CUBE.

The items are directories of the input chosen by three conditions, each optional: a
depth below the input, a directory filter on the path, and a file filter on the names
of the files a directory holds. Each item gets one child, a workflow of a pipeline or
an instance of a plugin, whose item parameter receives a value built from the item's
path by a template, by default a regular-expression filter that matches that directory
and no other. The plan is a pure function of its arguments, so it is tested on the
metal and written to the output directory before anything is created.

Key components:
    - PlanRefused: why a plan cannot be made
    - items_find: the item paths in an input directory
    - itemValue_render: one item's value from the template
    - params_parse: the ``--params`` JSON, as settings
    - plan_build: the plan

Dependencies:
    - pftree.filters, dymap.models
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from pftree.filters import FilterLogic, FilterToken, dirPath_matches, fileNames_filter, tokens_parse

from dymap.models import ItemSelection, JsonRow, Options, ParamSetting, ParamValue, Plan, PlanItem, TargetKind

ITEM_PLACEHOLDER: str = "{item}"
ITEM_REGEX_PLACEHOLDER: str = "{itemRegex}"
FILTER_SEPARATOR: str = ","


class PlanRefused(Exception):
    """The input cannot be planned as asked: no items, too many, or an item name the
    filter grammar cannot carry.

    Example:
        >>> str(PlanRefused("too many"))
        'too many'
    """


def items_find(inputDir: Path, selection: ItemSelection) -> list[str]:
    """The items: the directories below the input that the selection chooses, as
    relative paths with ``/`` separators, sorted.

    The walk follows symbolic links, as pl-dircopy's data nodes need, and visits each
    real directory once. Hidden directories, whose names start with a period, are
    neither items nor walked into. The input itself is never an item.

    Args:
        inputDir: the plugin's input directory.
        selection: the depth, directory filter and file filter.

    Returns:
        The relative paths, sorted.

    Example:
        >>> import tempfile; root = Path(tempfile.mkdtemp())
        >>> for name in ("link/sub-02/anat", "link/sub-01", ".cache/x"): (root / name).mkdir(parents=True)
        >>> (root / "link/sub-01/t1.mgz").write_text(""); (root / "link/sub-02/anat/t1.mgz").write_text("")
        0
        0
        >>> items_find(root, ItemSelection()), items_find(root, ItemSelection(depth=2))
        (['link'], ['link/sub-01', 'link/sub-02'])
        >>> items_find(root, ItemSelection(fileFilter="glob:*.mgz"))
        ['link/sub-01', 'link/sub-02/anat']
    """
    dirTokens: list[FilterToken] = tokens_parse(selection.dirFilter) if selection.dirFilter else []
    fileTokens: list[FilterToken] = tokens_parse(selection.fileFilter) if selection.fileFilter else []
    dirLogic: FilterLogic = FilterLogic.parse(selection.dirFilterLogic)
    fileLogic: FilterLogic = FilterLogic.parse(selection.fileFilterLogic)
    depth: int = selection.depthEffective
    seen: set[str] = set()
    found: list[str] = []
    for root, dirNames, fileNames in os.walk(inputDir, followlinks=True):
        real: str = os.path.realpath(root)
        if real in seen:
            dirNames.clear()
            continue
        seen.add(real)
        relPath: str = Path(root).relative_to(inputDir).as_posix()
        level: int = 0 if relPath == "." else relPath.count("/") + 1
        dirNames[:] = sorted(name for name in dirNames if not name.startswith("."))
        if depth and level >= depth:
            dirNames.clear()
        if level == 0 or (depth and level != depth):
            continue
        if dirTokens and not dirPath_matches(relPath, dirTokens, dirLogic):
            continue
        if fileTokens and not fileNames_filter(sorted(fileNames), fileTokens, fileLogic):
            continue
        found.append(relPath)
    return sorted(found)


def itemValue_render(template: str, item: str) -> str:
    """One item's value: the template with ``{item}`` and ``{itemRegex}`` replaced by its path.

    ``{itemRegex}`` is the path escaped for a regular expression, so the default
    template, ``re:^{itemRegex}$``, matches the item's directory exactly; a plain
    substring filter of ``sub-01`` would also match ``sub-010``.

    Args:
        template: the value template.
        item: the item's path.

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


def settings_read(piping: str, values: JsonRow, where: str) -> list[ParamSetting]:
    """The settings in one JSON object of parameter values.

    Raises:
        ValueError: a value that is not a string, number or boolean.

    Example:
        >>> settings_read("recon", JsonRow({"verbosity": 2}), "--params")[0].name
        'verbosity'
    """
    settings: list[ParamSetting] = []
    for name in values.fields:
        value: ParamValue | None = values.value(name)
        if value is None:
            raise ValueError(f"{where}: parameter {name!r} must be a string, number or boolean")
        settings.append(ParamSetting(piping=piping, name=name, value=value))
    return settings


def params_parse(text: str, kind: TargetKind) -> list[ParamSetting]:
    """The ``--params`` flag: further parameter values for every child.

    For a pipeline the JSON names pipings by title, then parameters; for a plugin it
    names the plugin's parameters directly.

    Args:
        text: JSON, as ``{"recon": {"verbosity": 2}}`` for a pipeline or
            ``{"verbosity": 2}`` for a plugin; empty for none.
        kind: whether the children are workflows or plugin instances.

    Returns:
        The settings, in the order given.

    Raises:
        ValueError: the text is not JSON of that shape.

    Example:
        >>> params_parse('{"recon": {"verbosity": 2}}', "pipeline")[0].piping
        'recon'
        >>> params_parse('{"verbosity": 2}', "plugin")[0].piping
        ''
    """
    if not text.strip():
        return []
    parsed: Any = json.loads(text)
    if not isinstance(parsed, dict):
        raise ValueError("--params must be a JSON object")
    if kind == "plugin":
        return settings_read("", JsonRow(parsed), "--params")
    settings: list[ParamSetting] = []
    for piping, values in parsed.items():
        if not isinstance(values, dict):
            raise ValueError(f"--params for piping {piping!r} must be a JSON object of parameter values")
        settings.extend(settings_read(piping, JsonRow(values), f"--params for piping {piping!r}"))
    return settings


def plan_build(
    inputDir: Path, options: Options, params: list[ParamSetting], previousId: int | None = None
) -> Plan:
    """The plan: one child per item of the input.

    Args:
        inputDir: the plugin's input directory.
        options: the flags: what each item gets, how items are chosen, the limit.
        params: further parameter values, from ``params_parse``.
        previousId: the data node the children attach to.

    Returns:
        The plan.

    Raises:
        PlanRefused: no items; more items than ``--maxItems``; two items with the
            same name, which would give two children one title; an item whose value
            would contain the filter grammar's comma, which would split it into two
            tokens; or a template that names no item.

    Example:
        >>> import tempfile; root = Path(tempfile.mkdtemp()); (root / "sub-01").mkdir()
        >>> plan_build(root, Options(pipeline="FreeSurfer per subject"), []).items[0].value
        're:^sub\\\\-01$'
    """
    paths: list[str] = items_find(inputDir, options.selection)
    if not paths:
        raise PlanRefused("no items: no directory below the input passes the item selection")
    if len(paths) > options.maxItems:
        raise PlanRefused(f"{len(paths)} items exceed --maxItems {options.maxItems}")
    titles: dict[str, str] = {}
    items: list[PlanItem] = []
    for path in paths:
        title: str = path.rsplit("/", 1)[-1]
        if title in titles:
            raise PlanRefused(f"items {titles[title]!r} and {path!r} share the name {title!r}")
        titles[title] = path
        value: str = itemValue_render(options.itemTemplate, path)
        if options.itemParam.endswith("Filter") and FILTER_SEPARATOR in value:
            raise PlanRefused(f"item {path!r} would give the filter {value!r}, which a comma splits in two")
        items.append(PlanItem(item=path, title=title, value=value))
    return Plan(
        pipeline=options.pipeline,
        plugin=options.plugin,
        itemPiping=options.itemPiping,
        itemParam=options.itemParam,
        params=params,
        previousId=previousId,
        items=items,
    )
