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
    dymap --plugin pl-freesurfer --CUBEurl http://cube:8000/api/v1/ /incoming /outgoing
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
from dymap.models import DEFAULT_ITEM_TEMPLATE, Options

TITLE: str = "Map a plugin or pipeline over the directories of the input: one child per item"
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
              Map a plugin or pipeline over an input tree

                         -- version {version} --

        A ChRIS DS plugin that creates one instance of a named plugin, or one
        workflow of a named pipeline, for each chosen directory of its input,
        each attached to the same data node and given its own directory, so
        that ChRIS runs the items in parallel, each in its own containers and
        with its own resources.
"""

SYNOPSIS_SHORT: str = """
    NAME

        dymap

    SYNOPSIS

        dymap                                                                   \\
            --plugin <name>[@<version>] | --pipeline <name>                     \\
            [--itemPiping <title>]                                              \\
            [--itemParam <parameter>]                                           \\
            [--itemTemplate <template>]                                         \\
            [--params <json>]                                                   \\
            [--itemFileFilter <filter>]                                         \\
            [--itemFileFilterLogic <AND|OR>]                                    \\
            [--itemDirFilter <filter>]                                          \\
            [--itemDirFilterLogic <AND|OR>]                                     \\
            [--itemDepth <n>]                                                   \\
            [--maxItems <n>]                                                    \\
            [--dryRun]                                                          \\
            [--CUBEurl <url>]                                                   \\
            [--verbosity <level>]                                               \\
            [--man] [--synopsis] [--version]                                    \\
            <inputDir> <outputDir>

    BRIEF EXAMPLE

        dymap --plugin pl-freesurfer --itemFileFilter 'glob:*.nii.gz'           \\
              --CUBEurl http://cube:8000/api/v1/ /incoming /outgoing
"""

SYNOPSIS_FULL: str = (
    SYNOPSIS_SHORT
    + f"""
    DESCRIPTION

        ``dymap`` runs a plugin, or a pipeline, once per item of its input.
        The items are directories of the input, chosen by up to three
        conditions: ``--itemFileFilter``, that the directory holds a file
        whose name passes the filter; ``--itemDirFilter``, that its path
        passes the filter; and ``--itemDepth``, how far below the input it
        is. With none of them the items are the input's own subdirectories;
        with a file filter alone they are the directories, at any depth, that
        hold a matching file.

        For each item the plugin creates one child attached to the same data
        node as itself: an instance of the ``--plugin``, or a workflow of the
        ``--pipeline``. The item's value goes to one parameter, by default
        ``--dirFilter`` set to a filter that matches that directory's path
        alone, so the child sees the whole input but processes one directory.
        ChRIS then schedules each child on its own, so the items run in
        parallel, each in its own containers and with the resources its
        plugin asks for, and each succeeds or fails alone.

        The plugin decides before it acts. It writes ``plan.json``, the items
        and the value each gets, before it creates anything, and
        ``created.json`` after, with each item's plugin instances and
        workflow, or the reason it failed. Both stay in the feed as the record
        of why the children exist. A rerun does not duplicate: an item whose
        child already hangs off the data node, with the item's name as its
        title and made from the same plugin or pipeline, is skipped.

        The plan is checked against CUBE before anything is created: the
        plugin or pipeline must exist by exact name, and the item parameter
        and every ``--params`` entry must be parameters of what they name,
        since CUBE ignores an unknown one and a misspelt item parameter would
        give every child the whole input.

        The plugin does not wait for its children. It speaks to CUBE with the
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
        The data node's output, as ChRIS mounts it. Its chosen directories
        are the items.

        <outputDir>
        Where ``plan.json`` and ``created.json`` are written.

        --plugin <name>[@<version>]
        The plugin each item gets one instance of, by exact name, at the
        version given or else the newest registered. One of ``--plugin`` and
        ``--pipeline`` is required.

        --pipeline <name>
        The exact name of the pipeline each item gets one workflow of.

        [--itemPiping <title>]
        For a pipeline, the title of the piping that receives the item. May
        be omitted when the pipeline has one piping.

        [--itemParam <parameter>]
        The parameter of the plugin, or of the item piping, set to the item's
        value. Default ``dirFilter``.

        [--itemTemplate <template>]
        How the value is built from the item: ``{{item}}`` is its path below
        the input and ``{{itemRegex}}`` that path escaped for a regular
        expression.
        Default ``{DEFAULT_ITEM_TEMPLATE}``, a filter matching exactly that
        directory; a plain ``sub-01`` would also match ``sub-010``.

        [--params <json>]
        Further parameter values for every child. For a plugin, by parameter
        name, as ``{{"verbosity": 2}}``; for a pipeline, by piping title and
        then parameter name, as ``{{"recon": {{"verbosity": 2}}}}``.

        [--itemFileFilter <filter>] [--itemFileFilterLogic <AND|OR>]
        A directory is an item only if it holds, directly, a file whose name
        passes this filter. With it, and no ``--itemDepth``, items are found
        at any depth, so a pl-dircopy node, which holds its data under a
        directory named for where it came from, needs no depth to be known.
        The filters are pftree's grammar: comma-separated tokens, each a
        substring, ``glob:``, ``re:`` or ``!`` negation.

        [--itemDirFilter <filter>] [--itemDirFilterLogic <AND|OR>]
        A directory is an item only if its path below the input passes this
        filter.

        [--itemDepth <n>]
        How far below the input the items are, 1 for its own
        subdirectories. Default 0: any depth when ``--itemFileFilter`` is set,
        and 1 when it is not. Directories whose names start with a period are
        never items, and the input itself is never one. Each child is titled
        by its item's own name, or, where names repeat, as series directories
        named alike under several subjects do, by as much of the item's path
        as tells it apart: sub-01/MPRAGE, sub-02/MPRAGE.

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

    One FreeSurfer instance per directory holding a NIfTI volume, at any
    depth:

        dymap --plugin pl-freesurfer@1.0.1 --itemFileFilter 'glob:*.nii.gz'     \\
              --CUBEurl http://cube:8000/api/v1/ /incoming /outgoing

    One FreeSurfer instance per MPRAGE series, where each subject holds
    several series directories; the instance reads that series' DICOM:

        dymap --plugin pl-freesurfer --itemFileFilter 'glob:*.dcm'              \\
              --itemDirFilter 're:MPRAGE[^/]*$'                                 \\
              --params '{{"fileFilter": "glob:*.dcm"}}'                         \\
              --CUBEurl http://cube:8000/api/v1/ /incoming /outgoing

    One workflow of a pipeline per subdirectory of the input:

        dymap --pipeline "FreeSurfer 8.2.0 per subject"                         \\
              --CUBEurl http://cube:8000/api/v1/ /incoming /outgoing

    Under a pl-dircopy node, whose subjects sit one level down, only those
    whose names start with sub-0, after looking at the plan:

        dymap --plugin pl-freesurfer --itemDepth 2 --dryRun                     \\
              --itemDirFilter 're:(^|/)sub-0[^/]*$' /incoming /outgoing

    A plugin instance with a further option:

        dymap --plugin pl-shexec --itemFileFilter dcm                           \\
              --params '{{"exec": "wc -l %{{inputWorkingDir}}"}}'               \\
              --CUBEurl http://cube:8000/api/v1/ /incoming /outgoing

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
    title="Map a plugin or pipeline over an input tree",
    text=BANNER_TEXT,
    version=__version__,
    synopsisShort=SYNOPSIS_SHORT,
    synopsisFull=SYNOPSIS_FULL,
)


def parser_build() -> argparse.ArgumentParser:
    """The plugin's flags, without the positional directories ChRIS adds.

    Every flag is a plain ``store`` or ``store_true``, which chris_plugin serialises;
    none is marked required, since ChRIS would then require it in every form, so a
    missing ``--plugin`` or ``--pipeline`` is caught in the run and exits 2.

    Example:
        >>> parser_build().parse_args(["--plugin", "pl-x"]).itemParam
        'dirFilter'
    """
    defaults: Options = Options()
    parser: argparse.ArgumentParser = argparse.ArgumentParser(
        prog="dymap",
        description="Create one child, a plugin instance or a workflow, per chosen directory of the input. "
        "Full manual: dymap --man",
    )
    parser.add_argument("--plugin", default="", help="plugin each item gets, name or name@version")
    parser.add_argument("--pipeline", default="", help="exact name of the pipeline each item gets")
    parser.add_argument("--itemPiping", default="", help="title of the piping that receives the item")
    parser.add_argument("--itemParam", default=defaults.itemParam, help="parameter set to the item")
    parser.add_argument(
        "--itemTemplate",
        default=defaults.itemTemplate,
        help="the item's value: {item} is its path, {itemRegex} the path escaped for a regex",
    )
    parser.add_argument("--params", default="", help="JSON of further values for every child")
    parser.add_argument("--itemFileFilter", default="", help="an item holds a file passing this filter")
    parser.add_argument("--itemFileFilterLogic", default="OR", help="AND or OR, how its tokens combine")
    parser.add_argument("--itemDirFilter", default="", help="an item's path passes this filter")
    parser.add_argument("--itemDirFilterLogic", default="OR", help="AND or OR, how its tokens combine")
    parser.add_argument(
        "--itemDepth", type=int, default=defaults.itemDepth, help="depth of the items; 0 for the default"
    )
    parser.add_argument(
        "--maxItems", type=int, default=defaults.maxItems, help="refuse an input with more items than this"
    )
    parser.add_argument("--dryRun", action="store_true", default=False, help="write the plan, create nothing")
    parser.add_argument("--CUBEurl", default="", help="CUBE API root as reachable from the job; or CHRIS_URL")
    parser.add_argument("--verbosity", type=int, default=defaults.verbosity, help="0 silences the console")
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
    return dymap_run(Options.fromNamespace(options), inputdir, outputdir)


main = chris_plugin(
    parser=parser,
    title=TITLE,
    category=CATEGORY,
    min_memory_limit=MIN_MEMORY,
    min_cpu_limit=MIN_CPU,
)(timed("dymap")(dymap))
