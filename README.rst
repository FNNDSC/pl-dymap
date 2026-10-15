pl-dymap
========

*Map a ChRIS plugin or pipeline over the directories of an input: one child per item, run in parallel.*

Why it exists
-------------

A ChRIS plugin runs in one container with one memory limit. Some programs need most of a machine for one input: FreeSurfer 8's ``recon-all`` peaks near 16 GB per subject. A plugin that loops over forty subjects in its one container runs them one after another, for days. One that runs them side by side in that container runs out of memory and loses all of them.

The way out is one plugin instance per subject. ChRIS schedules each instance as its own container, with its own memory, on whatever node has room, and each succeeds or fails alone. But a ChRIS pipeline is a fixed graph. It has no step that means "one branch per directory found here," and the number of subjects is not known until the data arrives.

pl-dymap is that step. It is an ordinary plugin in the feed: it looks at its input, decides one child per item, writes that decision down, and asks CUBE to create the children beside it. The decision and its result stay in the feed as data, like the output of any other node, so anyone reading the feed can see why the branches exist.

What it does
------------

Attach pl-dymap to a data node holding subjects and name a plugin:

.. code-block:: console

    dymap --plugin pl-freesurfer --itemFileFilter 'glob:*.nii.gz' --CUBEurl http://cube:8000/api/v1/ /incoming /outgoing

Every directory of the input that holds a NIfTI volume, at whatever depth, is an item; here ``sub-01`` to ``sub-40``. For each, pl-dymap creates one instance of pl-freesurfer under the same data node. Each instance is given a filter that selects its own subject and nothing else, by default ``--dirFilter re:^sub\-01$``, so the plugin sees the whole input but processes one directory. The feed afterwards looks like this:

.. code-block:: text

    data node (sub-01 ... sub-40)
    ├── pl-dymap          plan.json, created.json
    ├── sub-01: pl-freesurfer   its own 32Gi container
    ├── sub-02: pl-freesurfer   its own 32Gi container
    └── ...

``--pipeline`` in place of ``--plugin`` creates one workflow of a named pipeline per item instead, for work of several steps.

pl-dymap does not wait for its children; it creates them and exits. Its output holds two files:

* ``plan.json``, written before anything is created: the plugin or pipeline, and each item with the title and filter it gets.
* ``created.json``, written after: for each item, whether it was posted, skipped or failed, with its plugin instances and workflow, or the reason it failed.

Running it again does not duplicate work. An item whose child already hangs off the data node, with the item's name as its title and made from the same plugin or pipeline, is skipped, so a rerun after a partial failure creates only what is missing.

Choosing the items
------------------

The items are directories of the input, chosen by up to three conditions:

* ``--itemFileFilter``: the directory holds a file whose name passes the filter, as ``glob:*.nii.gz`` or ``dcm``.
* ``--itemDirFilter``: the directory's path below the input passes the filter, as ``re:(^|/)sub-[^/]*$``.
* ``--itemDepth``: the directory is that far below the input.

With a file filter and no depth, items are found at any depth. That matters because a data node often holds its data under a directory named for where it came from: pl-dircopy presents an upload as ``home_chris_uploads_subjects``, with the subjects inside. With no item flags at all, the items are the input's own subdirectories. The filters use the pf* stack's grammar: comma-separated tokens, each a substring, ``glob:``, ``re:`` or ``!`` negation.

Each child is titled by its item's own name, ``sub-01``. Where names repeat, as series directories named alike under several subjects do, a title takes as much of the item's path as tells it apart: ``sub-01/MPRAGE``, ``sub-02/MPRAGE``. A data node's output never changes, so a rerun gives the same titles and recognises its earlier children.

Some layouts and the flags for them:

.. list-table::
   :header-rows: 1

   * - Layout
     - Flags
     - Items
   * - ``subjN/*.dcm``, each subject one T1 series
     - ``--itemFileFilter 'glob:*.dcm'``
     - ``subjN``
   * - ``subjN/<series>/*.dcm``, only the MPRAGE series wanted
     - ``--itemFileFilter 'glob:*.dcm' --itemDirFilter 're:MPRAGE[^/]*$'``
     - ``subjN/MPRAGE...``, one per series
   * - ``subjN/anat/*.nii.gz``
     - ``--itemFileFilter 're:\.nii(\.gz)?$'``
     - ``subjN/anat``
   * - any of these under a pl-dircopy node
     - the same; any depth absorbs the node's link directory
     - the same

The child plugin must still pick the right file within its item: for pl-freesurfer reading DICOM, ``--params '{"fileFilter": "glob:*.dcm"}'``.

Guards
------

pl-dymap checks its plan against CUBE before creating anything. The plugin or pipeline must exist by exact name. The parameter that receives the item, ``--itemParam``, must be one of its parameters: CUBE silently ignores an unknown parameter, so a misspelt ``dirFiltr`` would otherwise hand every child the whole input. ``--maxItems``, by default 100, refuses an input with more items, so that a mistaken input cannot flood the cluster. ``--dryRun`` writes the plan and creates nothing.

Authority
---------

pl-dymap speaks to CUBE with the token CUBE gives every plugin job, ``CHRIS_USER_TOKEN``, and attaches its children to the data node CUBE names in ``CHRIS_PREV_PLG_INST_ID``. The token is never a flag, never written to the output, and never printed. CUBE does not yet tell a job its own address, so ``--CUBEurl`` names it, as reachable from inside the job's container, or the job's ``CHRIS_URL`` environment variable does.

That token is the job owner's full API token, as CUBE gives it to every plugin today. pl-dymap uses it only to read plugins, pipelines and instances and to create children under its own data node, but the token itself is not limited to that.

Plugins and pipelines
---------------------

``--plugin pl-freesurfer`` takes the newest registered version; ``--plugin pl-freesurfer@1.0.0`` pins one. Each instance gets the plugin's own resources: pl-freesurfer's minimum memory, 32Gi, sizes every child. ``--params '{"verbosity": 2}'`` sets further values on every instance.

A pipeline suits work of several steps. ``--itemPiping`` names the piping that receives the item, and ``--params`` sets values on any piping, by title: ``--params '{"count": {"verbosity": 2}}'``.

Exit status
-----------

0 when every item was created or skipped; 1 when the plan was refused or rejected, CUBE could not be reached, or any item failed; 2 on a usage error, such as neither ``--plugin`` nor ``--pipeline``. ``dymap --man`` prints the full manual.

Development
-----------

.. code-block:: console

    uv venv --python /usr/bin/python3.14 .venv
    uv pip install --python .venv/bin/python -e ".[dev]"
    .venv/bin/pytest -q

The tests plan over temporary trees, post against a stand-in for CUBE, and exercise the real client against a local HTTP stub, so they need no ChRIS. CI lints and tests on Python 3.11 to 3.14, builds the image on every push to ``main``, and on a ``v*`` tag pushes it to Docker Hub and ghcr.io and uploads the plugin's description to cube.chrisproject.org.
