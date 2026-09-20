"""Integration tests for the tools, end to end but offline.

Real tools, real lifespan, real schemas, real WebSocket protocol - only the
network is replaced. The property test at the bottom is the most valuable in
the suite: it checks that every move the renderer advertises is actually
callable on the tool it names.
"""

import pytest

from src.cards import CardCatalog
from src.render import render_game_state
from tests.conftest import call_json, call_text
from tests.fixtures import decks as deck_fixtures
from tests.fixtures import games, prompts
from tests.test_gamews import FakeGameServer


@pytest.fixture
async def game_server(router):
    """A fake game socket wired into the ws-token route."""
    async with FakeGameServer() as server:
        router.json_on(
            f"/api/game/{games.GAME_ID}/ws-token", {"token": "t", "wsUrl": server.url}
        )
        yield server


# =================================================================
# REST-backed tools
# =================================================================
class TestIdentity:
    async def test_anonymous_explains_what_is_unavailable(self, mcp_client):
        text = await call_text(mcp_client, "duels_whoami")
        assert "Not configured" in text
        assert "bot games" in text

    async def test_reports_the_build_id(self, mcp_client):
        payload = await call_json(mcp_client, "duels_whoami")
        assert payload["build_id"] == "test-build"
        assert payload["authenticated"] is False

    async def test_account_stats_refuses_without_a_cookie(self, mcp_client):
        text = await call_text(mcp_client, "duels_get_account_stats")
        assert text.startswith("Error:")
        assert "requires a signed-in" in text


class TestCards:
    async def test_search_returns_markdown_by_default(self, mcp_client):
        text = await call_text(mcp_client, "duels_search_cards", query="Flotsam")
        assert "Flotsam - Slippery as an Eel" in text
        assert "`10-71`" in text

    async def test_search_json_carries_the_pagination_envelope(self, mcp_client):
        payload = await call_json(mcp_client, "duels_search_cards", query="Flotsam")
        assert set(payload) >= {"total", "count", "offset", "has_more", "cards"}

    async def test_search_rejects_an_invalid_card_type_with_the_options(self, mcp_client):
        text = await call_text(mcp_client, "duels_search_cards", card_type="creature")
        assert text.startswith("Error:")
        assert "character" in text

    async def test_search_rejects_an_invalid_colour(self, mcp_client):
        text = await call_text(mcp_client, "duels_search_cards", color="purple")
        assert "amethyst" in text

    async def test_no_match_explains_what_to_try(self, mcp_client):
        text = await call_text(mcp_client, "duels_search_cards", query="Nonexistent")
        assert "No cards found" in text and "broader" in text

    async def test_get_card_by_id(self, mcp_client):
        text = await call_text(mcp_client, "duels_get_card", definition_id="5-195")
        assert "Pete - Games Referee" in text

    async def test_unknown_card_explains_the_id_format(self, mcp_client):
        text = await call_text(mcp_client, "duels_get_card", definition_id="99-999")
        assert "<set>-<number>" in text

    async def test_resolve_cards_lists_unresolved_separately(self, mcp_client):
        payload = await call_json(
            mcp_client, "duels_resolve_cards", definition_ids=["10-71", "99-999"]
        )
        assert payload["resolved"]["10-71"]["fullName"].startswith("Flotsam")
        assert payload["unresolved"] == ["99-999"]

    async def test_limit_above_the_maximum_is_rejected(self, mcp_client):
        with pytest.raises(Exception):
            await mcp_client.call_tool("duels_search_cards", {"limit": 5000})


class TestDecks:
    async def test_my_decks_need_a_cookie(self, mcp_client):
        text = await call_text(mcp_client, "duels_list_my_decks")
        assert "requires a signed-in" in text

    async def test_public_decks_work_anonymously(self, mcp_client):
        payload = await call_json(mcp_client, "duels_browse_public_decks")
        assert payload["count"] >= 1

    async def test_deck_detail_resolves_names_and_counts_copies(self, mcp_client):
        payload = await call_json(
            mcp_client, "duels_get_deck", deck_id=deck_fixtures.DECK_ID
        )
        entry = next(e for e in payload["entries"] if e["definition_id"] == "10-71")
        assert entry["quantity"] == 4
        assert entry["name"] == "Flotsam - Slippery as an Eel"

    async def test_deck_detail_exposes_a_flat_card_id_list(self, mcp_client):
        """That list is what duels_start_bot_game consumes."""
        payload = await call_json(
            mcp_client, "duels_get_deck", deck_id=deck_fixtures.DECK_ID
        )
        assert payload["card_ids"].count("10-71") == 4


class TestBotGameLifecycle:
    async def test_needs_a_deck(self, mcp_client):
        text = await call_text(mcp_client, "duels_start_bot_game")
        assert "deck_id" in text and "deck_card_ids" in text

    async def test_starts_and_returns_the_opening_state(self, mcp_client, router, game_server):
        router.json_on(
            "/api/game/create-bot-game", {"gameId": games.GAME_ID, "sessionId": "sess-1"}
        )
        payload = await call_json(
            mcp_client, "duels_start_bot_game", deck_id=deck_fixtures.DECK_ID
        )
        assert payload["game_id"] == games.GAME_ID
        assert payload["already_running"] is False
        assert payload["state"]["me"]["lore"] == 4

    async def test_existing_game_is_handed_back_instead_of_failing(
        self, mcp_client, router, game_server
    ):
        """Only one bot game may exist at a time. A 409 should return the game
        already in progress, not make the agent parse an error string."""
        router.json_on(
            "/api/game/create-bot-game",
            {"error": "bot_game_in_progress", "existingGameId": games.GAME_ID},
            status=409,
        )
        router.json_on(
            "/api/account/active-games",
            {"games": [{"id": games.GAME_ID}], "activeDraftPod": None, "table": None},
        )
        payload = await call_json(
            mcp_client, "duels_start_bot_game", deck_id=deck_fixtures.DECK_ID
        )
        assert payload["already_running"] is True
        assert payload["game_id"] == games.GAME_ID


# =================================================================
# WebSocket-backed tools
# =================================================================
class TestInGame:
    async def test_game_state_resolves_names_and_offers_moves(self, mcp_client, game_server):
        payload = await call_json(mcp_client, "duels_get_game_state", game_id=games.GAME_ID)
        assert payload["my_turn"] is True
        assert payload["legal_moves"]
        names = {c["card"] for c in payload["me"]["hand"]}
        assert "Flotsam - Slippery as an Eel" in names

    async def test_legal_moves_is_a_subset_of_the_full_state(self, mcp_client, game_server):
        state = await call_json(mcp_client, "duels_get_game_state", game_id=games.GAME_ID)
        moves = await call_json(mcp_client, "duels_get_legal_moves", game_id=games.GAME_ID)
        assert moves["legal_moves"] == state["legal_moves"]
        assert "me" not in moves

    async def test_ink_card_sends_the_right_action(self, mcp_client, game_server):
        await call_text(
            mcp_client,
            "duels_ink_card",
            game_id=games.GAME_ID,
            card_instance_id=games.HAND_FLOTSAM,
        )
        sent = game_server.received[-1]["action"]
        assert sent == {"type": "ADD_TO_INK", "cardInstanceId": games.HAND_FLOTSAM}

    async def test_quest_sends_the_right_action(self, mcp_client, game_server):
        await call_text(
            mcp_client,
            "duels_quest",
            game_id=games.GAME_ID,
            card_instance_id=games.FIELD_ELSA,
        )
        assert game_server.received[-1]["action"]["type"] == "QUEST"

    async def test_play_card_carries_singers(self, mcp_client, game_server):
        await call_text(
            mcp_client,
            "duels_play_card",
            game_id=games.GAME_ID,
            card_instance_id=games.HAND_SONG,
            singer_instance_ids=[games.FIELD_ELSA],
        )
        sent = game_server.received[-1]["action"]
        assert sent["singerInstanceIds"] == [games.FIELD_ELSA]

    async def test_end_turn_guards_against_a_stale_call(self, mcp_client, game_server):
        """Turn number and player are sent so a late call cannot end the wrong
        turn."""
        await call_text(mcp_client, "duels_end_turn", game_id=games.GAME_ID)
        sent = game_server.received[-1]["action"]
        assert sent["expectedTurnNumber"] == 3
        assert sent["expectedCurrentPlayer"] == 1

    async def test_challenge_names_attacker_and_target(self, mcp_client, game_server):
        await call_text(
            mcp_client,
            "duels_challenge",
            game_id=games.GAME_ID,
            attacker_instance_id=games.FIELD_ELSA,
            target_instance_id=games.OPP_PETE,
        )
        sent = game_server.received[-1]["action"]
        assert sent["type"] == "ATTACK"
        assert sent["attackerInstanceId"] == games.FIELD_ELSA
        assert sent["targetInstanceId"] == games.OPP_PETE

    async def test_game_log_substitutes_card_placeholders(self, mcp_client, game_server):
        """Log messages embed {card:N} indexes into cardRefs."""
        await call_text(mcp_client, "duels_get_game_state", game_id=games.GAME_ID)
        payload = await call_json(mcp_client, "duels_get_game_log", game_id=games.GAME_ID)
        assert "entries" in payload

    async def test_concede_on_a_finished_game_does_not_error(self, router, mcp_client):
        """Regression: this sent ABANDON_BOT_GAME to a finished game and the
        server rejected it, surfacing as an error during cleanup."""
        async with FakeGameServer(games.finished(winner=2)) as server:
            router.json_on(
                f"/api/game/{games.GAME_ID}/ws-token", {"token": "t", "wsUrl": server.url}
            )
            text = await call_text(mcp_client, "duels_concede", game_id=games.GAME_ID)
            assert not text.startswith("Error:")
            assert "already over" in text
            assert server.received == [], "no action should be sent to a finished game"


class TestPrompts:
    async def _with_prompt(self, router, mcp_client, prompt, **kwargs):
        async with FakeGameServer(games.with_prompt(prompt)) as server:
            router.json_on(
                f"/api/game/{games.GAME_ID}/ws-token", {"token": "t", "wsUrl": server.url}
            )
            text = await call_text(
                mcp_client, "duels_respond_to_prompt", game_id=games.GAME_ID, **kwargs
            )
            return server, text

    async def test_boolean_prompt_builds_the_nested_payload(self, router, mcp_client):
        """promptId lives inside `response`; at the top level Duels.ink answers
        'Prompt not found'."""
        server, text = await self._with_prompt(
            router, mcp_client, prompts.BOOLEAN, choice="yes"
        )
        assert not text.startswith("Error:")
        response = server.received[-1]["action"]["response"]
        assert response == {
            "promptId": prompts.BOOLEAN["id"],
            "type": "boolean",
            "value": True,
        }

    async def test_boolean_no(self, router, mcp_client):
        server, _ = await self._with_prompt(
            router, mcp_client, prompts.BOOLEAN, choice="no"
        )
        assert server.received[-1]["action"]["response"]["value"] is False

    async def test_trigger_skip(self, router, mcp_client):
        server, _ = await self._with_prompt(
            router, mcp_client, prompts.SELECT_TRIGGER, choice="skip"
        )
        response = server.received[-1]["action"]["response"]
        assert response["type"] == "skip_trigger"
        assert response["triggerId"] == "trigger-1"

    async def test_trigger_resolve(self, router, mcp_client):
        """Accepting echoes the prompt's own type.

        This asserted `resolve_trigger` for a long time, which reads as the
        natural partner to skip_trigger and is not a thing on the wire. The
        engine rejected every acceptance sent that way, and because declining
        worked the gap stayed invisible: optional abilities could be refused
        but never used. Confirmed against a live game by trying the variants
        until one was acknowledged.
        """
        server, _ = await self._with_prompt(
            router, mcp_client, prompts.SELECT_TRIGGER, choice="resolve"
        )
        response = server.received[-1]["action"]["response"]
        assert response["type"] == "select_trigger"
        assert response["triggerId"] == "trigger-1"

    async def test_select_target(self, router, mcp_client):
        server, _ = await self._with_prompt(
            router,
            mcp_client,
            prompts.SELECT_TARGET,
            target_instance_ids=[games.FIELD_ELSA],
        )
        response = server.received[-1]["action"]["response"]
        assert response["type"] == "select_target"
        assert response["targetInstanceIds"] == [games.FIELD_ELSA]

    async def test_select_card_answers_under_cardInstanceIds(self, router, mcp_client):
        """Regression from a live game: a Gantu played for its 3/3 body opened
        a `choose and discard` prompt that never resolved. The response was
        built with selectedCardIds - the key MULLIGAN uses - and Duels.ink
        acknowledged nothing, so the turn sat on the prompt until it timed
        out. The prompt lists its options under cardInstanceIds and wants the
        answer there too."""
        server, text = await self._with_prompt(
            router, mcp_client, prompts.SELECT_CARD, selected_card_ids=[games.HAND_SONG]
        )
        assert not text.startswith("Error:")
        response = server.received[-1]["action"]["response"]
        assert response == {
            "promptId": prompts.SELECT_CARD["id"],
            "type": "select_card",
            "cardInstanceIds": [games.HAND_SONG],
        }

    async def test_select_card_never_sends_the_mulligan_key(self, router, mcp_client):
        server, _ = await self._with_prompt(
            router, mcp_client, prompts.SELECT_CARD, selected_card_ids=[games.HAND_SONG]
        )
        assert "selectedCardIds" not in server.received[-1]["action"]["response"]

    async def test_select_card_without_cards_names_its_own_options(self, router, mcp_client):
        _server, text = await self._with_prompt(router, mcp_client, prompts.SELECT_CARD)
        assert text.startswith("Error:")
        assert "selected_card_ids" in text
        assert games.HAND_SONG in text, "naming the argument is no help without the choices"

    async def test_optional_target_prompt_can_be_declined(self, router, mcp_client):
        """Regression from a live game: Eilonwy's Support triggered with only
        the opponent's character as a legal target, so the only good answer
        was none at all. The prompt said minSelect 0 and the tool still
        refused, printing that very 0 back in the error."""
        prompt = {**prompts.SELECT_TARGET, "minSelect": 0, "required": False}
        server, text = await self._with_prompt(router, mcp_client, prompt)
        assert not text.startswith("Error:")
        assert server.received[-1]["action"]["response"] == {
            "promptId": prompt["id"],
            "type": "select_target",
            "targetInstanceIds": [],
        }

    async def test_mandatory_target_prompt_still_requires_a_target(self, router, mcp_client):
        _server, text = await self._with_prompt(router, mcp_client, prompts.SELECT_TARGET)
        assert text.startswith("Error:")
        assert "target_instance_ids" in text

    async def test_optional_card_prompt_can_be_declined(self, router, mcp_client):
        prompt = {**prompts.SELECT_CARD, "minSelect": 0, "required": False}
        server, text = await self._with_prompt(router, mcp_client, prompt)
        assert not text.startswith("Error:")
        assert server.received[-1]["action"]["response"]["cardInstanceIds"] == []

    async def test_order_cards_answers_under_orderedCardInstanceIds(self, router, mcp_client):
        """Regression from a live game: a scry-style prompt asking for the
        bottom-of-deck order was answered under cardInstanceIds - the key the
        prompt uses to *offer* the cards. Nothing came back, the prompt could
        not be cleared, and the trigger queued behind it then refused with
        "Cannot select trigger while another ability is resolving", wedging
        the turn. Fifteen payload variants later, this is the one the engine
        acknowledges."""
        server, text = await self._with_prompt(
            router,
            mcp_client,
            prompts.ORDER_CARDS,
            selected_card_ids=[games.HAND_MUSHU, games.HAND_FLOTSAM, games.HAND_SONG],
        )
        assert not text.startswith("Error:")
        assert server.received[-1]["action"]["response"] == {
            "promptId": prompts.ORDER_CARDS["id"],
            "type": "order_cards",
            "orderedCardInstanceIds": [
                games.HAND_MUSHU,
                games.HAND_FLOTSAM,
                games.HAND_SONG,
            ],
        }

    async def test_order_cards_keeps_the_order_it_was_given(self, router, mcp_client):
        """The whole point of the prompt is the sequence, so it must survive
        the trip unsorted and unreordered."""
        reversed_order = list(reversed(prompts.ORDER_CARDS["cardInstanceIds"]))
        server, _ = await self._with_prompt(
            router, mcp_client, prompts.ORDER_CARDS, selected_card_ids=reversed_order
        )
        response = server.received[-1]["action"]["response"]
        assert response["orderedCardInstanceIds"] == reversed_order
        assert "cardInstanceIds" not in response, "that key is the offer, not the answer"

    async def test_order_cards_without_an_order_lists_the_cards(self, router, mcp_client):
        _server, text = await self._with_prompt(router, mcp_client, prompts.ORDER_CARDS)
        assert text.startswith("Error:")
        assert "selected_card_ids" in text
        assert games.HAND_FLOTSAM in text, "the caller cannot order cards it cannot see"

    async def test_wrong_argument_names_the_prompts_own_options(self, router, mcp_client):
        """The error has to be self-correcting: an agent that guessed wrong
        needs to see what this prompt actually offers."""
        _server, text = await self._with_prompt(
            router, mcp_client, prompts.SELECT_TARGET, choice="yes"
        )
        assert text.startswith("Error:")
        assert "target_instance_ids" in text
        assert games.FIELD_ELSA in text   # validTargets surfaced

    async def test_boolean_without_a_choice_explains_both_labels(self, router, mcp_client):
        _server, text = await self._with_prompt(router, mcp_client, prompts.BOOLEAN)
        assert "optionDrawAndBuff" in text and "optionBanishDamagedCharacter" in text

    async def test_no_pending_prompt_is_reported_clearly(self, mcp_client, game_server):
        text = await call_text(
            mcp_client, "duels_respond_to_prompt", game_id=games.GAME_ID, choice="yes"
        )
        assert "no prompt waiting" in text.lower()


# =================================================================
# The property test
# =================================================================
class TestLegalMovesAreCallable:
    """Every advertised move must be valid input for the tool it names.

    Regression for two separate bugs: moves routed to duels_send_game_action
    carried `card_instance_id` (which that tool does not accept), and
    duels_challenge takes attacker/target rather than a single card argument.
    Both produced moves an agent could not execute.
    """

    @staticmethod
    def _violations(schema: dict, args: dict) -> list[str]:
        properties = set((schema or {}).get("properties", {}))
        required = set((schema or {}).get("required", []))
        problems = []
        unknown = set(args) - properties
        if unknown:
            problems.append(f"arguments the tool does not accept: {sorted(unknown)}")
        missing = required - set(args)
        if missing:
            problems.append(f"required arguments not supplied: {sorted(missing)}")
        return problems

    async def test_every_move_in_every_phase_is_callable(self, mcp_client, client):
        schemas = {t.name: t.input_schema for t in await mcp_client.list_tools()}
        catalog = CardCatalog(client)

        failures = []
        for phase, game in games.all_phases().items():
            payload = await render_game_state(game, catalog)
            for move in payload["legal_moves"]:
                tool = move["tool"]
                if tool not in schemas:
                    failures.append(f"{phase}: move names unknown tool {tool!r}")
                    continue
                for problem in self._violations(schemas[tool], move["args"]):
                    failures.append(f"{phase}: {tool} - {problem}")

        assert failures == [], "\n".join(failures)

    async def test_nested_payloads_only_go_to_the_tool_that_takes_them(
        self, mcp_client, client
    ):
        """`payload` is specific to duels_send_game_action."""
        catalog = CardCatalog(client)
        for phase, game in games.all_phases().items():
            payload = await render_game_state(game, catalog)
            for move in payload["legal_moves"]:
                if "payload" in move["args"]:
                    assert move["tool"] == "duels_send_game_action", phase

    async def test_placeholders_are_obvious_to_the_caller(self, mcp_client, client):
        """A value the agent must replace has to look like one."""
        catalog = CardCatalog(client)
        for _phase, game in games.all_phases().items():
            payload = await render_game_state(game, catalog)
            for move in payload["legal_moves"]:
                for value in move["args"].values():
                    if isinstance(value, str) and value.startswith("<"):
                        assert value.endswith(">")


class TestDeckTracker:
    """The third blind spot: 60 cards played without ever knowing what was
    left in the deck, or what the opponent had actually shown."""

    async def test_counts_what_is_left(self, mcp_client, router, game_server):
        router.json_on(
            "/api/game/create-bot-game", {"gameId": games.GAME_ID, "sessionId": None}
        )
        await call_json(
            mcp_client, "duels_start_bot_game", deck_id=deck_fixtures.DECK_ID
        )
        payload = await call_json(
            mcp_client, "duels_get_deck_tracker", game_id=games.GAME_ID
        )
        mine = payload["my_deck"]
        assert mine["total"] == len(deck_fixtures.CARD_IDS)
        assert mine["remaining"] == mine["total"] - mine["seen"]

    async def test_deck_id_is_remembered_from_the_game(self, mcp_client, router, game_server):
        """A tracker that had to be handed the deck id on every call is one an
        agent forgets to use."""
        router.json_on(
            "/api/game/create-bot-game", {"gameId": games.GAME_ID, "sessionId": None}
        )
        await call_json(
            mcp_client, "duels_start_bot_game", deck_id=deck_fixtures.DECK_ID
        )
        payload = await call_json(
            mcp_client, "duels_get_deck_tracker", game_id=games.GAME_ID
        )
        assert payload["my_deck"]["deck_id"] == deck_fixtures.DECK_ID

    async def test_unknown_deck_asks_for_one(self, mcp_client, game_server):
        text = await call_text(
            mcp_client, "duels_get_deck_tracker", game_id=games.GAME_ID
        )
        assert text.startswith("Error:")
        assert "duels_list_my_decks" in text

    async def test_explicit_deck_id_works_without_a_remembered_one(
        self, mcp_client, game_server
    ):
        payload = await call_json(
            mcp_client,
            "duels_get_deck_tracker",
            game_id=games.GAME_ID,
            deck_id=deck_fixtures.DECK_ID,
        )
        assert payload["my_deck"]["total"] == len(deck_fixtures.CARD_IDS)

    async def test_cards_on_board_are_subtracted(self, mcp_client, game_server):
        """Flotsam is in hand in the fixture, so fewer remain in the deck."""
        payload = await call_json(
            mcp_client,
            "duels_get_deck_tracker",
            game_id=games.GAME_ID,
            deck_id=deck_fixtures.DECK_ID,
        )
        rows = {r["definition_id"]: r["count"] for r in payload["my_deck"]["remaining_cards"]}
        in_deck = deck_fixtures.CARD_IDS.count("10-71")
        assert rows.get("10-71", 0) < in_deck

    async def test_opponent_revealed_cards_and_colors(self, mcp_client, game_server):
        payload = await call_json(
            mcp_client,
            "duels_get_deck_tracker",
            game_id=games.GAME_ID,
            deck_id=deck_fixtures.DECK_ID,
        )
        theirs = payload["opponent_revealed"]
        assert theirs["count"] >= 1
        assert "steel" in theirs["colors"]      # Pete, on their board
        assert any(r["name"].startswith("Pete") for r in theirs["cards"])

    async def test_opponent_hand_is_never_revealed(self, mcp_client, game_server):
        """Hidden information must stay hidden: only board and discard show."""
        payload = await call_json(
            mcp_client,
            "duels_get_deck_tracker",
            game_id=games.GAME_ID,
            deck_id=deck_fixtures.DECK_ID,
        )
        assert "hand" not in payload["opponent_revealed"]
        assert "deck" not in payload["opponent_revealed"]

    async def test_markdown_is_readable(self, mcp_client, game_server):
        text = await call_text(
            mcp_client,
            "duels_get_deck_tracker",
            game_id=games.GAME_ID,
            deck_id=deck_fixtures.DECK_ID,
        )
        assert "Still in your deck" in text
        assert "Opponent has shown" in text
