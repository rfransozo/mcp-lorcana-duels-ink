"""Tests for the game-state renderer.

This is where most of the risk lives: the renderer decides what an agent is
told it can do. Several cases below are regressions for bugs that reached the
live site before being caught.
"""

import pytest

from src.render import (
    BOARD_CAPABILITIES,
    CAPABILITY_TOOLS,
    HAND_CAPABILITIES,
    NON_ACTION_FLAGS,
    game_state_markdown,
    render_game_state,
)
from tests.fixtures import games, prompts


def tools_offered(payload: dict) -> set[str]:
    return {m["tool"] for m in payload["legal_moves"]}


class TestCapabilityMap:
    def test_only_observed_flags_are_mapped(self):
        """Regression: canMove, canBoost and canActivate were once mapped here
        but do not exist in the Duels.ink client at all. Emitting moves for
        invented flags produced actions the server rejected."""
        assert set(CAPABILITY_TOOLS) == {
            "canInk",
            "canPlay",
            "canQuest",
            "canChallenge",
            "canSing",
        }

    def test_zones_partition_the_mapped_flags(self):
        assert HAND_CAPABILITIES | BOARD_CAPABILITIES == set(CAPABILITY_TOOLS)
        assert not (HAND_CAPABILITIES & BOARD_CAPABILITIES)

    def test_property_flags_are_not_actions(self):
        assert "canAffordInkCost" in NON_ACTION_FLAGS
        assert "canBeSinger" in NON_ACTION_FLAGS


class TestPhaseGates:
    async def test_coin_toss_offers_play_and_draw(self, catalog):
        payload = await render_game_state(games.coin_toss(mine=True), catalog)
        choices = {
            m["args"]["payload"]["choice"]
            for m in payload["legal_moves"]
            if m["args"].get("action_type") == "CHOOSE_STARTING_PLAYER"
        }
        assert choices == {"play", "draw"}

    async def test_coin_toss_does_not_offer_inking(self, catalog):
        """availableActions is already populated during the coin toss, so
        trusting it blindly advertises moves the game will not accept."""
        payload = await render_game_state(games.coin_toss(mine=True), catalog)
        assert "duels_ink_card" not in tools_offered(payload)
        assert "duels_end_turn" not in tools_offered(payload)

    async def test_coin_toss_won_by_opponent_means_wait(self, catalog):
        payload = await render_game_state(games.coin_toss(mine=False), catalog)
        assert tools_offered(payload) == {"duels_wait_for_my_turn"}

    async def test_mulligan_offers_the_mulligan(self, catalog):
        payload = await render_game_state(games.mulligan(my_turn_to_act=True), catalog)
        move = payload["legal_moves"][0]
        assert move["args"]["action_type"] == "MULLIGAN"
        assert move["args"]["payload"] == {"selectedCardIds": []}

    async def test_mulligan_already_submitted_means_wait(self, catalog):
        """Regression: this returned 'ready' immediately, so an agent waiting
        on the opponent's mulligan spun instead of blocking."""
        payload = await render_game_state(games.mulligan(my_turn_to_act=False), catalog)
        assert tools_offered(payload) == {"duels_wait_for_my_turn"}

    async def test_pending_prompt_blocks_everything_else(self, catalog):
        payload = await render_game_state(games.with_prompt(prompts.BOOLEAN), catalog)
        assert tools_offered(payload) == {"duels_respond_to_prompt"}

    async def test_opponents_turn_means_wait(self, catalog):
        payload = await render_game_state(games.opponents_turn(), catalog)
        assert tools_offered(payload) == {"duels_wait_for_my_turn"}

    async def test_finished_game_has_no_moves(self, catalog):
        payload = await render_game_state(games.finished(winner=1), catalog)
        assert payload["legal_moves"] == []
        assert payload["winner"] == 1 and payload["i_won"] is True

    async def test_loss_is_reported_from_my_perspective(self, catalog):
        payload = await render_game_state(games.finished(winner=2), catalog)
        assert payload["i_won"] is False

    async def test_live_game_always_offers_a_next_step(self, catalog):
        """Regression: with nothing playable and canEndTurn false, the move list
        came back empty and the agent had nowhere to go."""
        payload = await render_game_state(games.stuck(), catalog)
        assert payload["legal_moves"], "a live game must never offer zero moves"
        assert tools_offered(payload) == {"duels_wait_for_my_turn"}


class TestZoneFiltering:
    async def test_hand_cards_are_not_offered_board_actions(self, catalog):
        """Regression: a hand card carrying a board capability produced a move
        the server rejected with 'Character not on field!'."""
        game = games.playing()
        game["availableActions"]["cards"][games.HAND_FLOTSAM]["canQuest"] = True
        payload = await render_game_state(game, catalog)
        quests = [
            m for m in payload["legal_moves"]
            if m["tool"] == "duels_quest"
            and m["args"].get("card_instance_id") == games.HAND_FLOTSAM
        ]
        assert quests == []

    async def test_board_cards_are_not_offered_hand_actions(self, catalog):
        game = games.playing()
        game["availableActions"]["cards"][games.FIELD_ELSA]["canInk"] = True
        payload = await render_game_state(game, catalog)
        inks = [
            m for m in payload["legal_moves"]
            if m["tool"] == "duels_ink_card"
            and m["args"].get("card_instance_id") == games.FIELD_ELSA
        ]
        assert inks == []

    async def test_board_card_can_still_quest(self, catalog):
        payload = await render_game_state(games.playing(), catalog)
        quests = [m for m in payload["legal_moves"] if m["tool"] == "duels_quest"]
        assert [m["args"]["card_instance_id"] for m in quests] == [games.FIELD_ELSA]

    async def test_hand_card_can_still_be_inked(self, catalog):
        payload = await render_game_state(games.playing(), catalog)
        inked = {
            m["args"]["card_instance_id"]
            for m in payload["legal_moves"]
            if m["tool"] == "duels_ink_card"
        }
        assert inked == {games.HAND_FLOTSAM, games.HAND_SONG}


class TestMoveArguments:
    async def test_challenge_uses_attacker_and_target_names(self, catalog):
        payload = await render_game_state(games.playing(), catalog)
        challenge = next(m for m in payload["legal_moves"] if m["tool"] == "duels_challenge")
        assert challenge["args"]["attacker_instance_id"] == games.FIELD_ELSA
        assert challenge["args"]["target_instance_id"].startswith("<")

    async def test_sing_carries_a_valid_singer(self, catalog):
        payload = await render_game_state(games.playing(), catalog)
        sings = [
            m for m in payload["legal_moves"]
            if m["tool"] == "duels_play_card"
            and m["args"].get("card_instance_id") == games.HAND_SONG
        ]
        assert sings and sings[0]["args"]["singer_instance_ids"] == [games.FIELD_ELSA]

    async def test_every_move_carries_the_game_id(self, catalog):
        for name, game in games.all_phases().items():
            payload = await render_game_state(game, catalog)
            for move in payload["legal_moves"]:
                assert move["args"].get("game_id") == games.GAME_ID, name

    async def test_every_move_explains_itself(self, catalog):
        for name, game in games.all_phases().items():
            payload = await render_game_state(game, catalog)
            for move in payload["legal_moves"]:
                assert move["why"].strip(), f"{name}: a move with no explanation"

    async def test_unknown_capability_degrades_to_a_hint(self, catalog):
        """A new Duels.ink flag must surface, not vanish - but it must be clear
        the action type still has to be supplied."""
        game = games.playing()
        game["availableActions"]["cards"][games.FIELD_ELSA]["canTeleport"] = True
        payload = await render_game_state(game, catalog)
        hints = [m for m in payload["legal_moves"] if "canTeleport" in m["why"]]
        assert hints and hints[0]["args"]["action_type"] == "<ACTION TYPE>"


class TestBoardDescription:
    async def test_cards_are_resolved_to_names(self, catalog):
        payload = await render_game_state(games.playing(), catalog)
        names = {c["card"] for c in payload["me"]["hand"]}
        assert "Flotsam - Slippery as an Eel" in names
        assert "Education or Elimination" in names

    async def test_blocked_reason_is_surfaced(self, catalog):
        payload = await render_game_state(games.playing(), catalog)
        flotsam = next(
            c for c in payload["me"]["hand"] if c["instance_id"] == games.HAND_FLOTSAM
        )
        assert flotsam["blocked"] == "Need 3 ink"

    async def test_damage_reduces_remaining_willpower(self, catalog):
        payload = await render_game_state(games.playing(), catalog)
        rapunzel = next(
            c for c in payload["me"]["field"] if c["instance_id"] == games.FIELD_RAPUNZEL
        )
        assert rapunzel["damage"] == 1
        assert rapunzel["willpower_remaining"] == 2   # 3 willpower - 1 damage

    async def test_exertion_is_reported(self, catalog):
        payload = await render_game_state(games.playing(), catalog)
        rapunzel = next(
            c for c in payload["me"]["field"] if c["instance_id"] == games.FIELD_RAPUNZEL
        )
        assert rapunzel["exerted"] is True

    async def test_opponent_hand_stays_hidden(self, catalog):
        """Hidden information must not leak: the opponent's hand is a count."""
        payload = await render_game_state(games.playing(), catalog)
        assert payload["opponent"]["hand_count"] == 5
        assert "hand" not in payload["opponent"]

    async def test_opponent_board_is_visible(self, catalog):
        payload = await render_game_state(games.playing(), catalog)
        assert payload["opponent"]["field"][0]["card"] == "Pete - Games Referee"

    async def test_scoreboard_fields(self, catalog):
        payload = await render_game_state(games.playing(), catalog)
        assert payload["me"]["lore"] == 4
        assert payload["opponent"]["lore"] == 2
        assert payload["me"]["ink_available"] == 2
        assert payload["me"]["ink_total"] == 2
        assert payload["my_turn"] is True

    async def test_my_turn_is_false_on_the_opponents_turn(self, catalog):
        payload = await render_game_state(games.opponents_turn(), catalog)
        assert payload["my_turn"] is False


class TestMarkdown:
    async def test_renders_every_phase_without_error(self, catalog):
        for name, game in games.all_phases().items():
            payload = await render_game_state(game, catalog)
            text = game_state_markdown(payload)
            assert text.strip(), f"{name}: empty markdown"
            assert games.GAME_ID in text, name

    async def test_shows_card_names_and_instance_ids(self, catalog):
        payload = await render_game_state(games.playing(), catalog)
        text = game_state_markdown(payload)
        assert "Flotsam - Slippery as an Eel" in text
        assert games.HAND_FLOTSAM in text

    async def test_flags_whose_turn_it_is(self, catalog):
        mine = game_state_markdown(await render_game_state(games.playing(), catalog))
        theirs = game_state_markdown(await render_game_state(games.opponents_turn(), catalog))
        assert "YOUR TURN" in mine
        assert "opponent's turn" in theirs

    async def test_announces_the_result(self, catalog):
        text = game_state_markdown(await render_game_state(games.finished(winner=1), catalog))
        assert "you won" in text

    async def test_survives_a_sparse_state(self, catalog):
        """Duels.ink may add or drop fields; rendering must degrade, not crash."""
        bare = {"id": games.GAME_ID, "status": "playing", "viewingAs": 1, "currentPlayer": 1}
        payload = await render_game_state(bare, catalog)
        assert game_state_markdown(payload).strip()
