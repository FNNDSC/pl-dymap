"""Speaking to CUBE: the few REST calls a plan needs, over plain JSON, with the job's token.

CUBE answers ``Accept: application/json`` with paginated ``count``, ``next`` and
``results``; the client follows ``next`` until it runs out. The token is CUBE's own,
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
from typing import Any, Protocol

from dymap.models import CubeError, InstanceRef, PipelineRef, PipingParam, PipingRef, WorkflowRef

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

    def children_find(self, previousId: int, title: str) -> list[InstanceRef]:
        """The instances directly under ``previousId`` whose title is exactly ``title``."""
        ...

    def workflow_get(self, workflowId: int) -> WorkflowRef:
        """One workflow."""
        ...

    def workflow_post(
        self, pipelineId: int, previousId: int, title: str, nodesInfo: list[dict[str, Any]]
    ) -> WorkflowRef:
        """Create a workflow of the pipeline under ``previousId``."""
        ...

    def workflowInstances_list(self, workflowId: int) -> list[InstanceRef]:
        """The plugin instances of a workflow."""
        ...


def intOrNone(value: Any) -> int | None:
    """An integer field that CUBE may leave null.

    Example:
        >>> intOrNone(None), intOrNone("7")
        (None, 7)
    """
    return None if value is None else int(value)


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

    def request_send(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        """One request, its JSON answer decoded.

        Args:
            method: ``GET`` or ``POST``.
            path: relative to the API root, or an absolute URL as CUBE's ``next`` gives.
            body: the JSON body of a POST.

        Returns:
            The decoded answer.

        Raises:
            CubeError: an HTTP error, with CUBE's message, or no response at all.

        Example:
            client.request_send("GET", "pipelines/search/?name_exact=x")
        """
        target: str = path if path.startswith(("http://", "https://")) else self.url + path.lstrip("/")
        data: bytes | None = json.dumps(body).encode() if body is not None else None
        request: urllib.request.Request = urllib.request.Request(target, data=data, method=method)
        request.add_header("Accept", "application/json")
        request.add_header("Authorization", f"Token {self._token}")
        if data is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
                return json.loads(response.read() or b"null")
        except urllib.error.HTTPError as err:
            detail: str = err.read().decode(errors="replace")[:500]
            raise CubeError(err.code, detail) from None
        except (urllib.error.URLError, TimeoutError, OSError) as err:
            raise CubeError(0, f"no response from {target}: {err}") from None

    def results_all(self, path: str) -> list[dict[str, Any]]:
        """Every result of a paginated listing, following ``next``.

        Example:
            client.results_all("pipelines/3/pipings/")
        """
        separator: str = "&" if "?" in path else "?"
        page: Any = self.request_send("GET", f"{path}{separator}limit={PAGE_LIMIT}")
        results: list[dict[str, Any]] = list(page.get("results", []))
        while page.get("next"):
            page = self.request_send("GET", page["next"])
            results.extend(page.get("results", []))
        return results

    def pipeline_find(self, name: str) -> PipelineRef | None:
        """The pipeline with exactly this name, or ``None``.

        Example:
            client.pipeline_find("FreeSurfer 8.2.0 per subject")
        """
        query: str = urllib.parse.urlencode({"name_exact": name})
        found: list[dict[str, Any]] = [
            result for result in self.results_all(f"pipelines/search/?{query}") if result.get("name") == name
        ]
        return PipelineRef(id=int(found[0]["id"]), name=found[0]["name"]) if found else None

    def pipings_list(self, pipelineId: int) -> list[PipingRef]:
        """The pipeline's pipings.

        Example:
            client.pipings_list(3)
        """
        return [
            PipingRef(
                id=int(row["id"]), title=row.get("title", ""), previousId=intOrNone(row.get("previous_id"))
            )
            for row in self.results_all(f"pipelines/{pipelineId}/pipings/")
        ]

    def params_list(self, pipelineId: int) -> list[PipingParam]:
        """Every piping parameter of the pipeline, with the pipeline's defaults.

        Example:
            client.params_list(3)
        """
        return [
            PipingParam(pipingId=int(row["plugin_piping_id"]), name=row["param_name"], value=row.get("value"))
            for row in self.results_all(f"pipelines/{pipelineId}/parameters/")
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
            InstanceRef(
                id=int(row["id"]),
                title=row["title"],
                previousId=intOrNone(row.get("previous_id")),
                workflowId=intOrNone(row.get("workflow_id")),
            )
            for row in self.results_all(f"plugins/instances/search/?{query}")
            if row.get("title") == title and intOrNone(row.get("previous_id")) == previousId
        ]

    def workflow_get(self, workflowId: int) -> WorkflowRef:
        """One workflow.

        Example:
            client.workflow_get(12)
        """
        row: Any = self.request_send("GET", f"pipelines/workflows/{workflowId}/")
        return WorkflowRef(id=int(row["id"]), pipelineId=int(row["pipeline_id"]), title=row.get("title", ""))

    def workflow_post(
        self, pipelineId: int, previousId: int, title: str, nodesInfo: list[dict[str, Any]]
    ) -> WorkflowRef:
        """Create a workflow of the pipeline under ``previousId``.

        Args:
            pipelineId: the pipeline.
            previousId: the instance the workflow's root attaches to.
            title: the workflow's title.
            nodesInfo: per-piping titles and parameter values; pipings left out take
                the pipeline's defaults.

        Returns:
            The workflow created.

        Example:
            client.workflow_post(3, 4985, "sub-01", [{"piping_id": 7, "title": "sub-01"}])
        """
        row: Any = self.request_send(
            "POST",
            f"pipelines/{pipelineId}/workflows/",
            {"previous_plugin_inst_id": previousId, "title": title, "nodes_info": json.dumps(nodesInfo)},
        )
        return WorkflowRef(id=int(row["id"]), pipelineId=int(row.get("pipeline_id", pipelineId)), title=title)

    def workflowInstances_list(self, workflowId: int) -> list[InstanceRef]:
        """The plugin instances of a workflow.

        Example:
            client.workflowInstances_list(12)
        """
        return [
            InstanceRef(
                id=int(row["id"]),
                title=row.get("title", ""),
                previousId=intOrNone(row.get("previous_id")),
                workflowId=workflowId,
            )
            for row in self.results_all(f"pipelines/workflows/{workflowId}/plugininstances/")
        ]
