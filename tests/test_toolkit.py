"""Unit tests for the cross-cutting tool helpers."""

import pytest

from src.client import DuelsError
from src.toolkit import progress, tool_errors


class TestToolErrors:
    async def test_passes_through_success(self):
        @tool_errors
        async def ok() -> str:
            return "fine"

        assert await ok() == "fine"

    async def test_duels_error_becomes_actionable_text(self):
        @tool_errors
        async def boom() -> str:
            raise DuelsError("session cookie expired")

        assert await boom() == "Error: session cookie expired"

    async def test_value_error_is_labelled_as_a_bad_argument(self):
        @tool_errors
        async def boom() -> str:
            raise ValueError("card_type must be one of: character, action")

        out = await boom()
        assert out.startswith("Error: invalid argument")
        assert "character" in out

    async def test_unexpected_errors_do_not_leak_internals(self):
        """Security: an internal message must never reach the client verbatim."""

        @tool_errors
        async def boom() -> str:
            raise KeyError("/secret/path/to/internal/thing")

        out = await boom()
        assert "secret" not in out
        assert "KeyError" in out
        assert out.startswith("Error:")

    async def test_keeps_the_wrapped_function_name(self):
        @tool_errors
        async def duels_something() -> str:
            return ""

        assert duels_something.__name__ == "duels_something"


class TestProgress:
    async def test_forwards_progress_as_keywords(self):
        """Regression: the message was once passed positionally as `total`,
        which made FastMCP reject it and took the whole tool down."""
        seen = {}

        class Ctx:
            async def report_progress(self, progress, total=None, message=None):
                seen.update(progress=progress, total=total, message=message)

        await progress(Ctx(), 0.5, "halfway")
        assert seen == {"progress": 0.5, "total": None, "message": "halfway"}

    async def test_a_failing_client_never_fails_the_tool(self):
        class Ctx:
            async def report_progress(self, **_kwargs):
                raise RuntimeError("client does not support progress")

        await progress(Ctx(), 0.1, "x")  # must not raise
