"""Unit tests for the Duels.ink REST client."""

import httpx
import pytest

from src.client import COOKIE_NAMES, DuelsClient, DuelsError


class TestCookieHeader:
    def test_bare_token_is_sent_under_both_known_names(self):
        """The cookie is HttpOnly, so a user may paste only the value. We send
        both better-auth names and let the server ignore the wrong one."""
        header = DuelsClient._build_cookie_header("abc123")
        for name in COOKIE_NAMES:
            assert f"{name}=abc123" in header

    def test_full_cookie_string_is_passed_through_untouched(self):
        raw = "__Secure-better-auth.session_token=abc123"
        assert DuelsClient._build_cookie_header(raw) == raw

    def test_multi_cookie_string_is_passed_through(self):
        raw = "a=1; b=2"
        assert DuelsClient._build_cookie_header(raw) == raw

    def test_empty_means_anonymous(self):
        assert DuelsClient._build_cookie_header("") == ""

    def test_authenticated_reflects_configuration(self, make_client):
        assert make_client().authenticated is False
        assert make_client(cookie="tok").authenticated is True


class TestRequireAuth:
    def test_anonymous_fails_fast_with_instructions(self, client):
        with pytest.raises(DuelsError) as exc:
            client.require_auth("Listing your decks")
        message = str(exc.value)
        assert "Listing your decks" in message
        assert "DevTools" in message

    def test_configured_cookie_passes(self, authed_client):
        authed_client.require_auth("anything")  # must not raise


class TestErrorMapping:
    @pytest.mark.parametrize(
        ("status", "expected"),
        [
            (401, "DevTools"),
            (403, "permission"),
            (404, "still exists"),
            (400, "documented schema"),
            (429, "Wait a few seconds"),
            (500, "on their side"),
        ],
    )
    async def test_status_maps_to_an_actionable_message(
        self, router, make_client, status, expected
    ):
        router.json_on("/api/thing", {"error": "boom"}, status=status)
        c = make_client()
        with pytest.raises(DuelsError) as exc:
            await c.get("/api/thing")
        assert expected in str(exc.value)
        await c.aclose()

    async def test_server_detail_is_surfaced(self, router, make_client):
        router.json_on("/api/game/create-bot-game", {"error": "player_deck_required"}, status=400)
        c = make_client()
        with pytest.raises(DuelsError) as exc:
            await c.post("/api/game/create-bot-game", {})
        assert "player_deck_required" in str(exc.value)
        await c.aclose()

    async def test_html_body_is_reported_as_the_api_having_changed(self, client):
        """Unknown /api/ paths fall through to the SPA's index.html. That is the
        signal the private API moved, and must not surface as a JSON error."""
        with pytest.raises(DuelsError) as exc:
            await client.get("/api/route-that-does-not-exist")
        assert "may have changed" in str(exc.value)

    async def test_network_failure_is_phrased_as_connectivity(self, router):
        def explode(_request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("no route to host")

        c = DuelsClient(transport=httpx.MockTransport(explode))
        with pytest.raises(DuelsError) as exc:
            await c.get("/api/version")
        assert "Check network connectivity" in str(exc.value)
        await c.aclose()


class TestAuthHeaders:
    async def test_cookie_is_sent_when_configured(self, router, authed_client):
        await authed_client.get("/api/decks")
        assert "cookie" in router.calls[-1].headers

    async def test_no_cookie_on_anonymous_client(self, router, client):
        await client.get("/api/decks/public", authed=False)
        assert "cookie" not in router.calls[-1].headers

    async def test_authed_false_suppresses_the_cookie(self, router, authed_client):
        await authed_client.get("/api/cards", authed=False)
        assert "cookie" not in router.calls[-1].headers


class TestSession:
    async def test_anonymous_session_is_none(self, client):
        assert await client.get_session() is None

    async def test_signed_in_session_is_returned(self, router, authed_client):
        router.json_on(
            "/api/auth/get-session",
            {"session": {"token": "t", "expiresAt": "2026-10-20"}, "user": {"name": "Fransozo"}},
        )
        session = await authed_client.get_session()
        assert session["user"]["name"] == "Fransozo"

    async def test_build_id_is_read(self, client):
        assert await client.build_id() == "test-build"

    async def test_build_id_returns_none_instead_of_raising(self, router, make_client):
        router.json_on("/api/version", {"error": "nope"}, status=500)
        c = make_client()
        assert await c.build_id() is None
        await c.aclose()


class TestWsToken:
    async def test_composes_the_url_from_the_returned_host(self, router, client):
        """The WS host is sharded (ws, ws0, ws1, ws3, ws4 all observed), so it
        must come from the response and never be hardcoded."""
        router.json_on("/api/game/g1/ws-token", {"token": "jwt123", "wsUrl": "wss://ws4.duels.ink"})
        url = await client.ws_token("game", "g1")
        assert url == "wss://ws4.duels.ink/game/g1?token=jwt123"

    async def test_trailing_slash_in_host_is_handled(self, router, client):
        router.json_on("/api/game/g1/ws-token", {"token": "t", "wsUrl": "wss://ws0.duels.ink/"})
        assert await client.ws_token("game", "g1") == "wss://ws0.duels.ink/game/g1?token=t"

    async def test_anonymous_games_pass_the_session_id(self, router, client):
        """An anonymous bot game's only credential is the sessionId from
        create-bot-game; without it this endpoint answers 401."""
        router.json_on("/api/game/g1/ws-token", {"token": "t", "wsUrl": "wss://ws3.duels.ink"})
        await client.ws_token("game", "g1", session_id="sess-1")
        assert router.calls[-1].url.params["sessionId"] == "sess-1"

    async def test_table_kind_is_supported(self, router, client):
        router.json_on("/api/table/t1/ws-token", {"token": "t", "wsUrl": "wss://ws.duels.ink"})
        assert await client.ws_token("table", "t1") == "wss://ws.duels.ink/table/t1?token=t"

    async def test_unknown_kind_is_rejected(self, client):
        with pytest.raises(DuelsError, match="game.*table"):
            await client.ws_token("lobby", "x")

    async def test_a_response_without_a_token_is_rejected(self, router, client):
        router.json_on("/api/game/g1/ws-token", {"wsUrl": "wss://ws.duels.ink"})
        with pytest.raises(DuelsError, match="usable WebSocket token"):
            await client.ws_token("game", "g1")

    async def test_a_token_without_a_host_falls_back_to_the_default(self, router, client):
        """Tables answer with a bare token; games name their shard.

        Insisting on `wsUrl` meant a lobby socket could never be opened, which
        is why our own seat read as disconnected to everyone at the table.
        """
        router.json_on("/api/table/t1/ws-token", {"token": "t"})
        url = await client.ws_token("table", "t1")
        assert url == "wss://ws.duels.ink/table/t1?token=t"

    async def test_a_named_host_still_wins(self, router, client):
        router.json_on("/api/game/g1/ws-token", {"token": "t", "wsUrl": "wss://ws4.duels.ink"})
        assert await client.ws_token("game", "g1") == "wss://ws4.duels.ink/game/g1?token=t"


class TestVerbs:
    async def test_empty_body_decodes_to_empty_dict(self, router, client):
        router.on("/api/thing", lambda _r: httpx.Response(204))
        assert await client.get("/api/thing") == {}

    async def test_post_sets_the_content_type(self, router, client):
        router.json_on("/api/thing", {"ok": True})
        await client.post("/api/thing", {"a": 1})
        assert router.calls[-1].headers["content-type"] == "application/json"

    async def test_delete_is_routed(self, router, client):
        router.json_on("/api/decks/d1", {"success": True})
        assert await client.delete("/api/decks/d1") == {"success": True}


class _RecordingStream(httpx.AsyncByteStream):
    """A response body that remembers whether httpx closed it."""

    def __init__(self, *chunks: bytes) -> None:
        self.chunks = chunks
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk

    async def aclose(self) -> None:
        self.closed = True


class TestEventStream:
    """Matchmaking is pushed over SSE, not the game WebSocket, so this is the
    only channel that says an opponent has been found."""

    SSE = (
        b":heartbeat\n\n"
        b'data: {"type":"init","data":{"inQueue":true}}\n\n'
        b"event: ping\n"
        b'data: {"type":"match_found","data":{"matchId":"m-1"}}\n\n'
    )

    def _serve(self, router, body: _RecordingStream, status: int = 200):
        router.on(
            "/api/matchmaking/events",
            lambda _req: httpx.Response(status, stream=body, headers={"content-type": "text/event-stream"}),
        )
        return body

    async def test_data_lines_are_decoded_and_the_rest_ignored(self, router, client):
        self._serve(router, _RecordingStream(self.SSE))
        async with client.stream_events("/api/matchmaking/events") as events:
            seen = [event async for event in events]
        assert [e["type"] for e in seen] == ["init", "match_found"]
        assert seen[1]["data"]["matchId"] == "m-1"

    async def test_a_malformed_frame_does_not_end_the_wait(self, router, client):
        """One bad frame must not cost the pairing that comes after it."""
        body = _RecordingStream(b"data: not json\n\n" + self.SSE)
        self._serve(router, body)
        async with client.stream_events("/api/matchmaking/events") as events:
            seen = [event async for event in events]
        assert [e["type"] for e in seen] == ["init", "match_found"]

    async def test_leaving_early_closes_the_connection(self, router, client):
        """The caller returns the moment a game starts. Returning out of an
        `async for` does not close the generator it was iterating, so the
        stream is opened by the context manager rather than inside it."""
        body = self._serve(router, _RecordingStream(self.SSE))
        async with client.stream_events("/api/matchmaking/events") as events:
            async for _event in events:
                break
        assert body.closed, "the SSE socket was left open"

    async def test_a_refused_stream_is_an_actionable_error(self, router, client):
        self._serve(router, _RecordingStream(b""), status=401)
        with pytest.raises(DuelsError) as exc:
            async with client.stream_events("/api/matchmaking/events"):
                pass
        assert "DevTools" in str(exc.value)

    async def test_the_session_cookie_is_sent(self, router, authed_client):
        """Sent as a cookie, not a bearer token - the stream 401s otherwise."""
        self._serve(router, _RecordingStream(self.SSE))
        async with authed_client.stream_events("/api/matchmaking/events") as events:
            _ = [e async for e in events]
        assert "session_token" in router.calls[-1].headers["cookie"]
