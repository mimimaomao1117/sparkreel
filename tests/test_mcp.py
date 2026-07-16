"""Smoke tests for the MCP server — the agent-native clipping interface.

Skips cleanly if the optional `mcp` dependency isn't installed. Verifies the
server object builds and registers exactly the tools an agent (Claude Code /
Cursor) is expected to call, without starting the stdio transport.
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest

pytest.importorskip("mcp", reason="install sparkreel[mcp] to test the MCP server")

from sparkreel import mcp_server as m

EXPECTED = {"create_clips", "list_jobs", "get_job", "make_sample"}


def test_server_object_and_entrypoint():
    assert m.mcp is not None
    assert callable(m.run)
    for name in EXPECTED:
        assert callable(getattr(m, name, None)), f"tool function missing: {name}"


def test_registered_tools_match_expected():
    tools = asyncio.run(m.mcp.list_tools())
    names = {t.name for t in tools}
    assert EXPECTED <= names, f"missing tools: {EXPECTED - names}"


def test_create_clips_signature_is_agent_friendly():
    import inspect
    sig = inspect.signature(m.create_clips)
    # the params an agent needs to drive clipping end-to-end
    for p in ("video", "platforms", "max_clips", "emphasis", "captions", "broll"):
        assert p in sig.parameters, f"create_clips missing param: {p}"
    assert sig.parameters["video"].default is inspect.Parameter.empty   # video is required
