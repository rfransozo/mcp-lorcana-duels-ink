"""Shared test fixtures.

Everything here runs offline. `httpx.MockTransport` stands in for the Duels.ink
REST API and a small in-process server stands in for the game WebSocket, so the
whole suite is deterministic and needs no network.
"""

import json
import os
import pathlib
import sys
from typing import Any, Callable, Optional

import httpx
import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# A stray cookie in the developer's environment would otherwise change what the
# auth-dependent tests see.
os.environ.pop("DUELS_SESSION_COOKIE", None)

from src.cards import CardCatalog  # noqa: E402
from src.client import DuelsClient  # noqa: E402
from tests.fixtures import cards as card_fixtures  # noqa: E402
from tests.fixtures import decks as deck_fixtures  # noqa: E402


# -----------------------------------------------------------------
# REST double
# -----------------------------------------------------------------
class RecordingRouter:
    """Routes requests to canned responses and records what was asked for.

    Tests assert on `calls` to check things like "the catalog only fetched the
    one set it needed".
    """

    def __init__(self) -> None:
        self.calls: list[httpx.Request] = []
        self.routes: dict[str, Callable[[httpx.Request], httpx.Response]] = {}
        self._install_defaults()

    def on(self, path: str, handler: Callable[[httpx.Request], httpx.Response]) -> None:
        self.routes[path] = handler

    def json_on(self, path: str, payload: Any, status: int = 200) -> None:
        self.on(path, lambda _req: httpx.Response(status, json=payload))

    @property
    def paths(self) -> list[str]:
        return [r.url.path for r in self.calls]

    def count(self, path: str) -> int:
        return sum(1 for r in self.calls if r.url.path == path)

    def _install_defaults(self) -> None:
        self.json_on("/api/version", {"buildId": "test-build"})
        self.json_on("/api/auth/get-session", None)
        self.json_on("/api/decks", deck_fixtures.MY_DECKS)
        self.json_on("/api/decks/public", deck_fixtures.PUBLIC_DECKS)
        self.json_on("/api/settings/public", {"settings": {"maintenance_mode": False}})
        self.json_on(
            "/api/account/active-games", {"games": [], "activeDraftPod": None, "table": None}
        )
        self.on("/api/cards", self._cards)
        self.on(f"/api/decks/{deck_fixtures.DECK_ID}", lambda _r: httpx.Response(
            200, json=deck_fixtures.detail()))
        self.on(f"/api/decks/{deck_fixtures.PUBLIC_DECK_ID}", lambda _r: httpx.Response(
            200, json=deck_fixtures.detail(deck_fixtures.PUBLIC_DECK_ID)))

    @staticmethod
    def _cards(request: httpx.Request) -> httpx.Response:
        params = request.url.params
        limit = int(params.get("limit", 100))
        offset = int(params.get("offset", 0))
        pool = card_fixtures.CARDS
        if "set" in params:
            pool = card_fixtures.cards_in_set(int(params["set"]))
        if "q" in params:
            needle = str(params["q"]).lower()
            pool = [
                c
                for c in pool
                if needle in c["fullName"].lower() or needle in c.get("rulesText", "").lower()
            ]
        return httpx.Response(200, json=card_fixtures.page(pool, limit, offset))

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        handler = self.routes.get(request.url.path)
        if handler is None:
            # Mirrors the site: unknown /api/ paths fall through to the SPA's
            # index.html, which is exactly the case the client must detect.
            return httpx.Response(200, text="<!DOCTYPE html><html><body>not found</body></html>")
        return handler(request)


@pytest.fixture
def router() -> RecordingRouter:
    return RecordingRouter()


@pytest.fixture
def make_client(router: RecordingRouter):
    """Build a DuelsClient wired to the mock transport."""
    created: list[DuelsClient] = []

    def _make(cookie: Optional[str] = None) -> DuelsClient:
        client = DuelsClient(cookie=cookie, transport=httpx.MockTransport(router.handle))
        created.append(client)
        return client

    yield _make


@pytest.fixture
async def client(make_client) -> DuelsClient:
    c = make_client()
    yield c
    await c.aclose()


@pytest.fixture
async def authed_client(make_client) -> DuelsClient:
    c = make_client(cookie="test-session-token")
    yield c
    await c.aclose()


@pytest.fixture
async def catalog(client: DuelsClient) -> CardCatalog:
    return CardCatalog(client)


# -----------------------------------------------------------------
# MCP server double
# -----------------------------------------------------------------
@pytest.fixture
async def mcp_client(monkeypatch, router: RecordingRouter):
    """A FastMCP in-memory Client whose lifespan builds the mocked DuelsClient.

    This exercises the real tools, the real lifespan and the real schemas -
    only the network is replaced.
    """
    from fastmcp import Client

    import src.app as app_module
    from src.app import mcp
    from src.tools import (  # noqa: F401  - importing registers the tools
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

    real_cls = app_module.DuelsClient

    def factory(*args: Any, **kwargs: Any) -> DuelsClient:
        kwargs.setdefault("transport", httpx.MockTransport(router.handle))
        return real_cls(*args, **kwargs)

    monkeypatch.setattr(app_module, "DuelsClient", factory)

    async with Client(mcp) as c:
        yield c


async def call_text(mcp_client, name: str, **kwargs: Any) -> str:
    """Call a tool and return its text output."""
    result = await mcp_client.call_tool(name, kwargs)
    return result.content[0].text


async def call_json(mcp_client, name: str, **kwargs: Any) -> Any:
    """Call a tool in JSON mode and parse the result.

    Returns {"__error__": text} when the tool reported an error, so tests can
    assert on failures without try/except.
    """
    kwargs["response_format"] = "json"
    text = await call_text(mcp_client, name, **kwargs)
    if text.startswith("Error:") or text.startswith("No "):
        return {"__error__": text}
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"__raw__": text}
