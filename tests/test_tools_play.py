"""Getting into a game: bot games, tables and the matchmaking queue.

The queue tools are the ones with teeth - they enter a real account into a
real queue against real people - so what is pinned here is that they only act
when they should, and that joining starts the heartbeat without which nobody
is ever paired.
"""

import httpx
import pytest

from tests.conftest import call_json, call_text
from tests.fixtures import decks as deck_fixtures
from tests.fixtures import games
from tests.test_gamews import FakeGameServer

pytestmark = pytest.mark.anyio

DECK_ID = deck_fixtures.DECK_ID


@pytest.fixture
async def game_socket(router):
    """A fake game socket on the route a freshly created game would use."""
    async with FakeGameServer() as server:
        router.json_on(
            f"/api/game/{games.GAME_ID}/ws-token", {"token": "t", "wsUrl": server.url}
        )
        yield server


class TestStartingABotGame:
    async def test_one_of_deck_id_or_cards_is_required(self, mcp_client):
        text = await call_text(mcp_client, "duels_start_bot_game")
        assert text.startswith("Error:")
        assert "deck_id" in text and "deck_card_ids" in text

    async def test_a_deck_with_no_cards_is_refused_by_id(self, router, mcp_client):
        """A 60-card list is the whole payload; an empty one would create an
        unplayable game."""
        router.on(
            f"/api/decks/{DECK_ID}",
            lambda _r: httpx.Response(200, json={"deck": {"id": DECK_ID, "cardIds": []}}),
        )
        text = await call_text(mcp_client, "duels_start_bot_game", deck_id=DECK_ID)
        assert text.startswith("Error:") and "no cardIds" in text

    async def test_raw_card_ids_skip_the_deck_lookup(self, router, mcp_client, game_socket):
        router.json_on(
            "/api/game/create-bot-game", {"gameId": games.GAME_ID, "sessionId": "s-1"}
        )
        text = await call_text(
            mcp_client, "duels_start_bot_game", deck_card_ids=["10-71"] * 60
        )
        assert "Bot game started" in text
        assert not any(c.url.path == f"/api/decks/{DECK_ID}" for c in router.calls)

    async def test_the_opening_state_comes_back_with_it(
        self, router, mcp_client, game_socket
    ):
        """Otherwise the first thing the caller must do is ask again."""
        router.json_on(
            "/api/game/create-bot-game", {"gameId": games.GAME_ID, "sessionId": "s-1"}
        )
        text = await call_text(mcp_client, "duels_start_bot_game", deck_id=DECK_ID)
        assert "Your hand" in text or "Legal moves" in text

    async def test_a_game_already_running_is_handed_back_not_raised(
        self, router, mcp_client, game_socket
    ):
        """Duels.ink answers 409 bot_game_in_progress, and the id of the game
        it means is only in the active-games list."""
        router.json_on(
            "/api/game/create-bot-game",
            {"error": "bot_game_in_progress"},
            status=409,
        )
        router.json_on("/api/account/active-games", {"games": [{"id": games.GAME_ID}]})
        text = await call_text(mcp_client, "duels_start_bot_game", deck_id=DECK_ID)
        assert not text.startswith("Error:")
        assert "already in progress" in text and games.GAME_ID in text

    async def test_a_409_with_no_running_game_still_fails(self, router, mcp_client):
        """Nothing to hand back means the error was real."""
        router.json_on(
            "/api/game/create-bot-game", {"error": "bot_game_in_progress"}, status=409
        )
        router.json_on("/api/account/active-games", {"games": []})
        text = await call_text(mcp_client, "duels_start_bot_game", deck_id=DECK_ID)
        assert text.startswith("Error:")

    async def test_a_create_that_returns_no_game_id_is_an_error(self, router, mcp_client):
        router.json_on("/api/game/create-bot-game", {"sessionId": "s-1"})
        text = await call_text(mcp_client, "duels_start_bot_game", deck_id=DECK_ID)
        assert text.startswith("Error:") and "did not return a game id" in text


class TestActiveGames:
    async def test_needs_a_cookie(self, mcp_client):
        text = await call_text(mcp_client, "duels_list_active_games")
        assert text.startswith("Error:")

    async def test_nothing_running_says_so(self, authed_mcp_client):
        text = await call_text(authed_mcp_client, "duels_list_active_games")
        assert "No games in progress" in text

    async def test_games_tables_and_pods_are_all_surfaced(self, router, authed_mcp_client):
        router.json_on(
            "/api/account/active-games",
            {
                "games": [{"id": games.GAME_ID, "status": "playing"}],
                "table": {"tableId": "table-0001"},
                "activeDraftPod": {"podId": "p-1"},
            },
        )
        text = await call_text(authed_mcp_client, "duels_list_active_games")
        assert games.GAME_ID in text and "playing" in text
        assert "Open table" in text and "table-0001" in text
        assert "Draft pod" in text and "p-1" in text


class TestTables:
    async def test_creating_a_table_returns_the_invite_link(self, router, authed_mcp_client):
        router.json_on(
            "/api/table/create", {"tableId": "table-0001", "url": "https://duels.ink/t/t-1"}
        )
        router.json_on("/api/table/table-0001/view", {"view": {"status": "assembling"}})
        text = await call_text(authed_mcp_client, "duels_create_table")
        assert "table-0001" in text and "https://duels.ink/t/t-1" in text

    async def test_creation_settings_travel_at_the_top_level(
        self, router, authed_mcp_client
    ):
        """A nested `config` object is accepted at creation and silently dropped."""
        router.json_on("/api/table/create", {"tableId": "table-0001", "url": "u"})
        router.json_on("/api/table/table-0001/view", {"view": {"status": "assembling"}})
        await call_text(
            authed_mcp_client,
            "duels_create_table",
            game_format="Coconut",
            timer_preset="standard",
            public=True,
            lore_to_win=25,
        )
        body = next(
            c for c in router.calls if c.url.path == "/api/table/create"
        ).content
        for expected in (b'"gameFormat":"Coconut"', b'"timerPreset":"standard"',
                         b'"visibility":"public"', b'"loreToWin":25'):
            assert expected in body
        assert b'"config"' not in body

    async def test_a_short_format_name_is_spelled_out(self, router, authed_mcp_client):
        """'Core' on its own is refused by the API; CoreConstructed is the name."""
        router.json_on("/api/table/create", {"tableId": "table-0001", "url": "u"})
        router.json_on("/api/table/table-0001/view", {"view": {"status": "assembling"}})
        await call_text(authed_mcp_client, "duels_create_table", game_format="Core")
        body = next(c for c in router.calls if c.url.path == "/api/table/create").content
        assert b'"gameFormat":"CoreConstructed"' in body

    async def test_an_unknown_format_names_the_real_ones(self, authed_mcp_client):
        text = await call_text(
            authed_mcp_client, "duels_create_table", game_format="Pauper"
        )
        assert text.startswith("Error:")
        assert "Coconut" in text and "CoreConstructed" in text

    async def test_seats_are_opened_after_creation(self, router, authed_mcp_client):
        """Creation ignores a seat count, so it has to be set as a settings action."""
        router.json_on("/api/table/create", {"tableId": "table-0001", "url": "u"})
        router.json_on("/api/table/table-0001/view", {"view": {"status": "assembling"}})
        router.json_on("/api/table/table-0001/action", {"success": True})
        await call_text(authed_mcp_client, "duels_create_table", seats=4)
        body = next(
            c for c in router.calls if c.url.path == "/api/table/table-0001/action"
        ).content
        assert b'"openSeats":4' in body

    async def test_an_unknown_action_lists_the_real_ones(self, authed_mcp_client):
        text = await call_text(
            authed_mcp_client, "duels_configure_table", table_id="table-0001", action="explode"
        )
        assert text.startswith("Error:")
        for action in ("set_deck", "ready", "add_bot", "start", "cancel"):
            assert action in text

    async def test_set_deck_without_a_deck_is_refused(self, authed_mcp_client):
        text = await call_text(
            authed_mcp_client, "duels_configure_table", table_id="table-0001", action="set_deck"
        )
        assert text.startswith("Error:") and "needs deck_id" in text

    async def test_set_deck_sends_the_whole_card_list(self, router, authed_mcp_client):
        """The table needs the contents, not just the id."""
        router.json_on("/api/table/table-0001/action", {"ok": True})
        router.json_on("/api/table/table-0001/view", {"status": "waiting"})
        await call_text(
            authed_mcp_client,
            "duels_configure_table",
            table_id="table-0001",
            action="set_deck",
            deck_id=DECK_ID,
        )
        body = next(c for c in router.calls if c.url.path == "/api/table/table-0001/action").content
        assert b"SET_DECK" in body and b"deckCardIds" in body

    async def test_starting_a_table_surfaces_the_new_game_id(
        self, router, authed_mcp_client
    ):
        """That id is the only way to reach the game that just began."""
        router.json_on("/api/table/table-0001/action", {"ok": True})
        router.json_on("/api/table/table-0001/view", {"status": "playing", "gameId": games.GAME_ID})
        text = await call_text(
            authed_mcp_client, "duels_configure_table", table_id="table-0001", action="start"
        )
        assert "Game started" in text and games.GAME_ID in text

    async def test_a_table_view_reads_without_a_cookie(self, router, mcp_client):
        """An invited player may not be signed in yet."""
        router.json_on("/api/table/table-0001/view", {"status": "waiting", "seats": []})
        text = await call_text(mcp_client, "duels_get_table", table_id="table-0001")
        assert not text.startswith("Error:") and "waiting" in text


class TestMatchmaking:
    """These put a real account into a queue against real people."""

    async def test_joining_needs_a_cookie(self, mcp_client):
        text = await call_text(
            mcp_client,
            "duels_matchmaking",
            action="join",
            queue_id="quick-play",
            deck_id=DECK_ID,
        )
        assert text.startswith("Error:")

    async def test_joining_starts_the_heartbeat(self, router, authed_mcp_client):
        """Without it the entry looks healthy and is never matched - the whole
        reason src/matchmaking.py exists."""
        router.json_on(
            "/api/matchmaking/join", {"status": "queued", "position": 1, "estimatedWait": 30}
        )
        text = await call_text(
            authed_mcp_client,
            "duels_matchmaking",
            action="join",
            queue_id="quick-play",
            deck_id=DECK_ID,
        )
        assert "heartbeat is running" in text
        assert "Do NOT re-join to poll" in text

    async def test_a_queue_that_matches_instantly_reports_the_game(
        self, router, authed_mcp_client
    ):
        router.json_on("/api/matchmaking/join", {"gameId": games.GAME_ID})
        text = await call_text(
            authed_mcp_client,
            "duels_matchmaking",
            action="join",
            queue_id="quick-play",
            deck_id=DECK_ID,
        )
        assert "Match found" in text and games.GAME_ID in text

    async def test_waiting_without_queueing_says_so_instead_of_blocking(
        self, authed_mcp_client
    ):
        text = await call_text(
            authed_mcp_client, "duels_matchmaking", action="wait", timeout_seconds=5
        )
        assert text.startswith("Error:") and "not in a queue" in text

    async def test_a_timeout_is_reported_as_still_queued_not_as_failure(
        self, router, authed_mcp_client, monkeypatch
    ):
        """Timing out is normal; treating it as an error would make the caller
        leave a perfectly good queue entry."""
        monkeypatch.setattr("src.matchmaking.HEARTBEAT_SECONDS", 0.01)
        monkeypatch.setattr("src.matchmaking.POLL_SECONDS", 0.01)
        router.json_on("/api/matchmaking/join", {"status": "queued", "position": 1})
        router.json_on("/api/matchmaking/heartbeat", {"ok": True})
        await call_text(
            authed_mcp_client,
            "duels_matchmaking",
            action="join",
            queue_id="quick-play",
            deck_id=DECK_ID,
        )
        text = await call_text(
            authed_mcp_client, "duels_matchmaking", action="wait", timeout_seconds=5
        )
        assert not text.startswith("Error:")
        assert "Still queued" in text and "not an error" in text

    async def test_the_coin_toss_can_be_answered_on_arrival(
        self, router, authed_mcp_client, monkeypatch
    ):
        """The toss is the shortest clock in the game - ten to fifteen seconds.

        Reading the state first and answering second spends all of it, and the
        server picks for you. That is how a ranked game began on somebody
        else's choice.
        """
        monkeypatch.setattr("src.matchmaking.HEARTBEAT_SECONDS", 0.01)
        monkeypatch.setattr("src.matchmaking.POLL_SECONDS", 0.01)
        router.json_on("/api/matchmaking/join", {"status": "queued"})
        router.json_on("/api/matchmaking/heartbeat", {"ok": True})
        router.json_on(
            "/api/account/active-games", {"games": [{"id": games.GAME_ID}]}
        )
        router.json_on("/api/matchmaking/leave", {"success": True})
        async with FakeGameServer(games.coin_toss()) as server:
            router.json_on(
                f"/api/game/{games.GAME_ID}/ws-token",
                {"token": "t", "wsUrl": server.url},
            )
            await call_text(
                authed_mcp_client,
                "duels_matchmaking",
                action="join",
                queue_id="quick-play",
                deck_id=DECK_ID,
            )
            text = await call_text(
                authed_mcp_client,
                "duels_matchmaking",
                action="wait",
                timeout_seconds=5,
                on_match_choose="play",
            )
            sent = [m["action"] for m in server.received if m.get("action")]
        assert any(a["type"] == "CHOOSE_STARTING_PLAYER" for a in sent)
        assert sent[-1]["choice"] == "play"
        assert "you go first" in text

    async def test_a_nonsense_choice_is_refused_before_queueing(
        self, authed_mcp_client
    ):
        text = await call_text(
            authed_mcp_client,
            "duels_matchmaking",
            action="wait",
            on_match_choose="coin",
        )
        assert text.startswith("Error:") and "'play' or 'draw'" in text

    async def test_without_the_choice_nothing_is_sent(
        self, router, authed_mcp_client, monkeypatch
    ):
        monkeypatch.setattr("src.matchmaking.HEARTBEAT_SECONDS", 0.01)
        monkeypatch.setattr("src.matchmaking.POLL_SECONDS", 0.01)
        router.json_on("/api/matchmaking/join", {"status": "queued"})
        router.json_on("/api/matchmaking/heartbeat", {"ok": True})
        router.json_on(
            "/api/account/active-games", {"games": [{"id": games.GAME_ID}]}
        )
        router.json_on("/api/matchmaking/leave", {"success": True})
        async with FakeGameServer(games.coin_toss()) as server:
            router.json_on(
                f"/api/game/{games.GAME_ID}/ws-token",
                {"token": "t", "wsUrl": server.url},
            )
            await call_text(
                authed_mcp_client,
                "duels_matchmaking",
                action="join",
                queue_id="quick-play",
                deck_id=DECK_ID,
            )
            await call_text(
                authed_mcp_client, "duels_matchmaking", action="wait", timeout_seconds=5
            )
            sent = [m["action"] for m in server.received if m.get("action")]
        assert not any(a["type"] == "CHOOSE_STARTING_PLAYER" for a in sent)

    async def test_leaving_stops_the_heartbeat_too(
        self, router, authed_mcp_client, monkeypatch
    ):
        """A heartbeat still running after leaving would re-queue nothing but
        would keep beating forever."""
        monkeypatch.setattr("src.matchmaking.HEARTBEAT_SECONDS", 0.01)
        router.json_on("/api/matchmaking/join", {"status": "queued"})
        router.json_on("/api/matchmaking/heartbeat", {"ok": True})
        router.json_on("/api/matchmaking/leave", {"success": True})
        await call_text(
            authed_mcp_client,
            "duels_matchmaking",
            action="join",
            queue_id="quick-play",
            deck_id=DECK_ID,
        )
        text = await call_text(authed_mcp_client, "duels_matchmaking", action="leave")
        assert "Left the matchmaking queue" in text
        after = len(router.calls)
        text = await call_text(
            authed_mcp_client, "duels_matchmaking", action="wait", timeout_seconds=5
        )
        assert "not in a queue" in text
        assert len(router.calls) == after, "no further beats after leaving"

    async def test_leaving_needs_a_cookie(self, mcp_client):
        text = await call_text(mcp_client, "duels_matchmaking", action="leave")
        assert text.startswith("Error:")
