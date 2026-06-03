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

from leo_pipeline.config import Discovery


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
