"""The ``dymap`` command: pl-dymap as a ChRIS DS plugin.

``main`` is the run wrapped twice: pflog's ``timed`` inside, so the run leaves one line
of JSON on standard error, and ``chris_plugin`` outside, so the registry sees it and
ChRIS supplies the two directories.

Key components:
    - TITLE, CATEGORY, MIN_MEMORY, MIN_CPU: the plugin's description for ChRIS
    - parser_build: the flags
    - MANUAL: the manual page
    - parser, main: for chris_plugin, the console script and the tests

Typical usage:
    dymap --pipeline "FreeSurfer 8.2.0 per subject" --CUBEurl http://cube:8000/api/v1/ /incoming /outgoing
    dymap --man /tmp /tmp

Dependencies:
    - chris_plugin, pflog, pftree.manual, dymap.dymap
"""

# No ``from __future__ import annotations`` here: chris_plugin reads main's annotations
# as objects to tell a DS plugin from an FS plugin.

import argparse
from pathlib import Path

from chris_plugin import chris_plugin
from pflog import timed
from pftree.manual import Manual, manual_print

from dymap import __version__
from dymap.dymap import dymap_run
from dymap.plan import DEFAULT_ITEM_TEMPLATE

TITLE: str = "Map a pipeline over every directory of the input: one workflow per item"
CATEGORY: str = "Utility"
MIN_MEMORY: str = "100Mi"
MIN_CPU: str = "500m"

BANNER_ART: str = r"""
       _           _
      | |         | |
 _ __ | |______ __| |_   _ _ __ ___   __ _ _ __
| '_ \| |______/ _` | | | | '_ ` _ \ / _` | '_ \
| |_) | |     | (_| | |_| | | | | | | (_| | |_) |
| .__/|_|      \__,_|\__, |_| |_| |_|\__,_| .__/
| |                   __/ |               | |
|_|                  |___/                |_|
"""

BANNER_TEXT: str = """
                    Map a pipeline over an input tree

                         -- version {version} --

        A ChRIS DS plugin that creates one workflow of a named pipeline for
        each directory of its input, each attached to the same
        data node and given its own directory, so that ChRIS runs the items
        in parallel, each in its own containers and with its own resources.
"""

SYNOPSIS_SHORT: str = """
    NAME

        dymap

    SYNOPSIS

        dymap                                                                   \\
            --pipeline <name>                                                   \\
            [--itemPiping <title>]                                              \\
            [--itemParam <parameter>]                                           \\
            [--itemTemplate <template>]                                         \\
            [--params <json>]                                                   \\
            [--dirFilter <filter>]                                              \\
            [--dirFilterLogic <AND|OR>]                                         \\
            [--itemDepth <n>]                                                   \\
            [--maxItems <n>]                                                    \\
            [--dryRun]                                                          \\
            [--CUBEurl <url>]                                                   \\
            [--verbosity <level>]                                               \\
            [--man] [--synopsis] [--version]                                    \\
            <inputDir> <outputDir>

    BRIEF EXAMPLE

        dymap --pipeline "FreeSurfer 8.2.0 per subject"                         \\
              --CUBEurl http://cube:8000/api/v1/ /incoming /outgoing
"""

SYNOPSIS_FULL: str = (
    SYNOPSIS_SHORT
    + f"""
    DESCRIPTION

        ``dymap`` runs a pipeline once per item of its input. Each directory
        at ``--itemDepth`` below the input, by default its own subdirectories,
        is an item; ``--dirFilter`` narrows which. For
        each item the plugin creates one workflow of the pipeline, attached to
        the same data node as the plugin itself, with the item's value given
        to one parameter of one piping, by default ``--dirFilter`` set to a
        filter that matches that directory's path alone. ChRIS then schedules each
        workflow on its own, so the items run in parallel, each in its own
        containers and with the resources its pipeline asks for, and each
        succeeds or fails alone.

        The plugin decides before it acts. It writes ``plan.json``, the items
        and the value each gets, before it creates anything, and
        ``created.json`` after, with each item's workflow and plugin instances
        or the reason it failed. Both stay in the feed as the record of why
        the workflows exist. A rerun does not duplicate: an item whose
        workflow from this pipeline already hangs off the data node, with the
        item's name as its title, is skipped.

        The plan is checked against CUBE before anything is created: the
        pipeline must exist by exact name, and the item parameter and every
        ``--params`` entry must be parameters of the pipings they name, since
        CUBE ignores an unknown one and a misspelt item parameter would give
        every workflow the whole input.

        The plugin does not wait for the workflows. It speaks to CUBE with the
        token CUBE gives every job, CHRIS_USER_TOKEN, which is never a flag and
        never written down; the data node is CHRIS_PREV_PLG_INST_ID. CUBE does
        not yet tell a job its own address, so ``--CUBEurl`` names it, or the
        CHRIS_URL environment variable.

        The exit status is 0 when every item was created or skipped, 1 when
        the plan was refused or rejected, CUBE could not be reached, or any
        item failed, and 2 on a usage error. A rerun after a failure creates
        only what is missing.

    ARGS

        <inputDir>
        The data node's output, as ChRIS mounts it. Its directories at
        ``--itemDepth`` are the items.

        <outputDir>
        Where ``plan.json`` and ``created.json`` are written.

        --pipeline <name>
        The exact name of the pipeline each item gets. Required.

        [--itemPiping <title>]
        The title of the piping that receives the item. May be omitted when
        the pipeline has one piping.

        [--itemParam <parameter>]
        The parameter of that piping set to the item's value. Default
        ``dirFilter``.

        [--itemTemplate <template>]
        How the value is built from the item: ``{{item}}`` is its path below
        the input and ``{{itemRegex}}`` that path escaped for a regular
        expression.
        Default ``{DEFAULT_ITEM_TEMPLATE}``, a filter matching exactly that
        directory; a plain ``sub-01`` would also match ``sub-010``.

        [--params <json>]
        Further parameter values for every workflow, by piping title and then
        parameter name, as ``{{"recon": {{"verbosity": 2}}}}``.

        [--dirFilter <filter>] [--dirFilterLogic <AND|OR>]
        Which directories are items, tested against their path below the
        input, in pftree's filter grammar:
        comma-separated tokens, each a substring, ``glob:``, ``re:`` or
        ``!`` negation. Default every directory not starting with a period.

        [--itemDepth <n>]
        How far below the input the items are. Default 1, the input's own
        subdirectories. A data node made by pl-dircopy holds its data under a
        directory named for where it came from, as
        ``home_chris_uploads_subjects``; depth 2 reaches the subjects inside
        it without naming it. Items are titled by their own name, which must
        be unique among them.

        [--maxItems <n>]
        The most items one run may create. Default 100. A larger input is
        refused, so that a mistaken input cannot flood the cluster.

        [--dryRun]
        Write ``plan.json`` and a ``created.json`` of planned items, and
        create nothing. Needs no CUBE.

        [--CUBEurl <url>]
        The CUBE API root, ending in ``/api/v1/``, as reachable from the
        container: inside a cluster that may be a service name, not the
        address a browser uses. Defaults to CHRIS_URL.

        [--verbosity <level>]
        0 prints nothing to the console; 1, the default, prints one line per
        item and a summary.

        [--man] [--synopsis] [--version]
        This page; its first part; the version.

    EXAMPLES

    One FreeSurfer workflow per subject directory:

        dymap --pipeline "FreeSurfer 8.2.0 per subject"                         \\
              --CUBEurl http://cube:8000/api/v1/ /incoming /outgoing

    The same, under a pl-dircopy node, whose subjects sit one level down:

        dymap --pipeline "FreeSurfer 8.2.0 per subject" --itemDepth 2           \\
              --CUBEurl http://cube:8000/api/v1/ /incoming /outgoing

    The subjects whose names start with sub-0, after looking at the plan:

        dymap --pipeline "FreeSurfer 8.2.0 per subject" --dryRun                \\
              --dirFilter 're:(^|/)sub-0[^/]*$' /incoming /outgoing

    A two-piping pipeline, the item to the first piping, an option to the
    second:

        dymap --pipeline "Convert then count" --itemPiping convert              \\
              --params '{{"count": {{"exec": "wc -l %{{inputWorkingDir}}"}}}}'  \\
              --CUBEurl http://cube:8000/api/v1/ /incoming /outgoing
"""
)

MANUAL: Manual = Manual(
    command="dymap",
    art=BANNER_ART,
    title="Map a pipeline over an input tree",
    text=BANNER_TEXT,
    version=__version__,
    synopsisShort=SYNOPSIS_SHORT,
    synopsisFull=SYNOPSIS_FULL,
)


def parser_build() -> argparse.ArgumentParser:
    """The plugin's flags, without the positional directories ChRIS adds.

    Every flag is a plain ``store`` or ``store_true``, which chris_plugin serialises;
    none is marked required, since ChRIS would then require it in every form, so a
    missing ``--pipeline`` is caught in the run and exits 2.

    Example:
        >>> parser_build().parse_args(["--pipeline", "p"]).itemParam
        'dirFilter'
    """
    parser: argparse.ArgumentParser = argparse.ArgumentParser(
        prog="dymap",
        description="Create one workflow of a pipeline per directory of the input. Full manual: dymap --man",
    )
    parser.add_argument("--pipeline", default="", help="exact name of the pipeline each item gets (required)")
    parser.add_argument("--itemPiping", default="", help="title of the piping that receives the item")
    parser.add_argument("--itemParam", default="dirFilter", help="parameter of that piping set to the item")
    parser.add_argument(
        "--itemTemplate",
        default=DEFAULT_ITEM_TEMPLATE,
        help="the item's value: {item} is its name, {itemRegex} the name escaped for a regex",
    )
    parser.add_argument("--params", default="", help="JSON of further values: {piping title: {param: value}}")
    parser.add_argument(
        "--dirFilter", default="", help="which directories are items, by path, pftree grammar"
    )
    parser.add_argument("--dirFilterLogic", default="OR", help="AND or OR, how --dirFilter tokens combine")
    parser.add_argument("--itemDepth", type=int, default=1, help="how far below the input the items are")
    parser.add_argument("--maxItems", type=int, default=100, help="refuse an input with more items than this")
    parser.add_argument("--dryRun", action="store_true", default=False, help="write the plan, create nothing")
    parser.add_argument("--CUBEurl", default="", help="CUBE API root as reachable from the job; or CHRIS_URL")
    parser.add_argument("--verbosity", type=int, default=1, help="0 silences the console")
    parser.add_argument("--man", action="store_true", default=False, help="print the full manual")
    parser.add_argument("--synopsis", action="store_true", default=False, help="print the manual's synopsis")
    parser.add_argument("--version", action="version", version=f"dymap {__version__}")
    return parser


parser: argparse.ArgumentParser = parser_build()


def dymap(options: argparse.Namespace, inputdir: Path, outputdir: Path) -> int:
    """The plugin's body: the manual, or one run.

    Example:
        dymap(parser.parse_args(["--man"]), Path("/tmp"), Path("/tmp"))
    """
    if options.man or options.synopsis:
        manual_print(MANUAL, shortOnly=not options.man)
        return 0
    return dymap_run(options, inputdir, outputdir)


main = chris_plugin(
    parser=parser,
    title=TITLE,
    category=CATEGORY,
    min_memory_limit=MIN_MEMORY,
    min_cpu_limit=MIN_CPU,
)(timed("dymap")(dymap))
