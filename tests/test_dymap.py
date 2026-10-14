"""pl-dymap: the plan, posting workflows and plugin instances against a CUBE stand-in, the real
client against a stub server, the records, the exit status, and the plugin description."""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, ClassVar

import pytest
from chris_plugin.parameters import serialize
from hostlib import JobSpec, job_run

from dymap import __version__
from dymap.__main__ import main, parser
from dymap.cube import CubeClient
from dymap.dymap import dymap_run
from dymap.models import (
    CubeError,
    InstanceRef,
    ItemSelection,
    JobEnv,
    NodeInfo,
    Options,
    ParamSetting,
    PipelineRef,
    PipingParam,
    PipingRef,
    PluginParameter,
    PluginRef,
    WorkflowRef,
)
from dymap.plan import PlanRefused, items_find, plan_build

TOKEN: str = "tok-3a7c9e1f-never-written"
PIPELINE: str = "FreeSurfer per subject"
PLUGIN: str = "pl-freesurfer"


def inputTree_make(root: Path, names: tuple[str, ...] = ("sub-01", "sub-02", "sub-010")) -> tuple[Path, Path]:
    inputDir: Path = root / "incoming"
    outputDir: Path = root / "outgoing"
    for name in names:
        (inputDir / name).mkdir(parents=True)
        (inputDir / name / "t1.nii.gz").write_text("x")
    (inputDir / "notes.txt").write_text("not an item")
    outputDir.mkdir()
    return inputDir, outputDir


class FakeCube:
    """A CUBE with one pipeline of one piping and two versions of one plugin, recording every
    workflow and plugin instance posted."""

    def __init__(self, params: tuple[str, ...] = ("dirFilter", "verbosity"), failOn: str = "") -> None:
        self.url: str = "http://cube.test/api/v1/"
        self.paramNames: tuple[str, ...] = params
        self.failOn: str = failOn
        self.posted: list[dict[str, Any]] = []
        self.instances: list[InstanceRef] = []
        self.workflows: dict[int, WorkflowRef] = {}
        self.instancesPosted: list[dict[str, Any]] = []

    def pipeline_find(self, name: str) -> PipelineRef | None:
        return PipelineRef(id=3, name=name) if name == PIPELINE else None

    def pipings_list(self, pipelineId: int) -> list[PipingRef]:
        return [PipingRef(id=7, title="recon", previousId=None)]

    def params_list(self, pipelineId: int) -> list[PipingParam]:
        return [PipingParam(pipingId=7, name=name, value="") for name in self.paramNames]

    def children_find(self, previousId: int, title: str) -> list[InstanceRef]:
        return [i for i in self.instances if i.previousId == previousId and i.title == title]

    def workflow_get(self, workflowId: int) -> WorkflowRef:
        return self.workflows[workflowId]

    def plugins_find(self, name: str) -> list[PluginRef]:
        if name != PLUGIN:
            return []
        return [PluginRef(id=5, name=name, version="0.9.0"), PluginRef(id=9, name=name, version="1.0.0")]

    def pluginParams_list(self, pluginId: int) -> list[PluginParameter]:
        return [PluginParameter(name=name, type="string") for name in self.paramNames]

    def workflow_post(
        self, pipelineId: int, previousId: int, title: str, nodes: list[NodeInfo]
    ) -> WorkflowRef:
        if nodes[0].title == self.failOn:
            raise CubeError(400, "refused")
        workflowId: int = 100 + len(self.posted)
        self.posted.append(
            {
                "pipelineId": pipelineId,
                "previousId": previousId,
                "title": title,
                "nodes": [node.toDict() for node in nodes],
            }
        )
        self.workflows[workflowId] = WorkflowRef(id=workflowId, pipelineId=pipelineId, title=title)
        self.instances.append(
            InstanceRef(
                id=1000 + workflowId,
                title=nodes[0].title,
                previousId=previousId,
                workflowId=workflowId,
                pluginId=9,
            )
        )
        return self.workflows[workflowId]

    def instance_post(
        self, pluginId: int, previousId: int, title: str, settings: list[ParamSetting]
    ) -> InstanceRef:
        if title == self.failOn:
            raise CubeError(400, "refused")
        instance = InstanceRef(
            id=2000 + len(self.instancesPosted),
            title=title,
            previousId=previousId,
            workflowId=None,
            pluginId=pluginId,
        )
        self.instancesPosted.append(
            {
                "pluginId": pluginId,
                "previousId": previousId,
                "title": title,
                "params": {s.name: s.value for s in settings},
            }
        )
        self.instances.append(instance)
        return instance

    def workflowInstances_list(self, workflowId: int) -> list[InstanceRef]:
        return [i for i in self.instances if i.workflowId == workflowId]


def run(
    options: list[str], inputDir: Path, outputDir: Path, cube: FakeCube, env: JobEnv | None = None
) -> int:
    job: JobEnv = JobEnv(token=TOKEN, previousId=42, url="") if env is None else env
    return dymap_run(
        Options.fromNamespace(parser.parse_args(options)), inputDir, outputDir, job, lambda url, token: cube
    )


NO_ENV: JobEnv = JobEnv(token="", previousId=None, url="")


def test_plan_itemsAreTopLevelDirectories_valuesMatchExactly(tmp_path: Path) -> None:
    inputDir, _ = inputTree_make(tmp_path)
    plan = plan_build(inputDir, Options(pipeline=PIPELINE), [], previousId=42)
    assert [item.item for item in plan.items] == ["sub-01", "sub-010", "sub-02"]
    assert plan.items[0].value == "re:^sub\\-01$"
    assert plan.previousId == 42


def test_plan_refusesTooManyItems_andCommas(tmp_path: Path) -> None:
    inputDir, _ = inputTree_make(tmp_path)
    with pytest.raises(PlanRefused, match="maxItems"):
        plan_build(inputDir, Options(pipeline=PIPELINE, maxItems=2), [])
    (inputDir / "a,b").mkdir()
    with pytest.raises(PlanRefused, match="comma"):
        plan_build(inputDir, Options(pipeline=PIPELINE), [])
    with pytest.raises(PlanRefused, match="names neither"):
        plan_build(inputDir, Options(pipeline=PIPELINE, itemTemplate="fixed", itemDirFilter="sub-01"), [])


def test_dryRun_writesPlan_needsNoCube(tmp_path: Path) -> None:
    inputDir, outputDir = inputTree_make(tmp_path)
    cube = FakeCube()
    assert (
        run(
            ["--pipeline", PIPELINE, "--dryRun", "--itemDirFilter", "re:^sub-0[12]$"],
            inputDir,
            outputDir,
            cube,
            NO_ENV,
        )
        == 0
    )
    plan = json.loads((outputDir / "plan.json").read_text())
    assert (
        plan["schema"] == "dymap-plan/2"
        and plan["kind"] == "pipeline"
        and [i["item"] for i in plan["items"]] == ["sub-01", "sub-02"]
    )
    record = json.loads((outputDir / "created.json").read_text())
    assert record["counts"]["planned"] == 2 and cube.posted == []


def test_post_oneWorkflowPerItem_underTheDataNode(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    inputDir, outputDir = inputTree_make(tmp_path)
    cube = FakeCube()
    assert run(["--pipeline", PIPELINE, "--CUBEurl", cube.url], inputDir, outputDir, cube) == 0
    assert len(cube.posted) == 3
    first = cube.posted[0]
    assert first["previousId"] == 42 and first["pipelineId"] == 3
    assert first["nodes"] == [
        {
            "piping_id": 7,
            "title": "sub-01",
            "plugin_parameter_defaults": [{"name": "dirFilter", "default": "re:^sub\\-01$"}],
        }
    ]
    record = json.loads((outputDir / "created.json").read_text())
    assert record["counts"] == {"planned": 0, "posted": 3, "skipped": 0, "failed": 0}
    assert record["outcomes"][0]["workflowId"] == 100 and record["outcomes"][0]["instanceIds"] == [1100]
    out: str = capsys.readouterr().out
    assert "posted   sub-01  workflow 100" in out and "3 posted, 0 skipped, 0 failed" in out


def test_rerun_skipsWhatExists(tmp_path: Path) -> None:
    inputDir, outputDir = inputTree_make(tmp_path)
    cube = FakeCube()
    assert run(["--pipeline", PIPELINE, "--CUBEurl", cube.url], inputDir, outputDir, cube) == 0
    (inputDir / "sub-03").mkdir()
    assert run(["--pipeline", PIPELINE, "--CUBEurl", cube.url], inputDir, outputDir, cube) == 0
    record = json.loads((outputDir / "created.json").read_text())
    assert record["counts"]["skipped"] == 3 and record["counts"]["posted"] == 1
    assert len(cube.posted) == 4


def test_sameTitleFromAnotherPipeline_isNotOurs(tmp_path: Path) -> None:
    inputDir, outputDir = inputTree_make(tmp_path, ("sub-01",))
    cube = FakeCube()
    cube.workflows[9] = WorkflowRef(id=9, pipelineId=99, title="other")
    cube.instances.append(InstanceRef(id=900, title="sub-01", previousId=42, workflowId=9, pluginId=9))
    assert run(["--pipeline", PIPELINE, "--CUBEurl", cube.url], inputDir, outputDir, cube) == 0
    assert len(cube.posted) == 1


def test_oneItemFails_othersPosted_exitOne(tmp_path: Path) -> None:
    inputDir, outputDir = inputTree_make(tmp_path)
    cube = FakeCube(failOn="sub-010")
    assert run(["--pipeline", PIPELINE, "--CUBEurl", cube.url], inputDir, outputDir, cube) == 1
    record = json.loads((outputDir / "created.json").read_text())
    assert record["counts"] == {"planned": 0, "posted": 2, "skipped": 0, "failed": 1}
    assert record["status"] is False and "refused" in record["outcomes"][1]["error"]


def test_misspeltItemParam_rejectedBeforeAnythingIsCreated(tmp_path: Path) -> None:
    inputDir, outputDir = inputTree_make(tmp_path)
    cube = FakeCube()
    assert (
        run(
            ["--pipeline", PIPELINE, "--itemParam", "dirFiltr", "--CUBEurl", cube.url],
            inputDir,
            outputDir,
            cube,
        )
        == 1
    )
    assert cube.posted == []
    assert "no parameter 'dirFiltr'" in json.loads((outputDir / "created.json").read_text())["error"]


def test_params_validated_andApplied(tmp_path: Path) -> None:
    inputDir, outputDir = inputTree_make(tmp_path, ("sub-01",))
    cube = FakeCube()
    bad: list[str] = ["--pipeline", PIPELINE, "--params", '{"recon": {"verbosty": 2}}', "--CUBEurl", cube.url]
    assert run(bad, inputDir, outputDir, cube) == 1 and cube.posted == []
    good: list[str] = [
        "--pipeline",
        PIPELINE,
        "--params",
        '{"recon": {"verbosity": 2}}',
        "--CUBEurl",
        cube.url,
    ]
    assert run(good, inputDir, outputDir, cube) == 0
    assert cube.posted[0]["nodes"][0]["plugin_parameter_defaults"] == [
        {"name": "verbosity", "default": 2},
        {"name": "dirFilter", "default": "re:^sub\\-01$"},
    ]


def test_unknownPipeline_and_missingEnvironment(tmp_path: Path) -> None:
    inputDir, outputDir = inputTree_make(tmp_path)
    cube = FakeCube()
    assert run(["--pipeline", "nope", "--CUBEurl", cube.url], inputDir, outputDir, cube) == 1
    assert run(["--pipeline", PIPELINE, "--CUBEurl", cube.url], inputDir, outputDir, cube, NO_ENV) == 1
    assert run(["--pipeline", PIPELINE], inputDir, outputDir, cube) == 2
    assert run(["--params", "{}"], inputDir, outputDir, cube) == 2
    assert run(["--pipeline", PIPELINE, "--plugin", PLUGIN], inputDir, outputDir, cube) == 2
    assert run(["--plugin", PLUGIN, "--itemPiping", "recon"], inputDir, outputDir, cube) == 2
    assert run(["--pipeline", PIPELINE, "--itemDepth", "-1"], inputDir, outputDir, cube) == 2
    assert (
        run(["--pipeline", PIPELINE, "--params", "[1]", "--CUBEurl", cube.url], inputDir, outputDir, cube)
        == 2
    )
    assert cube.posted == []


def test_tokenNeverWrittenOrPrinted(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    inputDir, outputDir = inputTree_make(tmp_path)
    cube = FakeCube(failOn="sub-02")
    run(["--pipeline", PIPELINE, "--CUBEurl", cube.url], inputDir, outputDir, cube)
    captured = capsys.readouterr()
    for path in outputDir.iterdir():
        assert TOKEN not in path.read_text()
    assert TOKEN not in captured.out and TOKEN not in captured.err
    assert TOKEN not in repr(CubeClient(cube.url, TOKEN))
    assert TOKEN not in repr(JobEnv(token=TOKEN, previousId=42, url=""))


class StubCube(BaseHTTPRequestHandler):
    """A two-page pipings listing and a workflow POST, recording what arrives."""

    seen: ClassVar[list[dict[str, Any]]] = []

    def log_message(self, format: str, *args: Any) -> None:
        return

    def reply(self, code: int, body: dict[str, Any]) -> None:
        payload: bytes = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:
        self.seen.append({"method": "GET", "path": self.path, "auth": self.headers.get("Authorization")})
        base: str = f"http://127.0.0.1:{self.server.server_address[1]}/api/v1/"
        if self.path.startswith("/api/v1/pipelines/3/pipings/?limit"):
            self.reply(
                200, {"next": base + "pipelines/3/pipings/?page=2", "results": [{"id": 7, "title": "a"}]}
            )
        elif self.path == "/api/v1/pipelines/3/pipings/?page=2":
            self.reply(200, {"next": None, "results": [{"id": 8, "title": "b", "previous_id": 7}]})
        else:
            self.reply(404, {"detail": "Not found."})

    def do_POST(self) -> None:
        length: int = int(self.headers.get("Content-Length", "0"))
        body: dict[str, Any] = json.loads(self.rfile.read(length))
        self.seen.append({"method": "POST", "path": self.path, "body": body})
        self.reply(201, {"id": 12, "pipeline_id": 3, "title": body["title"]})


@pytest.fixture
def stubServer() -> Iterator[str]:
    StubCube.seen = []
    server: HTTPServer = HTTPServer(("127.0.0.1", 0), StubCube)
    thread: threading.Thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}/api/v1"
    server.shutdown()


def test_client_followsPages_sendsToken_postsWorkflow(stubServer: str) -> None:
    client = CubeClient(stubServer, TOKEN)
    pipings = client.pipings_list(3)
    assert [(p.id, p.previousId) for p in pipings] == [(7, None), (8, 7)]
    assert all(seen["auth"] == f"Token {TOKEN}" for seen in StubCube.seen)
    workflow = client.workflow_post(3, 42, "p: sub-01", [NodeInfo(pipingId=7, title="sub-01")])
    assert workflow.id == 12
    body = StubCube.seen[-1]["body"]
    assert body["previous_plugin_inst_id"] == 42 and json.loads(body["nodes_info"])[0]["piping_id"] == 7
    with pytest.raises(CubeError) as raised:
        client.workflow_get(5)
    assert raised.value.status == 404


def test_client_unreachable_isCubeError() -> None:
    with pytest.raises(CubeError) as raised:
        CubeClient("http://127.0.0.1:9/api/v1/", TOKEN).pipings_list(1)
    assert raised.value.status == 0


def test_parser_serialisesForChris_noRequiredFlags() -> None:
    names: set[str] = {spec["name"] for spec in serialize(parser)}
    assert {
        "plugin",
        "pipeline",
        "itemPiping",
        "itemParam",
        "itemTemplate",
        "params",
        "itemFileFilter",
        "itemDirFilter",
        "itemDepth",
        "maxItems",
        "dryRun",
    } <= names
    assert "dirFilter" not in names
    assert "CUBEtoken" not in names and not any(action.required for action in parser._actions)


def test_main_manual_andSpan(capsys: pytest.CaptureFixture[str]) -> None:
    options: argparse.Namespace = parser.parse_args(["--man"])
    assert main(options, Path("/tmp"), Path("/tmp")) == 0
    captured = capsys.readouterr()
    assert "--itemTemplate" in captured.out and "CHRIS_USER_TOKEN" in captured.out
    span = json.loads(captured.err.strip().splitlines()[-1])
    assert span["name"] == "dymap" and span["status"] is True


def test_chris_plugin_info_describesThePlugin() -> None:
    binDir: Path = Path(sys.executable).parent
    spec = JobSpec(
        argv=[str(binDir / "chris_plugin_info"), "--dock-image", "docker.io/fnndsc/pl-dymap:2.0.0", "dymap"],
        env={"PATH": f"{binDir}{os.pathsep}{os.environ.get('PATH', '')}"},
    )
    result = job_run(spec)
    assert result.status, [*result.errors, result.stderr]
    description = json.loads(result.stdout)
    assert description["type"] == "ds" and description["selfexec"] == "dymap"
    assert description["version"] == __version__ and description["min_memory_limit"] == "100Mi"


def test_itemDepth_reachesSubjectsUnderALink_titlesAreNames(tmp_path: Path) -> None:
    inputDir, outputDir = inputTree_make(
        tmp_path, ("home_chris_uploads_subjects/sub-01", "home_chris_uploads_subjects/sub-02")
    )
    cube = FakeCube()
    assert (
        run(["--pipeline", PIPELINE, "--itemDepth", "2", "--CUBEurl", cube.url], inputDir, outputDir, cube)
        == 0
    )
    node = cube.posted[0]["nodes"][0]
    assert node["title"] == "sub-01"
    assert node["plugin_parameter_defaults"] == [
        {"name": "dirFilter", "default": "re:^home_chris_uploads_subjects/sub\\-01$"}
    ]


def test_itemDepth_collidingNames_andNoItems_refused(tmp_path: Path) -> None:
    inputDir, outputDir = inputTree_make(tmp_path, ("a/sub-01", "b/sub-01"))
    cube = FakeCube()
    assert (
        run(["--pipeline", PIPELINE, "--itemDepth", "2", "--CUBEurl", cube.url], inputDir, outputDir, cube)
        == 1
    )
    assert "share the name 'sub-01'" in json.loads((outputDir / "created.json").read_text())["error"]
    assert (
        run(["--pipeline", PIPELINE, "--itemDepth", "5", "--CUBEurl", cube.url], inputDir, outputDir, cube)
        == 1
    )
    assert cube.posted == []


def test_itemFileFilter_findsItemsAtAnyDepth_andCombinesWithDirFilter(tmp_path: Path) -> None:
    inputDir, _ = inputTree_make(tmp_path, ("link/sub-01", "link/sub-02/anat", "link/sub-03", "link/.cache"))
    (inputDir / "link/sub-02/anat/t1.nii.gz").write_text("x")
    (inputDir / "link/sub-03/t1.nii.gz").unlink()
    (inputDir / "link/sub-03/notes.txt").write_text("x")
    (inputDir / "link/.cache/t1.nii.gz").write_text("x")
    nifti = ItemSelection(fileFilter="glob:*.nii.gz")
    assert items_find(inputDir, nifti) == ["link/sub-01", "link/sub-02/anat"]
    assert items_find(inputDir, ItemSelection(fileFilter="glob:*.nii.gz", depth=2)) == ["link/sub-01"]
    assert items_find(inputDir, ItemSelection(fileFilter="glob:*.nii.gz", dirFilter="anat")) == [
        "link/sub-02/anat"
    ]
    assert items_find(inputDir, ItemSelection(fileFilter="notes,t1", fileFilterLogic="OR", depth=2)) == [
        "link/sub-01",
        "link/sub-03",
    ]


def test_itemFileFilter_followsALinkOnce(tmp_path: Path) -> None:
    inputDir, _ = inputTree_make(tmp_path, ("data/sub-01",))
    (inputDir / "home_chris_uploads").symlink_to(inputDir / "data")
    (inputDir / "data/loop").symlink_to(inputDir / "data")
    assert items_find(inputDir, ItemSelection(fileFilter="glob:*.nii.gz")) in (
        ["data/sub-01"],
        ["home_chris_uploads/sub-01"],
    )


def test_plugin_oneInstancePerItem_newestVersion_rerunSkips(tmp_path: Path) -> None:
    inputDir, outputDir = inputTree_make(tmp_path, ("link/sub-01", "link/sub-02"))
    cube = FakeCube()
    options: list[str] = ["--plugin", PLUGIN, "--itemFileFilter", "glob:*.nii.gz", "--CUBEurl", cube.url]
    assert run([*options, "--params", '{"verbosity": 2}'], inputDir, outputDir, cube) == 0
    assert cube.posted == [] and len(cube.instancesPosted) == 2
    first = cube.instancesPosted[0]
    assert first["pluginId"] == 9 and first["previousId"] == 42 and first["title"] == "sub-01"
    assert first["params"] == {"verbosity": 2, "dirFilter": "re:^link/sub\\-01$"}
    record = json.loads((outputDir / "created.json").read_text())
    assert record["pluginId"] == 9 and record["pipelineId"] is None
    assert record["outcomes"][0]["instanceIds"] == [2000] and record["outcomes"][0]["workflowId"] is None
    plan = json.loads((outputDir / "plan.json").read_text())
    assert plan["kind"] == "plugin" and plan["params"] == [{"piping": "", "name": "verbosity", "value": 2}]
    assert run(options, inputDir, outputDir, cube) == 0
    assert json.loads((outputDir / "created.json").read_text())["counts"]["skipped"] == 2
    assert len(cube.instancesPosted) == 2


def test_plugin_explicitVersion_otherVersionIsNotOurs(tmp_path: Path) -> None:
    inputDir, outputDir = inputTree_make(tmp_path, ("sub-01",))
    cube = FakeCube()
    cube.instances.append(InstanceRef(id=800, title="sub-01", previousId=42, workflowId=None, pluginId=9))
    assert run(["--plugin", f"{PLUGIN}@0.9.0", "--CUBEurl", cube.url], inputDir, outputDir, cube) == 0
    assert [posted["pluginId"] for posted in cube.instancesPosted] == [5]


def test_plugin_instanceInsideAWorkflow_isNotOurs(tmp_path: Path) -> None:
    inputDir, outputDir = inputTree_make(tmp_path, ("sub-01",))
    cube = FakeCube()
    assert run(["--pipeline", PIPELINE, "--CUBEurl", cube.url], inputDir, outputDir, cube) == 0
    assert run(["--plugin", PLUGIN, "--CUBEurl", cube.url], inputDir, outputDir, cube) == 0
    assert len(cube.posted) == 1 and len(cube.instancesPosted) == 1


def test_plugin_rejections_createNothing(tmp_path: Path) -> None:
    inputDir, outputDir = inputTree_make(tmp_path, ("sub-01",))
    cube = FakeCube()
    for options in (
        ["--plugin", "pl-nope"],
        ["--plugin", f"{PLUGIN}@3.0.0"],
        ["--plugin", PLUGIN, "--itemParam", "dirFiltr"],
        ["--plugin", PLUGIN, "--params", '{"verbosty": 2}'],
    ):
        assert run([*options, "--CUBEurl", cube.url], inputDir, outputDir, cube) == 1, options
        assert json.loads((outputDir / "created.json").read_text())["error"]
    assert run(["--plugin", PLUGIN, "--params", '{"exec": [1]}'], inputDir, outputDir, cube) == 2
    assert cube.instancesPosted == []


def test_options_fromNamespace_ignoresManualFlags() -> None:
    options = Options.fromNamespace(parser.parse_args(["--plugin", PLUGIN, "--man"]))
    assert options.plugin == PLUGIN and options.selection.depthEffective == 1
