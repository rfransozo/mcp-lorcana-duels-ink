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
        """Every mapped flag has been seen on the wire, and only those.

        canMove and canBoost were both removed once as invented, and both are
        real: they are simply conditional on the board. canMove needs a
        location in play, canBoost needs a Boost character *and* the ink to
        pay. No fixture had either, so every test agreed they did not exist
        until a deck built around locations and items proved otherwise.

        canActivate is the one that really is absent - activated abilities
        arrive as a structured `activatedAbilities` list instead.
        """
        assert set(CAPABILITY_TOOLS) == {
            "canInk",
            "canPlay",
            "canQuest",
            "canChallenge",
            "canSing",
            "canMove",
            "canBoost",
        }
        assert "canActivate" not in CAPABILITY_TOOLS

    def test_zones_partition_the_mapped_flags(self):
        assert HAND_CAPABILITIES | BOARD_CAPABILITIES == set(CAPABILITY_TOOLS)
        assert not (HAND_CAPABILITIES & BOARD_CAPABILITIES)

    def test_property_flags_are_not_actions(self):
        assert "canAffordInkCost" in NON_ACTION_FLAGS
        assert "canBeSinger" in NON_ACTION_FLAGS

    def test_has_prefixed_flags_describe_the_card(self):
        """hasSingTogether and hasQuestAbility both turned up advertised as
        unmapped capabilities, each offering a move that did not exist -
        hasQuestAbility on a Moana whose ink was still wet."""
        assert "hasSingTogether" in NON_ACTION_FLAGS
        assert "hasQuestAbility" in NON_ACTION_FLAGS


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


class TestCardText:
    """Regressions for the three blind spots that lost the measured game.

    The whole game was played in markdown, and `_card_line` rendered name,
    stats and flags but never the rules text. Three concrete costs: the RC's
    LOW BATTERIES was discovered by failing, Squeaks was treated as a wall
    without noticing Evasive, and the opponent's Pete was never read at all.
    """

    async def test_keywords_are_shown_on_the_card_line(self, catalog):
        game = games.playing()
        game["myPlayer"]["hand"].append(games.card("kw-1", "10-103"))  # Mushu, Rush
        text = game_state_markdown(await render_game_state(game, catalog))
        assert "{Rush}" in text

    async def test_keyword_values_are_rendered(self, catalog):
        """Resist and Singer carry a value; Evasive does not."""
        game = games.playing()
        game["myPlayer"]["hand"].append(games.card("kw-2", "7-11"))  # Troubadour
        text = game_state_markdown(await render_game_state(game, catalog))
        assert "Resist +1" in text and "Singer 4" in text

    async def test_named_ability_is_shown(self, catalog):
        """Regression: the RC's LOW BATTERIES was invisible, so the ink it
        needs to quest was discovered only by the action being rejected."""
        game = games.playing()
        game["myPlayer"]["hand"].append(games.card("rc-1", "12-77"))
        text = game_state_markdown(await render_game_state(game, catalog))
        assert "LOW BATTERIES" in text
        assert "pay 1" in text

    async def test_rules_text_is_shown_for_hand_cards(self, catalog):
        text = game_state_markdown(await render_game_state(games.playing(), catalog))
        assert "Evasive (Only characters with Evasive can challenge" in text

    async def test_rules_text_is_shown_for_opponent_cards(self, catalog):
        """Regression: the opponent's Pete cancels actions, which would have
        countered a removal spell. It was never visible."""
        text = game_state_markdown(await render_game_state(games.playing(), catalog))
        assert "BLOW THE WHISTLE" in text

    async def test_duplicate_card_text_is_printed_once(self, catalog):
        game = games.playing()
        game["myPlayer"]["hand"].append(games.card("dup-1", "10-71"))
        game["myPlayer"]["hand"].append(games.card("dup-2", "10-71"))
        text = game_state_markdown(await render_game_state(game, catalog))
        assert text.count("Only characters with Evasive can challenge") == 1
        assert "_(text above)_" in text

    async def test_card_without_abilities_does_not_break(self, catalog):
        game = games.playing()
        game["myPlayer"]["hand"] = [games.card("plain-1", "12-133")]  # Dangerous Plan
        text = game_state_markdown(await render_game_state(game, catalog))
        assert "Dangerous Plan" in text
        assert "{" not in text.split("## Your hand")[1].split("\n")[1]

    async def test_unknown_card_id_does_not_break(self, catalog):
        game = games.playing()
        game["myPlayer"]["hand"] = [games.card("ghost-1", "99-999")]
        assert game_state_markdown(await render_game_state(game, catalog)).strip()


class TestLocationsAndActivatedAbilities:
    """Regressions from the first game played with locations and items.

    Two blind spots surfaced at once. `canMove` had been removed from the
    capability map as invented, but it is real - it simply never appears until
    you control a location, so no earlier board produced it. And activated
    abilities never arrive as a `can` flag at all: they come as a structured
    `activatedAbilities` list, with the costs already resolved, which the
    renderer dropped on the floor. An item sitting there ready to use looked
    identical to one that could do nothing.
    """

    @staticmethod
    def _ability(**over):
        base = {
            "name": "HAUNTING PRESENCE",
            "inkCost": 0,
            "exertCost": True,
            "banishCost": False,
            "discardCost": 0,
            "canActivate": True,
        }
        base.update(over)
        return base

    async def test_boost_becomes_a_move_with_its_cost(self, catalog):
        """canBoost only appears with a Boost character and the ink to pay."""
        game = games.playing()
        game["availableActions"]["cards"][games.FIELD_ELSA].update(
            {"canBoost": True, "boostCost": 2}
        )
        payload = await render_game_state(game, catalog)
        move = next(m for m in payload["legal_moves"]
                    if m["args"].get("action_type") == "BOOST")
        assert move["args"]["payload"] == {"cardInstanceId": games.FIELD_ELSA}
        assert "2 ink" in move["why"]

    async def test_effective_strength_beats_the_printed_one(self, catalog):
        """A boosted Hercules reads 0/3 on the card and hits for 3. The
        engine knows; the catalogue does not."""
        game = games.playing()
        game["availableActions"]["cards"][games.FIELD_ELSA]["effectiveStrength"] = 4
        text = game_state_markdown(await render_game_state(game, catalog))
        assert "4/3, base 1" in text

    async def test_can_move_offers_each_location(self, catalog):
        game = games.playing()
        game["availableActions"]["cards"][games.FIELD_ELSA]["canMove"] = True
        payload = await render_game_state(game, catalog)
        moves = [m for m in payload["legal_moves"]
                 if m["args"].get("action_type") == "MOVE_TO_LOCATION"]
        assert len(moves) == 1
        assert moves[0]["args"]["payload"] == {
            "characterInstanceId": games.FIELD_ELSA,
            "locationInstanceId": games.LOCATION_CORONA,
        }

    async def test_can_move_is_not_reported_as_unmapped(self, catalog):
        game = games.playing()
        game["availableActions"]["cards"][games.FIELD_ELSA]["canMove"] = True
        payload = await render_game_state(game, catalog)
        assert not [m for m in payload["legal_moves"] if "canMove" in m["why"]]

    async def test_activatable_ability_becomes_a_move(self, catalog):
        game = games.playing()
        game["availableActions"]["cards"][games.LOCATION_CORONA] = {
            "activatedAbilities": [self._ability()]
        }
        payload = await render_game_state(game, catalog)
        move = next(m for m in payload["legal_moves"]
                    if m["args"].get("action_type") == "ACTIVATE_ABILITY")
        assert move["args"]["payload"] == {
            "cardInstanceId": games.LOCATION_CORONA,
            "abilityName": "HAUNTING PRESENCE",
        }

    async def test_blocked_ability_is_shown_but_not_offered(self, catalog):
        """It still has to be visible - knowing an ability exists and costs 3
        ink is what tells you to hold the ink for it."""
        game = games.playing()
        game["availableActions"]["cards"][games.LOCATION_CORONA] = {
            "activatedAbilities": [
                self._ability(name="OUT OF SIGHT", inkCost=3, exertCost=False,
                              canActivate=False, blockedReason="Not enough ink (need 3)")
            ]
        }
        payload = await render_game_state(game, catalog)
        assert not [m for m in payload["legal_moves"]
                    if m["args"].get("action_type") == "ACTIVATE_ABILITY"]
        text = game_state_markdown(payload)
        assert "OUT OF SIGHT" in text and "3 ink" in text
        assert "Not enough ink (need 3)" in text

    async def test_non_play_blocked_reasons_reach_the_agent(self, catalog):
        """Only playBlockedReason used to be surfaced, so "Already inked this
        turn" and "Ink dry (no Rush)" were rediscovered by being refused."""
        game = games.playing()
        game["availableActions"]["cards"][games.FIELD_RAPUNZEL]["questBlockedReason"] = (
            "Ink dry (no Rush)"
        )
        payload = await render_game_state(game, catalog)
        rapunzel = next(c for c in payload["me"]["field"]
                        if c["instance_id"] == games.FIELD_RAPUNZEL)
        assert rapunzel["blocked"] == "Ink dry (no Rush)"


class TestSingTogether:
    """Regression from a live game with a Sing Together song in hand.

    `hasSingTogether` reads like a capability but is a property of the card:
    the engine reports it true from turn one, with no singers on the board and
    `canSing` false. Treated as an action it produced a legal move that did
    not exist - the same class of mistake as advertising a canMove the bundle
    never sends. It belongs with the descriptive flags, and the numbers that
    come with it (singTogetherCost, totalAvailableSingerCost) are what a
    player actually needs.
    """

    @staticmethod
    def _song(game: dict, **actions: object) -> dict:
        """Turn the fixture song into a Sing Together song."""
        entry = game["availableActions"]["cards"][games.HAND_SONG]
        entry.update(
            {
                "hasSingTogether": True,
                "singTogetherCost": 6,
                "totalAvailableSingerCost": 0,
                "canSing": False,
                "validSingers": [],
            }
        )
        entry.update(actions)
        return game

    async def test_it_never_becomes_a_legal_move(self, catalog):
        payload = await render_game_state(self._song(games.playing()), catalog)
        assert not [m for m in payload["legal_moves"] if "hasSingTogether" in m["why"]]

    async def test_it_is_not_listed_as_a_capability(self, catalog):
        payload = await render_game_state(self._song(games.playing()), catalog)
        song = next(c for c in payload["me"]["hand"] if c["instance_id"] == games.HAND_SONG)
        assert "hasSingTogether" not in song.get("can", [])

    async def test_the_cost_and_the_progress_are_reported(self, catalog):
        game = self._song(games.playing(), totalAvailableSingerCost=4)
        payload = await render_game_state(game, catalog)
        song = next(c for c in payload["me"]["hand"] if c["instance_id"] == games.HAND_SONG)
        assert song["sing_together"] == {"cost": 6, "available": 4}

    async def test_the_progress_reaches_the_markdown(self, catalog):
        game = self._song(games.playing(), totalAvailableSingerCost=4)
        text = game_state_markdown(await render_game_state(game, catalog))
        assert "Sing Together 6 - singers ready 4/6" in text

    async def test_singing_together_offers_every_singer(self, catalog):
        """One singer is enough for an ordinary song and never enough for this
        one, so the suggested move has to carry all of them."""
        game = self._song(
            games.playing(),
            canSing=True,
            validSingers=[games.FIELD_ELSA, games.FIELD_RAPUNZEL],
            totalAvailableSingerCost=6,
        )
        payload = await render_game_state(game, catalog)
        sing = next(
            m for m in payload["legal_moves"]
            if m["tool"] == "duels_play_card"
            and m["args"].get("card_instance_id") == games.HAND_SONG
        )
        assert sing["args"]["singer_instance_ids"] == [games.FIELD_ELSA, games.FIELD_RAPUNZEL]

    async def test_an_ordinary_song_still_exerts_only_one_singer(self, catalog):
        """Guard on the other side: no Sing Together means no over-exerting."""
        payload = await render_game_state(games.playing(), catalog)
        sing = next(
            m for m in payload["legal_moves"]
            if m["tool"] == "duels_play_card"
            and m["args"].get("card_instance_id") == games.HAND_SONG
        )
        assert sing["args"]["singer_instance_ids"] == [games.FIELD_ELSA]


class TestDiscard:
    async def test_discard_contents_are_exposed(self, catalog):
        """Regression: the discard was a bare count, so a board wipe told you
        that something happened but never what did it."""
        payload = await render_game_state(games.playing(), catalog)
        names = {c["card"] for c in payload["me"]["discard"]}
        assert "Mushu - Stealthy Dragon" in names

    async def test_discard_is_grouped_with_counts(self, catalog):
        text = game_state_markdown(await render_game_state(games.playing(), catalog))
        assert "2x Mushu - Stealthy Dragon" in text

    async def test_both_discards_are_reported(self, catalog):
        text = game_state_markdown(await render_game_state(games.playing(), catalog))
        assert "Discard - you" in text and "Discard - opponent" in text

    async def test_count_still_matches_the_list(self, catalog):
        payload = await render_game_state(games.playing(), catalog)
        assert payload["me"]["discard_count"] == len(payload["me"]["discard"])

    async def test_empty_discard_adds_no_section(self, catalog):
        """The scoreboard always has a Discard column; the section should only
        appear when there is something in the pile."""
        game = games.playing()
        game["myPlayer"]["discard"] = []
        game["opponent"]["discard"] = []
        text = game_state_markdown(await render_game_state(game, catalog))
        assert "## Discard" not in text
