"""In-game paths that the main tool suite never reaches.

The log, the wait, conceding and the odd shapes of prompt: each one is a place
where being wrong costs a turn rather than an error, because the game simply
sits there.
"""

import pytest

from tests.conftest import call_json, call_text
from tests.fixtures import games, prompts
from tests.test_gamews import FakeGameServer

pytestmark = pytest.mark.anyio


async def with_game(router, mcp_client, game=None, *, logs=None, tool=None, **kwargs):
    """Run one tool against a fake socket serving `game`."""
    async with FakeGameServer(game, logs=logs) as server:
        router.json_on(
            f"/api/game/{games.GAME_ID}/ws-token", {"token": "t", "wsUrl": server.url}
        )
        text = await call_text(mcp_client, tool, game_id=games.GAME_ID, **kwargs)
        return server, text


async def answer(router, mcp_client, prompt, **kwargs):
    return await with_game(
        router,
        mcp_client,
        games.with_prompt(prompt),
        tool="duels_respond_to_prompt",
        **kwargs,
    )


class TestTheGameLog:
    LOGS = [
        {"id": "a", "turnNumber": 1, "player": 1, "type": "TURN_START",
         "message": "Opponent turn begins"},
        {"id": "b", "turnNumber": 2, "player": 2, "type": "CARD_PLAYED",
         "message": "You played {card:0}",
         "cardRefs": [{"id": "10-103", "name": "Mushu - Stealthy Dragon"}]},
        {"id": "c", "turnNumber": 2, "player": 2, "type": "CARD_QUEST",
         "message": "You quested with {card:0}", "cardRefs": [{"id": "10-71"}]},
    ]

    async def _log(self, router, mcp_client, logs, **kwargs):
        _server, text = await with_game(
            router, mcp_client, logs=logs, tool="duels_get_game_log", **kwargs
        )
        return text

    async def test_an_empty_log_says_so(self, router, mcp_client):
        assert "No log entries yet" in await self._log(router, mcp_client, [])

    async def test_entries_are_grouped_by_turn(self, router, mcp_client):
        text = await self._log(router, mcp_client, self.LOGS)
        assert "**Turn 1**" in text and "**Turn 2**" in text
        assert text.index("**Turn 1**") < text.index("**Turn 2**")

    async def test_each_line_says_whose_it_was(self, router, mcp_client):
        text = await self._log(router, mcp_client, self.LOGS)
        assert "[P1] Opponent turn begins" in text
        assert "[P2] You played" in text

    async def test_card_placeholders_are_substituted(self, router, mcp_client):
        """A log full of {card:0} is unreadable, and the names are in a
        parallel list."""
        text = await self._log(router, mcp_client, self.LOGS)
        assert "You played Mushu - Stealthy Dragon" in text
        assert "{card:0}" not in text

    async def test_a_ref_without_a_name_falls_back_to_its_id(self, router, mcp_client):
        text = await self._log(router, mcp_client, self.LOGS)
        assert "You quested with 10-71" in text

    async def test_limit_takes_the_most_recent(self, router, mcp_client):
        text = await self._log(router, mcp_client, self.LOGS, limit=1)
        assert "quested" in text and "Opponent turn begins" not in text


class TestWaitingForYourTurn:
    async def _wait(self, router, mcp_client, game):
        _server, text = await with_game(
            router, mcp_client, game, tool="duels_wait_for_my_turn", timeout_seconds=5
        )
        return text

    async def test_your_own_turn_returns_at_once(self, router, mcp_client):
        assert "Still not your turn" not in await self._wait(
            router, mcp_client, games.playing()
        )

    async def test_a_finished_game_stops_waiting(self, router, mcp_client):
        """Blocking for a turn that will never come is the worst outcome
        available."""
        assert "Still not your turn" not in await self._wait(
            router, mcp_client, games.finished()
        )

    async def test_a_pending_prompt_counts_as_your_turn(self, router, mcp_client):
        assert "Still not your turn" not in await self._wait(
            router, mcp_client, games.with_prompt(prompts.BOOLEAN)
        )

    async def test_the_opponents_coin_toss_is_not_your_turn(self, router, mcp_client):
        """Being in the coin-toss phase is not the same as holding the choice,
        and returning here would make the caller spin.

        A timeout is not a failure either, so the board still comes back.
        """
        text = await self._wait(router, mcp_client, games.coin_toss(mine=False))
        assert "Still not your turn" in text
        assert games.GAME_ID in text

    async def test_the_opponents_mulligan_is_not_your_turn(self, router, mcp_client):
        assert "Still not your turn" in await self._wait(
            router, mcp_client, games.mulligan(my_turn_to_act=False)
        )


class TestChoosingWhichPrompt:
    async def test_an_explicit_id_is_honoured(self, router, mcp_client):
        game = games.with_prompt(prompts.BOOLEAN)
        game["pendingPrompts"] = [prompts.SELECT_TARGET, prompts.BOOLEAN]
        server, _text = await with_game(
            router,
            mcp_client,
            game,
            tool="duels_respond_to_prompt",
            prompt_id=prompts.BOOLEAN["id"],
            choice="yes",
        )
        assert server.received[-1]["action"]["response"]["promptId"] == prompts.BOOLEAN["id"]

    async def test_the_first_pending_prompt_is_the_default(self, router, mcp_client):
        game = games.with_prompt(prompts.BOOLEAN)
        game["pendingPrompts"] = [prompts.BOOLEAN, prompts.SELECT_TARGET]
        server, _text = await with_game(
            router, mcp_client, game, tool="duels_respond_to_prompt", choice="yes"
        )
        assert server.received[-1]["action"]["response"]["promptId"] == prompts.BOOLEAN["id"]

    async def test_an_unknown_id_lists_the_ones_that_exist(self, router, mcp_client):
        _server, text = await answer(
            router, mcp_client, prompts.BOOLEAN, prompt_id="not-a-prompt", choice="yes"
        )
        assert text.startswith("Error:") and prompts.BOOLEAN["id"] in text


class TestNumericAndUnseenPrompts:
    async def test_a_numeric_prompt_takes_a_number(self, router, mcp_client):
        server, text = await answer(
            router, mcp_client, prompts.SELECT_NUMERIC, numeric_value=2
        )
        assert not text.startswith("Error:")
        assert server.received[-1]["action"]["response"]["numericValue"] == 2

    async def test_a_numeric_prompt_without_one_shows_the_range(self, router, mcp_client):
        _server, text = await answer(router, mcp_client, prompts.SELECT_NUMERIC)
        assert text.startswith("Error:") and "numeric_value" in text
        assert "'min': 1" in text and "'max': 3" in text

    async def test_an_unseen_prompt_type_passes_the_answer_through(
        self, router, mcp_client
    ):
        """Refusing a shape we have not met would wedge the game on a prompt
        nobody can clear."""
        odd = {**prompts.SELECT_NUMERIC, "type": "choose_a_direction"}
        server, text = await answer(
            router, mcp_client, odd, numeric_value=7, choice="yes"
        )
        assert not text.startswith("Error:")
        response = server.received[-1]["action"]["response"]
        assert response["type"] == "choose_a_direction"
        assert response["numericValue"] == 7 and response["value"] is True

    async def test_the_passthrough_carries_targets_and_cards_too(
        self, router, mcp_client
    ):
        odd = {**prompts.SELECT_TARGET, "type": "choose_a_direction"}
        server, _text = await answer(
            router,
            mcp_client,
            odd,
            target_instance_ids=[games.FIELD_ELSA],
            selected_card_ids=[games.HAND_SONG],
        )
        response = server.received[-1]["action"]["response"]
        assert response["targetInstanceIds"] == [games.FIELD_ELSA]
        assert response["cardInstanceIds"] == [games.HAND_SONG]

    async def test_an_options_summary_falls_back_to_the_whole_prompt(
        self, router, mcp_client
    ):
        """When a prompt carries none of the fields we know about, showing the
        rest beats showing nothing."""
        bare = {
            "id": "prompt-bare-1",
            "player": 2,
            "type": "select_numeric",
            "required": True,
            "somethingNew": [1, 2, 3],
        }
        _server, text = await answer(router, mcp_client, bare)
        assert "somethingNew" in text


class TestConceding:
    async def test_a_bot_game_is_abandoned_not_conceded(self, router, mcp_client):
        """The two have different action names and the wrong one is refused."""
        server, text = await with_game(
            router, mcp_client, games.playing(isBotGame=True), tool="duels_concede"
        )
        assert server.received[-1]["action"]["type"] == "ABANDON_BOT_GAME"
        assert "ABANDON_BOT_GAME" in text

    async def test_a_game_against_a_person_is_conceded(self, router, mcp_client):
        server, text = await with_game(
            router, mcp_client, games.playing(isBotGame=False), tool="duels_concede"
        )
        assert server.received[-1]["action"]["type"] == "CONCEDE"
        assert "CONCEDE" in text

    async def test_conceding_releases_the_socket(self, router, authed_mcp_client):
        """A socket left open keeps the account looking present in a game it
        has walked away from."""
        await with_game(
            router, authed_mcp_client, games.playing(isBotGame=True), tool="duels_concede"
        )
        data = await call_json(authed_mcp_client, "duels_list_active_games")
        assert games.GAME_ID not in (data.get("connected_game_ids") or [])


class TestTheEscapeHatch:
    async def test_an_action_is_forwarded_uppercased(self, router, mcp_client):
        server, _text = await with_game(
            router,
            mcp_client,
            tool="duels_send_game_action",
            action_type="some_new_action",
            payload={"foo": "bar"},
        )
        sent = server.received[-1]["action"]
        assert sent["type"] == "SOME_NEW_ACTION" and sent["foo"] == "bar"

    async def test_leaving_through_it_releases_the_socket_too(
        self, router, authed_mcp_client
    ):
        await with_game(
            router,
            authed_mcp_client,
            games.playing(isBotGame=True),
            tool="duels_send_game_action",
            action_type="ABANDON_BOT_GAME",
        )
        data = await call_json(authed_mcp_client, "duels_list_active_games")
        assert games.GAME_ID not in (data.get("connected_game_ids") or [])
