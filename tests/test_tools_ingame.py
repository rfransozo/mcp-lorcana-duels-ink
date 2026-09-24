"""In-game paths that the main tool suite never reaches.

The log, the wait, conceding and the odd shapes of prompt: each one is a place
where being wrong costs a turn rather than an error, because the game simply
sits there.
"""

import json

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

    async def test_your_turn_blocked_on_an_opponent_keeps_waiting(
        self, router, mcp_client
    ):
        """You Have Forgotten Me asks every opponent to discard, and the turn
        waits on them. Returning at once left the caller polling by hand."""
        game = games.playing(opponentHasPendingPrompts=True)
        game["availableActions"]["canEndTurn"] = False
        assert "Still not your turn" in await self._wait(router, mcp_client, game)

    async def test_a_waiting_opponent_does_not_hold_you_when_you_can_act(
        self, router, mcp_client
    ):
        game = games.playing(opponentHasPendingPrompts=True)
        assert "Still not your turn" not in await self._wait(router, mcp_client, game)

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
        # `value`, the key a boolean answers under too. numericValue was a
        # guess the engine refused with "Invalid prompt response".
        response = server.received[-1]["action"]["response"]
        assert response["value"] == 2
        assert "numericValue" not in response

    async def test_a_disabled_answer_is_refused_with_the_one_left(
        self, router, mcp_client
    ):
        """Ursula's Trickery: "discard a card" with an empty hand. The prompt
        says so with yesDisabled; sending yes anyway only earns a refusal."""
        prompt = {**prompts.BOOLEAN, "yesDisabled": True}
        server, text = await answer(router, mcp_client, prompt, choice="yes")
        assert text.startswith("Error:") and "choice='no'" in text
        assert not [m for m in server.received if m.get("action")]

    async def test_the_answer_that_is_left_goes_through(self, router, mcp_client):
        prompt = {**prompts.BOOLEAN, "yesDisabled": True}
        server, text = await answer(router, mcp_client, prompt, choice="no")
        assert not text.startswith("Error:")
        assert server.received[-1]["action"]["response"]["value"] is False

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
        server, text = await answer(router, mcp_client, odd, numeric_value=7)
        assert not text.startswith("Error:")
        response = server.received[-1]["action"]["response"]
        assert response["type"] == "choose_a_direction"
        # Both known shapes answer under `value`, so an unseen one gets it too.
        assert response["value"] == 7

    async def test_an_unseen_prompt_can_take_a_yes_or_no(self, router, mcp_client):
        odd = {**prompts.SELECT_NUMERIC, "type": "choose_a_direction"}
        server, _text = await answer(router, mcp_client, odd, choice="yes")
        assert server.received[-1]["action"]["response"]["value"] is True

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

    async def test_a_modal_prompt_is_answered_by_option_id(self, router, mcp_client):
        """`select_modal` had no branch at all.

        The fallback sent no choice, the engine replied 'Invalid prompt
        response', and the turn sat on the prompt burning the clock. The wire
        key is optionId.
        """
        server, _text = await answer(
            router, mcp_client, prompts.SELECT_MODAL, choice="2"
        )
        response = server.received[-1]["action"]["response"]
        assert response["type"] == "select_modal"
        assert response["optionId"] == "2"

    async def test_a_modal_prompt_takes_the_label_too(self, router, mcp_client):
        """The id is a bare '1' or '2'; the label is the player's name."""
        server, _text = await answer(
            router, mcp_client, prompts.SELECT_MODAL, choice="Supxr"
        )
        assert server.received[-1]["action"]["response"]["optionId"] == "2"

    async def test_a_modal_prompt_refuses_an_option_it_does_not_have(
        self, router, mcp_client
    ):
        """Better a refusal naming the options than a silent wrong answer."""
        _server, text = await answer(
            router, mcp_client, prompts.SELECT_MODAL, choice="nobody"
        )
        assert text.startswith("Error:") and "select_modal" in text

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


class TestPlayingAWholeTurn:
    """Round trips are what the clock is actually spent on.

    Two games were lost on time without a single bad play: a turn is two
    minutes of real time and each separate tool call spends some of it. This
    tool exists to make a turn cost one call instead of four.
    """

    HAND = games.HAND_FLOTSAM
    FIELD = games.FIELD_ELSA

    async def plan(self, router, mcp_client, steps, game=None):
        return await with_game(
            router, mcp_client, game, tool="duels_play_turn", steps=steps
        )

    async def test_the_whole_plan_is_sent_in_order(self, router, mcp_client):
        server, text = await self.plan(
            router,
            mcp_client,
            [
                {"do": "ink", "card": self.HAND},
                {"do": "quest", "card": self.FIELD},
                {"do": "end"},
            ],
        )
        sent = [m["action"]["type"] for m in server.received if m.get("action")]
        assert sent == ["ADD_TO_INK", "QUEST", "END_TURN"]
        assert "Played 3 of 3 steps" in text

    async def test_ending_carries_what_turn_it_meant_to_end(self, router, mcp_client):
        """Otherwise a plan that went stale ends somebody else's turn."""
        server, _text = await self.plan(router, mcp_client, [{"do": "end"}])
        end = server.received[-1]["action"]
        assert end["expectedTurnNumber"] == games.base_game()["turnNumber"]
        assert end["expectedCurrentPlayer"] == games.base_game()["currentPlayer"]

    async def test_a_challenge_becomes_an_attack(self, router, mcp_client):
        server, _text = await self.plan(
            router,
            mcp_client,
            [{"do": "challenge", "card": self.FIELD, "target": games.OPP_PETE}],
        )
        sent = server.received[-1]["action"]
        assert sent["type"] == "ATTACK"
        assert sent["attackerInstanceId"] == self.FIELD
        assert sent["targetInstanceId"] == games.OPP_PETE

    async def test_a_shift_travels_with_the_play(self, router, mcp_client):
        server, _text = await self.plan(
            router,
            mcp_client,
            [{"do": "play", "card": self.HAND, "shift_target": self.FIELD}],
        )
        assert server.received[-1]["action"]["shiftTargetInstanceId"] == self.FIELD

    async def test_a_bad_step_costs_nothing(self, router, mcp_client):
        """Half a turn is worse than none: the board has moved and the plan
        that described it is gone."""
        server, text = await self.plan(
            router,
            mcp_client,
            [{"do": "ink", "card": self.HAND}, {"do": "dance", "card": self.HAND}],
        )
        assert text.startswith("Error:")
        assert "steps[1]" in text and "dance" in text
        assert [m for m in server.received if m.get("action")] == []

    async def test_a_step_missing_its_card_names_the_index(self, router, mcp_client):
        _server, text = await self.plan(router, mcp_client, [{"do": "quest"}])
        assert text.startswith("Error:") and "steps[0]" in text and "card" in text

    async def test_a_challenge_without_a_target_is_refused(self, router, mcp_client):
        _server, text = await self.plan(
            router, mcp_client, [{"do": "challenge", "card": self.FIELD}]
        )
        assert text.startswith("Error:") and "target" in text

    async def test_a_waiting_prompt_stops_the_plan(self, router, mcp_client):
        """The position now needs a decision the plan could not have known
        about, so the rest is handed back rather than guessed at."""
        _server, text = await self.plan(
            router,
            mcp_client,
            [{"do": "quest", "card": self.FIELD}, {"do": "end"}],
            game=games.with_prompt(prompts.BOOLEAN),
        )
        assert "Stopped:" in text and "duels_respond_to_prompt" in text
        assert "Not taken:" in text and "end" in text

    async def test_nothing_is_sent_once_a_prompt_is_waiting(self, router, mcp_client):
        server, _text = await self.plan(
            router,
            mcp_client,
            [{"do": "quest", "card": self.FIELD}],
            game=games.with_prompt(prompts.BOOLEAN),
        )
        assert [m for m in server.received if m.get("action")] == []

    async def test_an_empty_plan_says_what_one_looks_like(self, router, mcp_client):
        _server, text = await self.plan(router, mcp_client, [])
        assert text.startswith("Error:")


class TestARefusalNamesWhoIsAnswering:
    """Live, at a four-player table: a quest refused with "Waiting for opponent
    to respond", and the moves attached said only that the server was "still
    resolving something" - so it went out again with nine seconds left. Now the
    refusal says whose card is asking, and with which ability."""

    STEPS = [{"do": "quest", "card": games.FIELD_ELSA}, {"do": "end"}]

    async def refused(self, router, mcp_client, call=call_text):
        async with FakeGameServer(
            games.waiting_on_an_answer(), accept=False, error="Waiting for opponent to respond"
        ) as server:
            router.json_on(
                f"/api/game/{games.GAME_ID}/ws-token", {"token": "t", "wsUrl": server.url}
            )
            result = await call(
                mcp_client, "duels_play_turn", game_id=games.GAME_ID, steps=self.STEPS
            )
            return server, result

    async def test_the_refusal_says_whose_card_and_which_ability(self, router, mcp_client):
        server, text = await self.refused(router, mcp_client)
        stopped = text.split("**Stopped:**", 1)[1].split("**Not taken:**", 1)[0]
        assert "Waiting for opponent to respond" in stopped
        assert "ENERGY CAPTURE on Ink Amplifier (DRobb's card)" in stopped
        assert "resolving something" not in stopped
        assert [m["action"]["type"] for m in server.received] == ["QUEST"]

    async def test_the_json_says_it_too(self, router, mcp_client):
        _server, payload = await self.refused(router, mcp_client, call=call_json)
        assert "ENERGY CAPTURE on Ink Amplifier (DRobb's card)" in payload["stopped_because"]
        assert payload["waiting_on"][0]["card_owner"] == "DRobb"
        assert payload["remaining"] == self.STEPS


class TestEveryStateToolSendsTheCompactJson:
    """Each tool asks for the compact view at its own call site, so each one is
    checked: the one that forgot would still send the 100,000-character kind."""

    @pytest.mark.parametrize(
        "tool, extra",
        [
            ("duels_get_game_state", {}),
            ("duels_wait_for_my_turn", {"timeout_seconds": 5}),
            ("duels_play_turn", {"steps": [{"do": "quest", "card": games.FIELD_ELSA}]}),
            ("duels_quest", {"card_instance_id": games.FIELD_ELSA}),
        ],
    )
    async def test_text_once_and_discards_by_name(self, router, mcp_client, tool, extra):
        _server, text = await with_game(
            router, mcp_client, games.playing(), tool=tool, response_format="json", **extra
        )
        assert "\n" not in text
        payload = json.loads(text)
        assert payload["card_text"]
        assert all(set(c) == {"instance_id", "card", "definition_id"}
                   for c in payload["me"]["discard"])
        assert all("text" not in c for c in payload["me"]["hand"])


class TestClaimingAStalledGame:
    """A game nobody is playing does not end itself, and waiting does nothing.

    The action names came out of the site's own JavaScript: every one of them
    was invisible to us while the state advertised the capability.
    """

    def timed(self, **view):
        game = games.playing()
        return {**game, "timerView": {"serverTimestamp": 1, **view}}

    async def claim(self, router, mcp_client, game, **kwargs):
        return await with_game(
            router, mcp_client, game, tool="duels_claim_victory", **kwargs
        )

    async def test_auto_takes_a_timeout(self, router, mcp_client):
        server, _text = await self.claim(
            router, mcp_client, self.timed(canDeclareVictory=True)
        )
        assert server.received[-1]["action"]["type"] == "DECLARE_VICTORY"

    async def test_auto_takes_an_absence(self, router, mcp_client):
        server, _text = await self.claim(
            router, mcp_client, self.timed(canClaimAfkVictory=True)
        )
        assert server.received[-1]["action"]["type"] == "CLAIM_AFK_VICTORY"

    async def test_auto_prefers_the_timeout(self, router, mcp_client):
        """Both are offered at once when a clock runs out on an idle player."""
        server, _text = await self.claim(
            router,
            mcp_client,
            self.timed(canDeclareVictory=True, canClaimAfkVictory=True),
        )
        assert server.received[-1]["action"]["type"] == "DECLARE_VICTORY"

    async def test_auto_with_nothing_to_claim_sends_nothing(self, router, mcp_client):
        server, text = await self.claim(
            router, mcp_client, self.timed(myTimeRemainingMs=60000)
        )
        assert text.startswith("Error:") and "Nothing is claimable" in text
        assert [m for m in server.received if m.get("action")] == []

    async def test_auto_points_at_the_ping_when_that_is_all_there_is(
        self, router, mcp_client
    ):
        _server, text = await self.claim(
            router, mcp_client, self.timed(canPingOpponent=True)
        )
        assert text.startswith("Error:") and "kind='ping'" in text

    async def test_a_ping_is_sent_when_asked_for(self, router, mcp_client):
        server, _text = await self.claim(
            router, mcp_client, self.timed(canPingOpponent=True), kind="ping"
        )
        assert server.received[-1]["action"]["type"] == "PING_OPPONENT"

    async def test_answering_a_ping_is_possible(self, router, mcp_client):
        """Ignoring one hands the opponent the claim."""
        server, _text = await self.claim(
            router, mcp_client, self.timed(wasAfkPinged=True), kind="respond"
        )
        assert server.received[-1]["action"]["type"] == "RESPOND_TO_AFK_PING"

    async def test_a_pregame_claim_has_its_own_name(self, router, mcp_client):
        server, _text = await self.claim(
            router, mcp_client, self.timed(canClaimPreGameVictory=True), kind="pregame"
        )
        assert server.received[-1]["action"]["type"] == "CLAIM_PREGAME_VICTORY"

    async def test_an_unknown_kind_lists_the_real_ones(self, router, mcp_client):
        _server, text = await self.claim(
            router, mcp_client, self.timed(), kind="surrender"
        )
        assert text.startswith("Error:")
        for kind in ("timeout", "afk", "pregame", "ping", "respond"):
            assert kind in text


class TestUndo:
    """The payloads came out of the site's bundle.

    `RESPOND_TO_UNDO` carries the answer as a boolean - there is no
    RESPOND_TO_UNDO_YES - and the bare type is the shape this API answers 200
    and ignores. `FREE_UNDO` and `CHOICE_UNDO` look like siblings and are not:
    they are log events, so tools for them would send types nothing handles.
    """

    async def undo(self, router, mcp_client, game=None, **kwargs):
        return await with_game(
            router, mcp_client, game, tool="duels_undo", **kwargs
        )

    async def test_requesting_needs_the_table_to_allow_it(self, router, mcp_client):
        server, text = await self.undo(router, mcp_client)
        assert text.startswith("Error:") and "nothing to take back" in text
        assert [m for m in server.received if m.get("action")] == []

    async def test_requesting_when_allowed(self, router, mcp_client):
        server, _text = await self.undo(
            router, mcp_client, games.playing(canRequestUndo=True)
        )
        assert server.received[-1]["action"]["type"] == "REQUEST_UNDO"

    async def test_accepting_carries_the_boolean(self, router, mcp_client):
        server, _text = await self.undo(router, mcp_client, action="accept")
        sent = server.received[-1]["action"]
        assert sent["type"] == "RESPOND_TO_UNDO" and sent["accept"] is True

    async def test_declining_carries_the_other_boolean(self, router, mcp_client):
        server, _text = await self.undo(router, mcp_client, action="decline")
        sent = server.received[-1]["action"]
        assert sent["type"] == "RESPOND_TO_UNDO" and sent["accept"] is False

    async def test_cancelling_your_own_request(self, router, mcp_client):
        server, _text = await self.undo(router, mcp_client, action="cancel")
        assert server.received[-1]["action"]["type"] == "CANCEL_UNDO"

    async def test_stopping_an_ability_mid_resolution(self, router, mcp_client):
        server, _text = await self.undo(router, mcp_client, action="cancel_ability")
        assert server.received[-1]["action"]["type"] == "CANCEL_ABILITY"

    async def test_rewinding_a_choice_inside_one(self, router, mcp_client):
        server, _text = await self.undo(router, mcp_client, action="rewind_choice")
        assert server.received[-1]["action"]["type"] == "REWIND_ABILITY_CHOICE"

    async def test_an_unknown_action_lists_the_real_ones(self, router, mcp_client):
        _server, text = await self.undo(router, mcp_client, action="rewrite_history")
        assert text.startswith("Error:")
        for name in ("request", "accept", "decline", "cancel_ability"):
            assert name in text


class TestRemovalVote:
    """A table's way of ejecting somebody who walked away."""

    def voting(self):
        return games.coconut_table(
            removalVoteCalled={"targetPlayer": 3, "votesFor": 1, "votesNeeded": 2}
        )

    async def vote(self, router, mcp_client, game=None, **kwargs):
        return await with_game(
            router, mcp_client, game, tool="duels_removal_vote", **kwargs
        )

    async def test_calling_one_names_the_target(self, router, mcp_client):
        server, _text = await self.vote(router, mcp_client, target_player=3)
        sent = server.received[-1]["action"]
        assert sent["type"] == "CALL_REMOVAL_VOTE" and sent["targetPlayer"] == 3

    async def test_calling_without_a_target_says_where_to_find_one(
        self, router, mcp_client
    ):
        server, text = await self.vote(router, mcp_client)
        assert text.startswith("Error:") and "player_number" in text
        assert [m for m in server.received if m.get("action")] == []

    async def test_agreeing_carries_the_boolean(self, router, mcp_client):
        server, _text = await self.vote(
            router, mcp_client, self.voting(), action="accept"
        )
        sent = server.received[-1]["action"]
        assert sent["type"] == "RESPOND_TO_REMOVAL_VOTE" and sent["accept"] is True

    async def test_refusing_carries_the_other(self, router, mcp_client):
        server, _text = await self.vote(
            router, mcp_client, self.voting(), action="decline"
        )
        assert server.received[-1]["action"]["accept"] is False

    async def test_an_eligibility_timer_is_not_a_vote_to_answer(
        self, router, mcp_client
    ):
        """removalVoteCall with availableAt names whoever is acting and when
        a vote against them could open - there is no vote to answer yet."""
        game = games.coconut_table(
            removalVoteCall={"targetPlayer": 3, "targetName": "Flower", "availableAt": 1}
        )
        server, text = await self.vote(router, mcp_client, game, action="decline")
        assert text.startswith("Error:") and "no vote running" in text
        assert [m for m in server.received if m.get("action")] == []

    async def test_answering_a_vote_nobody_called(self, router, mcp_client):
        server, text = await self.vote(router, mcp_client, action="accept")
        assert text.startswith("Error:") and "no vote running" in text
        assert [m for m in server.received if m.get("action")] == []

    async def test_withdrawing_one(self, router, mcp_client):
        server, _text = await self.vote(
            router, mcp_client, self.voting(), action="cancel"
        )
        assert server.received[-1]["action"]["type"] == "CANCEL_REMOVAL_VOTE"


class TestAnsweringAPromptTheWrongWay:
    """The answer that was accepted and threw the choice away."""

    async def test_a_select_card_refuses_target_instance_ids(self, router, mcp_client):
        """minSelect was 0, so the old guard stayed quiet: an empty selection
        went out, four cards went to the bottom of the deck, and the Princess
        they were dug for was gone."""
        prompt = {"id": "p1", "player": 1, "type": "select_card",
                  "cardInstanceIds": ["c1", "c2"], "minSelect": 0, "maxSelect": 1}
        server, text = await with_game(
            router,
            mcp_client,
            games.with_prompt(prompt),
            tool="duels_respond_to_prompt",
            target_instance_ids=["c1"],
        )
        assert text.startswith("Error:")
        assert "selected_card_ids" in text and "target_instance_ids" in text
        assert [m for m in server.received if m.get("action")] == []

    async def test_the_right_field_still_works(self, router, mcp_client):
        prompt = {"id": "p1", "player": 1, "type": "select_card",
                  "cardInstanceIds": ["c1", "c2"], "minSelect": 0, "maxSelect": 1}
        server, _text = await with_game(
            router,
            mcp_client,
            games.with_prompt(prompt),
            tool="duels_respond_to_prompt",
            selected_card_ids=["c1"],
        )
        assert server.received[-1]["action"]["response"]["cardInstanceIds"] == ["c1"]


class TestTheStateWeShowIsTheStateAfter:
    """The socket's cached game can still be the one from before the action."""

    async def test_a_lagging_update_is_waited_for(self, router, mcp_client):
        """Rendered without waiting, a successful END_TURN still reported
        YOUR TURN and the next call was refused as "Not your turn"."""
        async with FakeGameServer(games.playing(), update_delay=0.2) as server:
            router.json_on(
                f"/api/game/{games.GAME_ID}/ws-token", {"token": "t", "wsUrl": server.url}
            )
            payload = await call_json(
                mcp_client, "duels_quest", game_id=games.GAME_ID,
                card_instance_id=games.FIELD_ELSA,
            )
        # The fake bumps stateVersion on every action; seeing the bump proves
        # the render waited for the push rather than using the cached state.
        assert payload["state_version"] > games.playing()["stateVersion"]

    async def end_turn(self, router, mcp_client, tool, *, as_json=False, steps=None, **server):
        """End the turn with `tool` against a server scripted by `server`."""
        async with FakeGameServer(games.playing(), **server) as fake:
            router.json_on(
                f"/api/game/{games.GAME_ID}/ws-token", {"token": "t", "wsUrl": fake.url}
            )
            kwargs = {"steps": steps or [{"do": "end"}]} if tool == "duels_play_turn" else {}
            call = call_json if as_json else call_text
            return await call(mcp_client, tool, game_id=games.GAME_ID, **kwargs)

    @pytest.mark.parametrize("tool", ["duels_play_turn", "duels_end_turn"])
    async def test_a_log_ahead_of_the_state_is_not_the_turn_ending(
        self, router, mcp_client, tool
    ):
        """Seen twice in one ranked game: "Played 4 of 4 steps: ..., end", and
        under it "YOUR TURN" with "End your turn" the only move. The server
        acknowledged, sent the log, then the state - the log woke the wait,
        and the render used the turn that had just ended."""
        text = await self.end_turn(
            router, mcp_client, tool, log_first=True, update_delay=1.0
        )
        assert "opponent's turn" in text
        assert "YOUR TURN" not in text and "Not confirmed" not in text

    @pytest.mark.parametrize(
        "outcome",
        [
            games.with_prompt(prompts.BOOLEAN),
            games.finished(winner=1),
            games.playing(opponentHasPendingPrompts=True),
        ],
        ids=["an-end-of-turn-prompt", "the-game-ended", "waiting-on-their-answer"],
    )
    async def test_a_turn_that_does_not_pass_can_still_have_ended(
        self, router, mcp_client, monkeypatch, outcome
    ):
        """An end-of-turn ability can ask something first, the game can end on
        it, or the table can wait on the opponent. Holding out for the turn to
        pass regardless would spend the whole wait, then doubt a true state."""
        monkeypatch.setattr("src.tools.ingame.TURN_PASS_SECONDS", 0.5)
        text = await self.end_turn(
            router, mcp_client, "duels_play_turn", after_end_turn=outcome
        )
        assert "Not confirmed" not in text

    @pytest.mark.parametrize("tool", ["duels_play_turn", "duels_end_turn"])
    async def test_a_state_that_never_comes_is_said_not_passed_off(
        self, router, mcp_client, monkeypatch, tool
    ):
        """What arrived is shown, but not as the turn still being ours."""
        monkeypatch.setattr("src.tools.ingame.TURN_PASS_SECONDS", 0.3)
        text = await self.end_turn(
            router, mcp_client, tool, after_end_turn=games.playing()
        )
        assert "Not confirmed" in text and "Do not end it again" in text

    @pytest.mark.parametrize(
        "steps, after, seen",
        [
            ([{"do": "end"}], None, True),
            ([{"do": "end"}], games.playing(), False),
            ([{"do": "quest", "card": games.FIELD_ELSA}], None, None),
        ],
        ids=["handed-over", "never-seen", "no-end-in-the-plan"],
    )
    async def test_the_json_says_whether_the_turn_end_was_seen(
        self, router, mcp_client, monkeypatch, steps, after, seen
    ):
        monkeypatch.setattr("src.tools.ingame.TURN_PASS_SECONDS", 0.3)
        payload = await self.end_turn(
            router, mcp_client, "duels_play_turn", as_json=True, steps=steps,
            after_end_turn=after,
        )
        assert payload.get("turn_end_seen") is seen
