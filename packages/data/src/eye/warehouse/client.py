"""Bounded HTTP client for the typed warehouse operations."""

from typing import Any

import httpx
from pydantic import BaseModel, TypeAdapter, ValidationError

from eye.contracts import Edge, GraphView, MetricDef
from eye.warehouse import operations as ops
from eye.warehouse.domain import DataParams, DataResponse

_PARTITIONS = TypeAdapter(ops.PartitionWindow)
_DOMAIN = TypeAdapter(DataResponse)


class DataUnavailable(RuntimeError):
    """The service is unavailable or returned an invalid response."""


class DataClient:
    def __init__(
        self, url: str, *, transport: httpx.BaseTransport | None = None
    ) -> None:
        self.http = httpx.Client(
            base_url=url,
            timeout=httpx.Timeout(60.0, connect=3.0, pool=3.0),
            transport=transport,
            trust_env=False,
        )

    def close(self) -> None:
        self.http.close()

    def _request[R](
        self, output: TypeAdapter[R], method: str, path: str, **kwargs: Any
    ) -> R:
        try:
            response = self.http.request(method, path, **kwargs)
            if response.status_code in (400, 404, 422):
                try:
                    body = response.json()
                except ValueError as exc:
                    raise DataUnavailable(
                        f"Invalid warehouse response: {path}"
                    ) from exc
                detail = body.get("detail") if isinstance(body, dict) else None
                raise ValueError(
                    detail if isinstance(detail, str) else "invalid warehouse request"
                )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise DataUnavailable(f"Warehouse request failed: {path}") from exc
        try:
            return output.validate_json(response.content)
        except ValidationError as exc:
            raise DataUnavailable(f"Invalid warehouse response: {path}") from exc

    def read[T: BaseModel, R](self, operation: ops.Operation[T, R], args: T) -> R:
        return self._request(
            operation.output,
            "POST",
            f"/v1/{operation.path}",
            json=args.model_dump(mode="json"),
        )

    def partitions(self) -> ops.PartitionWindow:
        return self._request(_PARTITIONS, "GET", "/v1/partitions")

    def metrics(self) -> list[MetricDef]:
        return self._request(ops.METRICS, "GET", "/v1/metrics")

    def domain(self, path: str, params: DataParams) -> DataResponse:
        return self._request(
            _DOMAIN,
            "GET",
            f"/data/{path}",
            params=params.model_dump(mode="json", exclude_none=True),
        )


class RemoteGraph:
    def __init__(self, client: DataClient) -> None:
        self.client = client

    def view(self, node: str | None = None, depth: int | None = None) -> GraphView:
        return self.client.read(ops.GRAPH, ops.GraphInput(node=node, depth=depth))

    def metrics(self) -> list[MetricDef]:
        return self.client.metrics()

    def in_edges(self, node: str) -> list[Edge]:
        return [edge for edge in self.view().edges if edge.dst == node]

    def out_edges(self, node: str) -> list[Edge]:
        return [edge for edge in self.view().edges if edge.src == node]

    def cycles_through(self, node: str) -> list[list[str]]:
        return self.client.read(ops.CYCLES, ops.NodeInput(node=node))

    def path_exists(self, src: str, dst: str) -> bool:
        return self.client.read(ops.PATH, ops.PathInput(src=src, dst=dst))
