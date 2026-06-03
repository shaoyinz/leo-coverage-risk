"""Shared test fixtures and helpers for the LEO pipeline test suite.

The A1 data-discovery tools are async MCP handlers that return the MCP envelope
``{"content": [{"type": "text", "text": <json|error|stub>}]}``. ``run_tool`` is the
single choke point that drives a handler synchronously and parses that envelope, so the
tests stay plain (no async pytest plugin needed).
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import pytest

from leo_pipeline.config import Discovery, Ingestion


def run_tool(sdk_tool: Any, args: dict[str, Any]) -> tuple[dict[str, Any], Any]:
    """Invoke an SdkMcpTool handler synchronously.

    Returns ``(envelope, parsed)`` where ``parsed`` is the JSON-decoded payload for a
    normal result, or the raw text string for ``error:``/``[stub]`` responses (which are
    not JSON). Callers inspect ``envelope.get("is_error")`` to branch on the error path.
    """
    envelope = asyncio.run(sdk_tool.handler(args))
    text = envelope["content"][0]["text"]
    try:
        parsed: Any = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        parsed = text
    return envelope, parsed


@pytest.fixture
def make_stac_item():
    """Factory for a minimal stand-in for a pystac Item.

    ``_summarize_stac_item`` only touches ``.id``, ``.properties`` (a dict) and
    ``.assets`` (a dict of objects exposing ``.href``), so a SimpleNamespace suffices.
    """

    def _make(
        item_id: str = "item-0",
        properties: dict[str, Any] | None = None,
        asset_hrefs: dict[str, str] | None = None,
    ) -> SimpleNamespace:
        assets = {k: SimpleNamespace(href=v) for k, v in (asset_hrefs or {}).items()}
        return SimpleNamespace(id=item_id, properties=properties or {}, assets=assets)

    return _make


@pytest.fixture
def redirect_manifest(tmp_path, monkeypatch):
    """Point write_data_manifest at a throwaway path so the real manifest is never touched.

    ``leo_pipeline.tools`` binds ``DISCOVERY`` at import; swap it for a fresh ``Discovery``
    whose ``manifest_path`` lives under tmp. Returns that path for assertions.
    """
    import leo_pipeline.tools as tools

    manifest_path = tmp_path / "data_manifest.json"
    monkeypatch.setattr(tools, "DISCOVERY", Discovery(manifest_path=manifest_path))
    return manifest_path


@pytest.fixture
def redirect_ingestion(tmp_path, monkeypatch):
    """Point the A2 surface cache at a throwaway dir so the real cache is never touched.

    Mirrors ``redirect_manifest``: ``leo_pipeline.tools`` binds ``INGESTION`` at import, so
    swap it for a fresh ``Ingestion`` whose ``cache_dir`` lives under tmp. Returns that dir.
    """
    import leo_pipeline.tools as tools

    cache_dir = tmp_path / "surfaces"
    monkeypatch.setattr(tools, "INGESTION", Ingestion(cache_dir=cache_dir))
    return cache_dir


@pytest.fixture
def make_read():
    """Factory for a synthetic ``_read_window_reprojected`` result (no rasterio, no network).

    Returns the same dict shape the real helper does — a small constant array on a valid
    metric transform — so ``fetch_aligned_surface`` can be exercised end-to-end (fuse,
    write, cache) without touching a COG. Pass ``nodata`` cells via ``holes``.
    """
    import numpy as np
    from rasterio.transform import from_bounds

    def _make(value: float, *, shape=(4, 4), nodata: float = -9999.0, holes=()):
        arr = np.full(shape, float(value), dtype="float32")
        for (r, c) in holes:
            arr[r, c] = nodata
        return {
            "array": arr,
            "transform": from_bounds(0, 0, shape[1] * 10, shape[0] * 10, shape[1], shape[0]),
            "crs": "EPSG:32617",
            "nodata": nodata,
            "gsd_m": 10.0,
            "shape": list(shape),
        }

    return _make
