"""Discoverability: first-line form rule, overview coverage, instructions, no titles."""

import re

from marvin_mcp import server

ABBREV = re.compile(r"\b(e\.g|i\.e|etc|incl|vs)\.$")


async def _tools():
    return await server.mcp.get_tools()


async def test_first_line_is_whole_sentence_max_75():
    tools = await _tools()
    bad = []
    for name, tool in tools.items():
        first = (tool.description or "").split("\n")[0].strip()
        if not (1 < len(first) <= 75 and first.endswith(".") and not ABBREV.search(first)):
            bad.append((name, len(first), first))
    assert not bad, bad


async def test_list_capabilities_covers_every_tool():
    tools = await _tools()
    listed = {n for _, items in server.CAPABILITIES for n, _, _ in items}
    assert listed == set(tools), {"missing": set(tools) - listed, "extra": listed - set(tools)}
    result = await server.list_capabilities.fn()
    assert result["tool_count"] == len(tools)
    assert result["cannot_via_mcp"]


async def test_instructions_point_to_list_capabilities():
    assert "list_capabilities" in server.INSTRUCTIONS
    assert str(server.mcp.instructions or "").startswith("Amazing Marvin MCP")


async def test_no_tool_carries_title():
    tools = await _tools()
    assert all(t.title is None for t in tools.values()), [n for n, t in tools.items() if t.title]
