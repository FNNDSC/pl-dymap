"""Speaking to CUBE: the few REST calls a plan needs, over plain JSON, with the job's token.

CUBE answers ``Accept: application/json`` with paginated ``count``, ``next`` and
``results``; the client follows ``next`` until it runs out. Every answer enters as a
``JsonRow`` and leaves this module as a record. The token is CUBE's own,
``CHRIS_USER_TOKEN``, given to the client at construction and sent only in the
``Authorization`` header; it is not part of any record, message or ``repr``.

Key components:
    - CubeApi: what the posting step needs from CUBE, so a test can supply a stand-in
    - CubeClient: the real client, standard library only

Dependencies:
    - dymap.models
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Protocol

from dymap.models import (
    CubeError,
    InstanceRef,
    JsonRow,
    NodeInfo,
    ParamSetting,
    PipelineRef,
    PipingParam,
    PipingRef,
    PluginParameter,
    PluginRef,
    WorkflowRef,
)

PAGE_LIMIT: int = 100
TIMEOUT_SECONDS: float = 30.0


class CubeApi(Protocol):
    """The calls the posting step makes; the real client and the tests' stand-in both have them."""

    url: str

    def pipeline_find(self, name: str) -> PipelineRef | None:
        """The pipeline with exactly this name, or ``None``."""
        ...

    def pipings_list(self, pipelineId: int) -> list[PipingRef]:
        """The pipeline's pipings."""
        ...

    def params_list(self, pipelineId: int) -> list[PipingParam]:
        """Every piping parameter of the pipeline, with the pipeline's defaults."""
        ...

    def plugins_find(self, name: str) -> list[PluginRef]:
        """Every version of the plugin with exactly this name."""
        ...

    def pluginParams_list(self, pluginId: int) -> list[PluginParameter]:
        """The plugin's parameters."""
        ...

    def children_find(self, previousId: int, title: str) -> list[InstanceRef]:
        """The instances directly under ``previousId`` whose title is exactly ``title``."""
        ...

    def workflow_get(self, workflowId: int) -> WorkflowRef:
        """One workflow."""
        ...

    def workflow_post(
        self, pipelineId: int, previousId: int, title: str, nodes: list[NodeInfo]
    ) -> WorkflowRef:
        """Create a workflow of the pipeline under ``previousId``."""
        ...

    def workflowInstances_list(self, workflowId: int) -> list[InstanceRef]:
        """The plugin instances of a workflow."""
        ...

    def instance_post(
        self, pluginId: int, previousId: int, title: str, settings: list[ParamSetting]
    ) -> InstanceRef:
        """Create an instance of the plugin under ``previousId``."""
        ...


def instance_read(row: JsonRow) -> InstanceRef:
    """A plugin instance from CUBE's JSON.

    Example:
        >>> instance_read(JsonRow({"id": 9, "title": "a", "previous_id": 4, "plugin_id": 6})).pluginId
        6
    """
    return InstanceRef(
        id=row.integer("id"),
        title=row.text("title"),
        previousId=row.integerOrNone("previous_id"),
        workflowId=row.integerOrNone("workflow_id"),
        pluginId=row.integerOrNone("plugin_id"),
        status=row.text("status"),
    )


class CubeClient:
    """The CUBE REST API, as much of it as a plan needs.

    Attributes:
        url: the API root, ending in ``/api/v1/``.

    Example:
        >>> CubeClient("http://cube:8000/api/v1", "secret")
        CubeClient(url='http://cube:8000/api/v1/')
    """

    def __init__(self, url: str, token: str) -> None:
        self.url: str = url.rstrip("/") + "/"
        self._token: str = token

    def __repr__(self) -> str:
        return f"CubeClient(url={self.url!r})"

    def request_send(self, method: str, path: str, body: JsonRow | None = None) -> JsonRow:
        """One request, its JSON answer decoded.

        Args:
            method: ``GET`` or ``POST``.
            path: relative to the API root, or an absolute URL as CUBE's ``next`` gives.
            body: the JSON body of a POST.

        Returns:
            The decoded answer; an empty row when CUBE sent no object.

        Raises:
            CubeError: an HTTP error, with CUBE's message, or no response at all.

        Example:
            client.request_send("GET", "pipelines/search/?name_exact=x")
        """
        target: str = path if path.startswith(("http://", "https://")) else self.url + path.lstrip("/")
        data: bytes | None = json.dumps(body.fields).encode() if body is not None else None
        request: urllib.request.Request = urllib.request.Request(target, data=data, method=method)
        request.add_header("Accept", "application/json")
        request.add_header("Authorization", f"Token {self._token}")
        if data is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
                decoded: object = json.loads(response.read() or b"null")
        except urllib.error.HTTPError as err:
            detail: str = err.read().decode(errors="replace")[:500]
            raise CubeError(err.code, detail) from None
        except (urllib.error.URLError, TimeoutError, OSError) as err:
            raise CubeError(0, f"no response from {target}: {err}") from None
        return JsonRow(decoded) if isinstance(decoded, dict) else JsonRow()

    def results_all(self, path: str) -> list[JsonRow]:
        """Every result of a paginated listing, following ``next``.

        Example:
            client.results_all("pipelines/3/pipings/")
        """
        separator: str = "&" if "?" in path else "?"
        page: JsonRow = self.request_send("GET", f"{path}{separator}limit={PAGE_LIMIT}")
        results: list[JsonRow] = page.rows("results")
        while page.text("next"):
            page = self.request_send("GET", page.text("next"))
            results.extend(page.rows("results"))
        return results

    def pipeline_find(self, name: str) -> PipelineRef | None:
        """The pipeline with exactly this name, or ``None``.

        Example:
            client.pipeline_find("FreeSurfer 8.2.0 per subject")
        """
        query: str = urllib.parse.urlencode({"name_exact": name})
        found: list[JsonRow] = [
            row for row in self.results_all(f"pipelines/search/?{query}") if row.text("name") == name
        ]
        return PipelineRef(id=found[0].integer("id"), name=name) if found else None

    def pipings_list(self, pipelineId: int) -> list[PipingRef]:
        """The pipeline's pipings.

        Example:
            client.pipings_list(3)
        """
        return [
            PipingRef(
                id=row.integer("id"), title=row.text("title"), previousId=row.integerOrNone("previous_id")
            )
            for row in self.results_all(f"pipelines/{pipelineId}/pipings/")
        ]

    def params_list(self, pipelineId: int) -> list[PipingParam]:
        """Every piping parameter of the pipeline, with the pipeline's defaults.

        Example:
            client.params_list(3)
        """
        return [
            PipingParam(
                pipingId=row.integer("plugin_piping_id"),
                name=row.text("param_name"),
                value=row.value("value"),
            )
            for row in self.results_all(f"pipelines/{pipelineId}/parameters/")
        ]

    def plugins_find(self, name: str) -> list[PluginRef]:
        """Every version of the plugin with exactly this name.

        Example:
            client.plugins_find("pl-freesurfer")
        """
        query: str = urllib.parse.urlencode({"name_exact": name})
        return [
            PluginRef(id=row.integer("id"), name=name, version=row.text("version"))
            for row in self.results_all(f"plugins/search/?{query}")
            if row.text("name") == name
        ]

    def pluginParams_list(self, pluginId: int) -> list[PluginParameter]:
        """The plugin's parameters.

        Example:
            client.pluginParams_list(9)
        """
        return [
            PluginParameter(name=row.text("name"), type=row.text("type"))
            for row in self.results_all(f"plugins/{pluginId}/parameters/")
        ]

    def children_find(self, previousId: int, title: str) -> list[InstanceRef]:
        """The instances directly under ``previousId`` whose title is exactly ``title``.

        CUBE's ``title`` filter matches substrings, case-insensitively, so the exact
        comparison is made here.

        Example:
            client.children_find(4985, "sub-01")
        """
        query: str = urllib.parse.urlencode({"previous_id": previousId, "title": title})
        return [
            instance
            for instance in map(instance_read, self.results_all(f"plugins/instances/search/?{query}"))
            if instance.title == title and instance.previousId == previousId
        ]

    def workflow_get(self, workflowId: int) -> WorkflowRef:
        """One workflow.

        Example:
            client.workflow_get(12)
        """
        row: JsonRow = self.request_send("GET", f"pipelines/workflows/{workflowId}/")
        return WorkflowRef(
            id=row.integer("id"), pipelineId=row.integer("pipeline_id"), title=row.text("title")
        )

    def workflow_post(
        self, pipelineId: int, previousId: int, title: str, nodes: list[NodeInfo]
    ) -> WorkflowRef:
        """Create a workflow of the pipeline under ``previousId``.

        Args:
            pipelineId: the pipeline.
            previousId: the instance the workflow's root attaches to.
            title: the workflow's title.
            nodes: per-piping titles and parameter values; pipings left out take
                the pipeline's defaults.

        Returns:
            The workflow created.

        Example:
            client.workflow_post(3, 4985, "sub-01", [NodeInfo(7, "sub-01")])
        """
        body: JsonRow = JsonRow(
            {
                "previous_plugin_inst_id": previousId,
                "title": title,
                "nodes_info": json.dumps([node.toDict() for node in nodes]),
            }
        )
        row: JsonRow = self.request_send("POST", f"pipelines/{pipelineId}/workflows/", body)
        return WorkflowRef(
            id=row.integer("id"), pipelineId=row.integerOrNone("pipeline_id") or pipelineId, title=title
        )

    def workflowInstances_list(self, workflowId: int) -> list[InstanceRef]:
        """The plugin instances of a workflow.

        Example:
            client.workflowInstances_list(12)
        """
        return [
            instance_read(row)
            for row in self.results_all(f"pipelines/workflows/{workflowId}/plugininstances/")
        ]

    def instance_post(
        self, pluginId: int, previousId: int, title: str, settings: list[ParamSetting]
    ) -> InstanceRef:
        """Create an instance of the plugin under ``previousId``.

        Args:
            pluginId: the plugin.
            previousId: the instance it attaches to.
            title: its title.
            settings: its parameter values; parameters left out take the plugin's defaults.

        Returns:
            The instance created.

        Example:
            client.instance_post(9, 4985, "sub-01", [ParamSetting("", "dirFilter", "re:^sub\\\\-01$")])
        """
        fields: dict[str, object] = {setting.name: setting.value for setting in settings}
        fields.update({"previous_id": previousId, "title": title})
        return instance_read(self.request_send("POST", f"plugins/{pluginId}/instances/", JsonRow(fields)))
