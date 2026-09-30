"""The first-party GSC MCP server must complete a stdio handshake on the pinned ``mcp``.

It is fork-only, so an upstream merge that bumps the ``mcp`` pin never touches it. The
v2026.9.24 merge moved the pin to 2.0.0, which removed ``mcp.server.fastmcp``; the server
exited at import, Hermes logged ``Connection closed``, and cron preflight blocked the
BigLobster Content Updater because its ``gsc`` toolset resolved to zero tools. Nothing
failed in CI, because nothing started the server. This does.
"""

import asyncio
import sys
from pathlib import Path

import pytest

pytest.importorskip("mcp")

from mcp import ClientSession, StdioServerParameters  # noqa: E402
from mcp.client.stdio import stdio_client  # noqa: E402

_SERVER = Path(__file__).resolve().parents[2] / "optional-mcps" / "gsc" / "server.py"


async def _list_tool_names() -> set:
    # No credential: the server reads it lazily on the first tool call, so the
    # handshake and tool listing must work without one.
    params = StdioServerParameters(command=sys.executable, args=[str(_SERVER)], env={})
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            return {tool.name for tool in (await session.list_tools()).tools}


def test_gsc_server_lists_its_tools_over_stdio():
    names = asyncio.run(asyncio.wait_for(_list_tool_names(), timeout=60))
    assert {"gsc_list_sites", "gsc_search_analytics", "gsc_inspect_url"} <= names
