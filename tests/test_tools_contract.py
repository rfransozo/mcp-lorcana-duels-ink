"""Contract tests for the exposed tool surface.

These enforce the quality bar the server was built to (Anthropic's MCP tool
guidance) so it cannot silently erode: naming, annotations, parameter
descriptions and docstrings that say when *not* to use a tool.
"""

import ast
import pathlib

import pytest

SRC_TOOLS_DIR = pathlib.Path(__file__).resolve().parent.parent / "src" / "tools"

DESTRUCTIVE_TOOLS = {
    "duels_delete_deck",
    "duels_concede",
    "duels_send_game_action",
    # Ejects a real person from a game in progress.
    "duels_removal_vote",
}

EXPECTED_RESOURCES = {
    "duels://cards/{definition_id}",
    "duels://game/{game_id}/state",
    "duels://game/{game_id}/log",
    "duels://deck/{deck_id}",
}


@pytest.fixture(scope="module")
async def tools():
    """The exposed tool list.

    Built once per module: these tests only introspect the surface, so they
    need neither the mocked transport nor a fresh server per test.
    """
    from fastmcp import Client

    from src.app import mcp
    from src.tools import (  # noqa: F401  - importing registers the tools
        analysis,
        cards,
        decks,
        history,
        identity,
        ingame,
        modes,
        play,
        resources,
        social,
    )

    async with Client(mcp) as client:
        return await client.list_tools()


def source_docstrings() -> dict[str, str]:
    """Docstrings as written in the source.

    FastMCP strips the Args/Returns/Examples sections out of the description it
    exposes (their content moves into the schema), so those sections have to be
    checked against the source rather than the wire format.
    """
    found: dict[str, str] = {}
    for path in SRC_TOOLS_DIR.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef) and node.name.startswith("duels_"):
                found[node.name] = ast.get_docstring(node) or ""
    return found


class TestNaming:
    async def test_every_tool_is_namespaced(self, tools):
        """Without a service prefix these collide with other MCP servers -
        search_cards and get_deck are not distinctive names."""
        unprefixed = [t.name for t in tools if not t.name.startswith("duels_")]
        assert unprefixed == []

    async def test_names_are_snake_case(self, tools):
        bad = [t.name for t in tools if t.name != t.name.lower() or "-" in t.name]
        assert bad == []

    async def test_names_are_unique(self, tools):
        names = [t.name for t in tools]
        assert len(names) == len(set(names))

    async def test_the_surface_is_not_empty(self, tools):
        assert len(tools) >= 43


class TestAnnotations:
    async def test_all_four_hints_are_declared(self, tools):
        missing = {}
        for tool in tools:
            a = tool.annotations
            absent = [
                name
                for name, value in (
                    ("readOnlyHint", a.read_only_hint),
                    ("destructiveHint", a.destructive_hint),
                    ("idempotentHint", a.idempotent_hint),
                    ("openWorldHint", a.open_world_hint),
                )
                if value is None
            ]
            if absent:
                missing[tool.name] = absent
        assert missing == {}

    async def test_every_tool_has_a_human_title(self, tools):
        assert [t.name for t in tools if not t.annotations.title] == []

    async def test_destructive_flag_marks_exactly_the_risky_tools(self, tools):
        """A client decides whether to confirm with the user based on this."""
        flagged = {t.name for t in tools if t.annotations.destructive_hint}
        assert flagged == DESTRUCTIVE_TOOLS

    async def test_read_only_tools_are_never_destructive(self, tools):
        contradictory = [
            t.name
            for t in tools
            if t.annotations.read_only_hint and t.annotations.destructive_hint
        ]
        assert contradictory == []

    async def test_everything_reaches_the_outside_world(self, tools):
        """Every tool ultimately talks to duels.ink."""
        assert [t.name for t in tools if not t.annotations.open_world_hint] == []


class TestInputSchemas:
    async def test_every_parameter_is_documented(self, tools):
        undocumented = {}
        for tool in tools:
            props = (tool.input_schema or {}).get("properties", {})
            bare = [name for name, spec in props.items() if not spec.get("description")]
            if bare:
                undocumented[tool.name] = bare
        assert undocumented == {}

    async def test_schemas_are_flat(self, tools):
        """A single wrapper object would force callers into {"params": {...}}."""
        wrapped = [
            t.name
            for t in tools
            if list((t.input_schema or {}).get("properties", {})) == ["params"]
        ]
        assert wrapped == []

    async def test_unknown_arguments_are_rejected(self, tools):
        permissive = [
            t.name
            for t in tools
            if (t.input_schema or {}).get("additionalProperties") is not False
        ]
        assert permissive == []

    async def test_data_tools_offer_both_output_formats(self, tools):
        """Markdown for a human, JSON for a program."""
        missing = [
            t.name
            for t in tools
            if "response_format" not in (t.input_schema or {}).get("properties", {})
        ]
        assert missing == []


class TestDocstrings:
    async def test_descriptions_are_substantial(self, tools):
        thin = [t.name for t in tools if len(t.description or "") < 200]
        assert thin == []

    def test_every_tool_documents_its_return_shape(self):
        docs = source_docstrings()
        assert docs, "no tool docstrings found in src/tools"
        missing = [name for name, doc in docs.items() if "Returns:" not in doc]
        assert missing == []

    async def test_every_tool_says_when_not_to_use_it(self, tools):
        """Steering away from the wrong tool matters as much as describing the
        right one; without it an agent picks by name alone."""
        missing = [
            t.name
            for t in tools
            if not any(
                marker in (t.description or "")
                for marker in ("Do NOT", "Note that", "Note there", "rather than")
            )
        ]
        assert missing == []

    async def test_descriptions_open_with_what_is_returned(self, tools):
        """Not "This tool ..." - lead with the result."""
        bad = [t.name for t in tools if (t.description or "").startswith("This tool")]
        assert bad == []

    def test_arguments_section_is_present(self):
        docs = source_docstrings()
        missing = [name for name, doc in docs.items() if "Args:" not in doc]
        assert missing == []

    async def test_every_tool_shows_a_worked_example(self, tools):
        """Asserted on the EXPOSED description, not the source docstring.

        FastMCP hands the docstring to griffe, and an indented block under
        `Examples:` is parsed as a section and dropped from the description the
        agent actually receives. A source-only check passes while every example
        silently fails to ship - which is exactly what happened the first time
        this was fixed after the MCPize quality review.
        """
        missing = [t.name for t in tools if "Examples:" not in (t.description or "")]
        assert missing == []

    async def test_examples_show_an_actual_invocation(self, tools):
        """An Examples block that only restates the description is not one."""
        thin = [
            t.name
            for t in tools
            if "->" not in (t.description or "").split("Examples:", 1)[-1]
        ]
        assert thin == []

    async def test_examples_sit_before_the_args_section(self, tools):
        """Placement is what keeps them in the description: after Args they are
        swallowed by the section parser."""
        for t in tools:
            body = t.description or ""
            assert "Args:" not in body, f"{t.name}: Args leaked into the description"

    def test_the_state_json_keys_are_documented(self):
        """The Returns section is the only place an agent learns what the JSON
        carries, so the keys added to it are named there."""
        returns = source_docstrings()["duels_get_game_state"].split("Returns:", 1)[1]
        for key in ("card_text", "waiting_on", "prompt_target_cards",
                    '"discard": [{"instance_id","card","definition_id"}]'):
            assert key in returns, key

    def test_source_docstrings_cover_every_exposed_tool(self):
        """Guards the AST lookup itself: a renamed module would otherwise make
        the two checks above silently pass on an empty set."""
        assert len(source_docstrings()) >= 40


class TestResources:
    async def test_expected_resource_templates_are_registered(self, mcp_client):
        templates = await mcp_client.list_resource_templates()
        assert {t.uri_template for t in templates} == EXPECTED_RESOURCES

    async def test_resources_are_described(self, mcp_client):
        templates = await mcp_client.list_resource_templates()
        assert [t.uri_template for t in templates if not t.description] == []
