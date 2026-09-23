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
from src import telemetry
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

    async def test_applied_effects_are_shown(self, catalog):
        """The in-game card object carries no strength at all - only
        definitionId, damage, exerted, justPlayed, appliedEffects, cardsUnder
        and hasQuestedThisTurn. Base stats come from the catalogue and buffs
        live in appliedEffects, so a character whose printed stats are stale
        says so nowhere else. effectiveStrength would settle it, but the
        engine only computes that when a challenge is possible."""
        game = games.playing()
        elsa = next(c for c in game["myPlayer"]["field"]
                    if c["instanceId"] == games.FIELD_ELSA)
        elsa["appliedEffects"] = [{"name": "Challenger +2"}]
        text = game_state_markdown(await render_game_state(game, catalog))
        assert "Challenger +2" in text

    async def test_effects_of_an_unknown_shape_still_render(self, catalog):
        """appliedEffects was never observed populated, so the element shape
        is a guess. Anything readable beats silently dropping it."""
        game = games.playing()
        elsa = next(c for c in game["myPlayer"]["field"]
                    if c["instanceId"] == games.FIELD_ELSA)
        elsa["appliedEffects"] = ["STRENGTH_UP", {"type": "WARD_GRANTED"}]
        text = game_state_markdown(await render_game_state(game, catalog))
        assert "STRENGTH_UP" in text and "WARD_GRANTED" in text

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
        """Each side's discard is headed by whoever owns it, named when known."""
        text = game_state_markdown(await render_game_state(games.playing(), catalog))
        assert "Discard - you" in text and "Discard - Bot (Normal)" in text

    async def test_an_unnamed_opponents_discard_is_still_headed(self, catalog):
        text = game_state_markdown(
            await render_game_state(games.playing(playerNames=None), catalog)
        )
        assert "Discard - Opponent" in text

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


class TestTheClock:
    """Games against people are timed; nothing else in the state says so.

    A won game was carried to 15-21 without the clock ever being visible.
    `turnGateState` looks like it might hold this and does not - it counts
    inks and cards played. The clock is `timerView`, and it is absent in bot
    games, which are untimed.
    """

    def timed(self, **view):
        game = games.playing()
        return {**game, "timerView": {"serverTimestamp": 1789944585109, **view}}

    async def test_a_bot_game_shows_no_clock(self, catalog):
        payload = await render_game_state(games.playing(), catalog)
        assert payload["clock"] is None
        assert "Clock" not in game_state_markdown(payload)

    async def test_a_timerview_of_nulls_is_also_no_clock(self, catalog):
        """Whether an untimed game omits timerView or sends it empty, the
        answer is the same - do not print a clock that does not exist."""
        payload = await render_game_state(
            self.timed(myTimeRemainingMs=None, opponentTimeRemainingMs=None), catalog
        )
        assert payload["clock"] is None

    async def test_both_clocks_are_shown(self, catalog):
        payload = await render_game_state(
            self.timed(myTimeRemainingMs=114623, opponentTimeRemainingMs=120000), catalog
        )
        assert payload["clock"]["my_ms"] == 114623
        line = game_state_markdown(payload)
        assert "you 1:54" in line and "opponent 2:00" in line

    async def test_a_running_clock_says_so(self, catalog):
        payload = await render_game_state(
            self.timed(
                myTimeRemainingMs=90000, opponentTimeRemainingMs=120000, myTimerTicking=True
            ),
            catalog,
        )
        assert payload["clock"]["my_clock_running"] is True
        assert "your clock is running" in game_state_markdown(payload)

    async def test_a_nearly_dead_clock_is_escalated(self, catalog):
        """The whole point of showing it is the moment it matters."""
        payload = await render_game_state(
            self.timed(
                myTimeRemainingMs=12000, opponentTimeRemainingMs=120000, myTimerTicking=True
            ),
            catalog,
        )
        text = game_state_markdown(payload)
        assert "you 0:12" in text
        assert "eliminates you" in text

    async def test_the_opponents_clock_is_not_escalated(self, catalog):
        """Their clock running low is their problem, not an alarm for us."""
        payload = await render_game_state(
            self.timed(
                myTimeRemainingMs=120000,
                opponentTimeRemainingMs=5000,
                opponentTimerTicking=True,
            ),
            catalog,
        )
        text = game_state_markdown(payload)
        assert "opponent's clock is running" in text
        assert "loses the game" not in text

    @pytest.mark.parametrize(
        ("ms", "shown"),
        [(0, "0:00"), (999, "0:00"), (1000, "0:01"), (59_999, "0:59"), (120_000, "2:00"),
         (-5000, "0:00"), (None, "?")],
    )
    async def test_time_is_floored_and_never_negative(self, ms, shown):
        from src.render import _mmss

        assert _mmss(ms) == shown


class TestATableOfMoreThanTwo:
    """A table seats two to four, and every one of them is in the race.

    Reading only `opponent` did not crash and did not warn - it rendered a
    four-player Coconut game as a duel, with two players' boards and lore
    simply absent. Everything here exists to make that failure loud.
    """

    async def test_every_opponent_reaches_the_payload(self, catalog):
        payload = await render_game_state(games.coconut_table(players=4), catalog)
        assert [o["name"] for o in payload["opponents"]] == ["Joe", "Ewaldo", "DRobb"]
        assert payload["player_count"] == 4

    async def test_every_opponent_reaches_the_scoreboard(self, catalog):
        text = game_state_markdown(await render_game_state(games.coconut_table(), catalog))
        for name, lore in [("Joe", 6), ("Ewaldo", 9), ("DRobb", 12)]:
            assert f"| {name} | {lore} |" in text

    async def test_a_table_of_two_is_still_a_table(self, catalog):
        """Coconut is a format, not a player count."""
        payload = await render_game_state(games.coconut_table(players=2), catalog)
        assert len(payload["opponents"]) == 1
        assert payload["player_count"] == 2
        assert payload["lore_to_win"] == 25

    async def test_a_table_of_three(self, catalog):
        payload = await render_game_state(games.coconut_table(players=3), catalog)
        assert [o["name"] for o in payload["opponents"]] == ["Joe", "Ewaldo"]

    async def test_each_opponent_gets_a_board(self, catalog):
        text = game_state_markdown(await render_game_state(games.coconut_table(), catalog))
        assert text.count(" board\n") == 4  # mine plus three

    async def test_targets_belonging_to_the_third_player_are_named(self, catalog):
        """Otherwise a challenge target from seat 4 prints as a bare UUID."""
        payload = await render_game_state(games.coconut_table(), catalog)
        blob = str(payload["legal_moves"])
        assert "opp4-pete" not in blob or "Pete" in blob

    async def test_the_singular_opponent_still_points_somewhere(self, catalog):
        """The old contract keeps working; it just stops being the whole story."""
        payload = await render_game_state(games.coconut_table(), catalog)
        assert payload["opponent"] == payload["opponents"][0]

    async def test_a_duel_is_unchanged(self, catalog):
        payload = await render_game_state(games.playing(), catalog)
        assert len(payload["opponents"]) == 1
        assert payload["player_count"] == 2
        assert payload["opponent"]["name"] == "Bot (Normal)"


class TestElimination:
    """A player can be out while the game goes on without them."""

    async def test_an_eliminated_opponent_is_marked(self, catalog):
        text = game_state_markdown(
            await render_game_state(games.coconut_table(eliminated=(3,)), catalog)
        )
        assert "Ewaldo (ELIMINATED)" in text
        assert "Joe (ELIMINATED)" not in text

    async def test_being_eliminated_myself_is_marked(self, catalog):
        game = games.coconut_table()
        game["myPlayer"]["eliminated"] = True
        text = game_state_markdown(await render_game_state(game, catalog))
        assert "**You** (ELIMINATED)" in text

    async def test_nobody_is_eliminated_by_default(self, catalog):
        text = game_state_markdown(await render_game_state(games.coconut_table(), catalog))
        assert "ELIMINATED" not in text


class TestTheGoal:
    """Twenty lore is a default, not a rule."""

    async def test_a_normal_game_still_wants_twenty(self, catalog):
        payload = await render_game_state(games.playing(), catalog)
        assert payload["lore_to_win"] == 20

    async def test_coconut_wants_twenty_five(self, catalog):
        payload = await render_game_state(games.coconut_table(), catalog)
        assert payload["lore_to_win"] == 25
        assert "first to 25 lore" in game_state_markdown(payload)

    async def test_pack_rush_wants_fifteen(self, catalog):
        payload = await render_game_state(games.playing(gameVariant="packRush"), catalog)
        assert payload["lore_to_win"] == 15

    async def test_a_table_may_overrule_its_variant(self, catalog):
        payload = await render_game_state(games.coconut_table(loreToWin=30), catalog)
        assert payload["lore_to_win"] == 30

    async def test_a_nonsense_threshold_falls_back(self, catalog):
        payload = await render_game_state(games.coconut_table(loreToWin=0), catalog)
        assert payload["lore_to_win"] == 25

    async def test_the_variant_is_named_in_the_header(self, catalog):
        text = game_state_markdown(await render_game_state(games.coconut_table(), catalog))
        assert "coconut" in text


class TestTheCoconut:
    """The card that is in play from turn one and never in the deck."""

    async def test_my_coconut_is_a_zone_of_its_own(self, catalog):
        text = game_state_markdown(await render_game_state(games.coconut_table(), catalog))
        assert "## Your Coconut" in text

    async def test_each_opponent_coconut_is_shown(self, catalog):
        text = game_state_markdown(await render_game_state(games.coconut_table(), catalog))
        assert "## Joe Coconut" in text and "## DRobb Coconut" in text

    async def test_a_game_without_coconuts_shows_none(self, catalog):
        payload = await render_game_state(games.playing(), catalog)
        assert payload["me"]["coconut"] == []
        assert "Coconut" not in game_state_markdown(payload)


class TestTheWinner:
    """With four players, 'player 4' is not an answer anybody can act on."""

    async def test_the_winner_is_named_when_known(self, catalog):
        game = games.coconut_table()
        game["status"], game["winner"] = "finished", 4
        text = game_state_markdown(await render_game_state(game, catalog))
        assert "DRobb (player 4)" in text

    async def test_an_unknown_winner_still_falls_back_to_the_number(self, catalog):
        game = games.coconut_table()
        game["status"], game["winner"] = "finished", 9
        text = game_state_markdown(await render_game_state(game, catalog))
        assert "winner: player 9" in text


class TestWhereACharacterStands:
    """A location changes what happens to whoever is standing on it."""

    def at_corona(self):
        game = games.playing()
        game["myPlayer"]["field"][0]["locationInstanceId"] = games.LOCATION_CORONA
        return game

    async def test_the_location_is_named_not_just_referenced(self, catalog):
        """The id is on the character; the name is on a card in items."""
        text = game_state_markdown(await render_game_state(self.at_corona(), catalog))
        assert "at Corona" in text

    async def test_the_id_survives_into_the_payload(self, catalog):
        payload = await render_game_state(self.at_corona(), catalog)
        assert payload["me"]["field"][0]["location_instance_id"] == games.LOCATION_CORONA

    async def test_a_character_standing_nowhere_says_nothing(self, catalog):
        payload = await render_game_state(games.playing(), catalog)
        assert "location" not in payload["me"]["field"][0]

    async def test_an_unknown_location_id_is_not_invented(self, catalog):
        game = games.playing()
        game["myPlayer"]["field"][0]["locationInstanceId"] = "inst-nowhere"
        payload = await render_game_state(game, catalog)
        assert "location" not in payload["me"]["field"][0]

    async def test_having_quested_already_is_shown(self, catalog):
        game = games.playing()
        game["myPlayer"]["field"][0]["hasQuestedThisTurn"] = True
        text = game_state_markdown(await render_game_state(game, catalog))
        assert "already quested" in text


class TestAGameAlreadyWon:
    """A stalled game does not end itself, and waiting does nothing."""

    def timed(self, **view):
        game = games.playing()
        return {**game, "timerView": {"serverTimestamp": 1, **view}}

    async def test_a_timeout_is_announced(self, catalog):
        text = game_state_markdown(
            await render_game_state(self.timed(canDeclareVictory=True), catalog)
        )
        assert "claim this game right now" in text and "timeout" in text
        assert "duels_claim_victory" in text

    async def test_an_absence_is_announced(self, catalog):
        text = game_state_markdown(
            await render_game_state(self.timed(canClaimAfkVictory=True), catalog)
        )
        assert "absence" in text

    async def test_a_ping_is_offered_when_nothing_is_claimable_yet(self, catalog):
        text = game_state_markdown(
            await render_game_state(self.timed(canPingOpponent=True), catalog)
        )
        assert "kind='ping'" in text
        assert "claim this game right now" not in text

    async def test_a_normal_game_says_none_of_it(self, catalog):
        text = game_state_markdown(
            await render_game_state(self.timed(myTimeRemainingMs=60000), catalog)
        )
        assert "claim" not in text.lower()

    async def test_the_active_clock_is_identified(self, catalog):
        """With three opponents, "the opponent's clock" names nobody."""
        payload = await render_game_state(
            self.timed(activePlayer=3, myTimeRemainingMs=60000), catalog
        )
        assert payload["clock"]["active_player"] == 3

    async def test_how_many_have_run_out_is_carried(self, catalog):
        payload = await render_game_state(
            self.timed(opponentZeroCount=2, myTimeRemainingMs=60000), catalog
        )
        assert payload["clock"]["opponents_out_of_time"] == 2

    async def test_being_the_one_pinged_is_visible(self, catalog):
        """Ignoring it hands the opponent the claim."""
        payload = await render_game_state(self.timed(wasAfkPinged=True), catalog)
        assert payload["clock"]["was_pinged"] is True


class TestTakingItBack:
    """Whether undo exists at all is the table's rule, not the player's."""

    async def test_an_available_undo_is_offered(self, catalog):
        game = games.playing(canRequestUndo=True, allowFreeUndo=True)
        text = game_state_markdown(await render_game_state(game, catalog))
        assert "taken back (free)" in text and "duels_undo" in text

    async def test_a_timed_undo_says_what_it_costs(self, catalog):
        """The wire sends milliseconds. Read as seconds it said "30000s"."""
        game = games.playing(canRequestUndo=True, undoTimeCost=30000)
        payload = await render_game_state(game, catalog)
        assert payload["undo"]["time_cost_ms"] == 30000
        assert "0:30 off your clock" in game_state_markdown(payload)

    async def test_a_table_with_undo_off_says_nothing(self, catalog):
        payload = await render_game_state(games.playing(), catalog)
        assert payload["undo"] is None
        assert "duels_undo" not in game_state_markdown(payload)

    async def test_the_rest_of_the_undo_block_survives(self, catalog):
        game = games.playing(
            canCancelInProgressAbility=True, undoDeclineLimitReached=True
        )
        payload = await render_game_state(game, catalog)
        assert payload["undo"]["can_cancel_ability"] is True
        assert payload["undo"]["declines_exhausted"] is True


class TestVotingSomebodyOut:
    """Only a table has this: with one opponent there is nobody to vote with."""

    def voting(self, **vote):
        return games.coconut_table(
            removalVoteCalled={"targetPlayer": 3, "votesFor": 1,
                               "votesNeeded": 2, **vote}
        )

    async def test_a_running_vote_is_impossible_to_miss(self, catalog):
        text = game_state_markdown(await render_game_state(self.voting(), catalog))
        assert "vote is running to remove player 3" in text
        assert "duels_removal_vote" in text

    async def test_having_voted_already_is_said(self, catalog):
        text = game_state_markdown(
            await render_game_state(self.voting(hasVoted=True), catalog)
        )
        assert "already voted" in text

    async def test_the_tally_reaches_the_payload(self, catalog):
        payload = await render_game_state(self.voting(), catalog)
        assert payload["removal_vote"]["votes_for"] == 1
        assert payload["removal_vote"]["votes_needed"] == 2

    async def test_a_duel_has_no_vote(self, catalog):
        payload = await render_game_state(games.playing(), catalog)
        assert payload["removal_vote"] is None


class TestWhyTheGameEnded:
    """Lore, a concession, a timeout and an absence all read identically."""

    async def test_the_reason_is_named(self, catalog):
        game = games.coconut_table()
        game["status"], game["winner"] = "finished", 4
        game["victoryReason"] = "timeout"
        text = game_state_markdown(await render_game_state(game, catalog))
        assert "Game over by timeout" in text

    async def test_a_game_without_one_still_reads(self, catalog):
        game = games.coconut_table()
        game["status"], game["winner"] = "finished", 4
        text = game_state_markdown(await render_game_state(game, catalog))
        assert "Game over -" in text


class TestAPromptNamesItsCards:
    """A prompt lists instance ids and nothing else.

    "Choose one of these four" arrived as four UUIDs and was answered by
    picking the first, twice in one ranked game - once on Imperial Invitation
    and once on Develop Your Brain. Both were blind.
    """

    def asking(self, ids, **extra):
        prompt = {"id": "p1", "player": 1, "type": "select_card",
                  "cardInstanceIds": list(ids), **extra}
        return games.playing(pendingPrompts=[prompt])

    async def test_a_card_on_the_board_is_named(self, catalog):
        text = game_state_markdown(
            await render_game_state(self.asking([games.FIELD_ELSA]), catalog)
        )
        assert "The cards it is asking about" in text
        assert "Elsa" in text.split("The cards it is asking about")[1]

    async def test_a_card_in_hand_is_named(self, catalog):
        payload = await render_game_state(self.asking([games.HAND_MUSHU]), catalog)
        assert "Mushu" in payload["prompt_cards"][games.HAND_MUSHU]

    async def test_a_revealed_card_is_named(self, catalog):
        """Imperial Invitation's four live here and nowhere else."""
        game = self.asking(["rev-1"])
        game["myPlayer"]["revealedCardsThisTurn"] = [games.card("rev-1", "10-45")]
        payload = await render_game_state(game, catalog)
        assert payload["prompt_cards"]["rev-1"]
        assert payload["me"]["revealed"][0]["instance_id"] == "rev-1"

    async def test_an_opponents_card_is_named(self, catalog):
        payload = await render_game_state(self.asking([games.OPP_PETE]), catalog)
        assert games.OPP_PETE in payload["prompt_cards"]

    async def test_targets_are_named_too_not_just_card_lists(self, catalog):
        game = games.playing(pendingPrompts=[{
            "id": "p1", "type": "select_target", "validTargets": [games.OPP_PETE],
        }])
        payload = await render_game_state(game, catalog)
        assert games.OPP_PETE in payload["prompt_cards"]

    async def test_an_id_in_no_zone_is_reported_not_hidden(self, catalog):
        """Better to say it cannot be named than to print a bare UUID."""
        payload = await render_game_state(self.asking(["inst-nowhere"]), catalog)
        assert payload["prompt_cards_unknown"] == ["inst-nowhere"]
        assert "cannot be named" in game_state_markdown(payload)

    async def test_a_game_with_no_prompt_has_no_legend(self, catalog):
        payload = await render_game_state(games.playing(), catalog)
        assert "prompt_cards" not in payload


class TestACoconutGameWithoutCoconuts:
    """Played one to the end without noticing.

    The seat said hasCoconut, the deck carried coconut-004, the game reported
    gameVariant "coconut" and a 25-lore goal - and no player had a coconutCard
    on the wire at all. 340 log entries never mentioned one. The free play
    simply never appeared in a legal move, which reads as "not available now"
    rather than "the card does not exist".
    """

    def stripped(self, players=2):
        """A Coconut game the way the server actually sent it."""
        game = games.coconut_table(players=players)
        game["myPlayer"].pop("coconutCard", None)
        for seat in game["opponents"]:
            seat.pop("coconutCard", None)
        return game

    async def test_the_absence_is_counted(self, catalog):
        payload = await render_game_state(self.stripped(), catalog)
        assert payload["coconuts_in_play"] == 0

    async def test_the_absence_is_announced(self, catalog):
        text = game_state_markdown(await render_game_state(self.stripped(), catalog))
        assert "No Coconut is in play" in text
        assert "cannot arrive" in text

    async def test_a_working_coconut_game_says_nothing_of_the_sort(self, catalog):
        payload = await render_game_state(games.coconut_table(), catalog)
        assert payload["coconuts_in_play"] == 4
        assert "No Coconut is in play" not in game_state_markdown(payload)

    async def test_a_partial_absence_is_still_counted(self, catalog):
        """One player having one is not the same as the format working."""
        game = games.coconut_table()
        game["myPlayer"].pop("coconutCard", None)
        payload = await render_game_state(game, catalog)
        assert payload["coconuts_in_play"] == 3
        assert "No Coconut is in play" not in game_state_markdown(payload)

    async def test_a_normal_game_is_not_asked_the_question(self, catalog):
        payload = await render_game_state(games.playing(), catalog)
        assert "coconuts_in_play" not in payload


class TestWhatAFourPlayerCoconutFound:
    """Five fields the wire had been sending all along.

    Every one was found by `src/telemetry.py` during a single ranked Coconut
    game - which is the first time the recorder paid for itself.
    """

    async def test_a_removal_vote_is_seen(self, catalog):
        """The key is `removalVoteCall`.

        We read `removalVoteCalled`, a guess with a trailing "d" the site has
        never sent, so eight real votes in one game were invisible. The vote
        is timed, so not seeing it is a silent abstention.
        """
        game = games.coconut_table()
        game["removalVoteCall"] = {
            "targetPlayer": 2, "votesFor": 1, "votesNeeded": 3, "calledBy": 1,
        }
        payload = await render_game_state(game, catalog)
        assert payload["removal_vote"]["target_player"] == 2
        assert payload["removal_vote"]["votes_needed"] == 3

    async def test_an_unfamiliar_vote_shape_is_handed_over_whole(self, catalog):
        """Only the outer key is confirmed; the inner names are still guesses.

        A shell with every field None would read as 'no vote in progress',
        which is the same failure again in a new costume.
        """
        game = games.coconut_table()
        game["removalVoteCall"] = {"somethingNew": 7}
        payload = await render_game_state(game, catalog)
        assert payload["removal_vote"]["raw"] == {"somethingNew": 7}

    async def test_no_vote_stays_none(self, catalog):
        payload = await render_game_state(games.coconut_table(), catalog)
        assert payload["removal_vote"] is None

    async def test_a_challenged_character_says_so(self, catalog):
        """Cards read this: 'if this character was challenged this turn'."""
        game = games.playing()
        game["myPlayer"]["field"][0].update({
            "wasChallengedThisTurn": True,
            "lastDamageWasChallenge": True,
            "lastDamageSource": "inst-opp-pete",
        })
        payload = await render_game_state(game, catalog)
        card = payload["me"]["field"][0]
        assert card["challenged_this_turn"] is True
        assert card["last_damage_from_challenge"] is True
        assert card["last_damage_source"] == "inst-opp-pete"

    async def test_a_revealed_hand_is_shown(self, catalog):
        """We paid a card for this information and then threw it away.

        Mowgli - Man Cub makes an opponent reveal their hand; the wire sent it
        under `revealedHand` and nothing read it.
        """
        game = games.coconut_table(players=2)
        game["opponents"][0]["revealedHand"] = [
            games.card("inst-revealed-1", "10-71"),
        ]
        payload = await render_game_state(game, catalog)
        assert payload["opponents"][0]["revealed_hand"]
        assert "revealed" in game_state_markdown(payload).lower()

    async def test_turn_counters_are_carried(self, catalog):
        """'If a character was banished this turn' needs the count."""
        game = games.playing()
        game["turnGateState"] = {"charactersBanishedThisTurn": {"1": 2}}
        payload = await render_game_state(game, catalog)
        assert payload["turn_counters"]["charactersBanishedThisTurn"] == {"1": 2}


class TestWhatTheMoanaGameFound:
    """Seven things a four-player Coconut game with the Moana deck showed."""

    # --- removalVoteCall is an eligibility timer ------------------------
    ELIGIBLE = {"targetPlayer": 3, "targetName": "Flower", "availableAt": 5_000}

    def eligible(self, now: int) -> dict:
        game = games.coconut_table()
        game["removalVoteCall"] = dict(self.ELIGIBLE)
        game["timerView"] = {"serverTimestamp": now}
        return game

    async def test_an_eligibility_timer_is_not_a_vote(self, catalog):
        """The wire names whoever is acting and when a vote could open.

        Read as a running vote it announced "Answer it" on every turn of a
        four-player game, and answering was refused - there was nothing to
        answer.
        """
        payload = await render_game_state(self.eligible(now=1_000), catalog)
        assert payload["removal_vote"]["kind"] == "eligible"
        assert payload["removal_vote"]["available_now"] is False
        text = game_state_markdown(payload)
        assert "vote is running" not in text
        assert "gone quiet" not in text

    async def test_an_open_window_says_a_vote_can_be_called(self, catalog):
        payload = await render_game_state(self.eligible(now=9_000), catalog)
        assert payload["removal_vote"]["available_now"] is True
        text = game_state_markdown(payload)
        assert "Flower has gone quiet" in text and "action='call'" in text
        assert "vote is running" not in text

    async def test_a_real_vote_still_reads_as_one(self, catalog):
        game = games.coconut_table()
        game["removalVoteCall"] = {"targetPlayer": 2, "votesFor": 1, "votesNeeded": 2}
        payload = await render_game_state(game, catalog)
        assert payload["removal_vote"]["kind"] == "running"
        assert "vote is running" in game_state_markdown(payload)

    # --- ink from the discard -------------------------------------------
    def inking_from_discard(self, can_ink: bool = True) -> dict:
        game = games.playing()
        game["availableActions"]["canInkFromDiscard"] = True
        game["availableActions"]["canInk"] = can_ink
        return game

    async def test_ink_from_the_discard_is_offered(self, catalog):
        """Moana: "you can ink cards from your discard". The move is the
        ordinary INK_CARD with the discard card's id, and nothing offered it."""
        payload = await render_game_state(self.inking_from_discard(), catalog)
        offered = {
            m["args"]["card_instance_id"]
            for m in payload["legal_moves"]
            if m["tool"] == "duels_ink_card"
        }
        assert "disc-3" in offered          # 12-77 is inkable
        assert "disc-1" not in offered      # 10-103 is not

    async def test_ink_from_the_discard_needs_an_ink_action_left(self, catalog):
        payload = await render_game_state(self.inking_from_discard(can_ink=False), catalog)
        assert not [
            m for m in payload["legal_moves"]
            if m["tool"] == "duels_ink_card"
            and m["args"]["card_instance_id"].startswith("disc-")
        ]

    async def test_moanas_property_is_not_a_move(self, catalog):
        """isDiscardInkSource describes Moana; it once became an
        "<ACTION TYPE>" placeholder the agent could not fill."""
        game = games.playing()
        game["availableActions"]["cards"][games.FIELD_ELSA]["isDiscardInkSource"] = True
        payload = await render_game_state(game, catalog)
        assert not [
            m for m in payload["legal_moves"]
            if m["args"].get("action_type") == "<ACTION TYPE>"
        ]

    # --- cards you are looking at ---------------------------------------
    async def test_the_cards_you_are_looking_at_are_named(self, catalog):
        """Besties, Assemble! and Gaston look at the top of the deck. Those
        cards travel in revealedCards, so every such prompt named nothing."""
        game = games.with_prompt({
            "id": "p-look", "player": 1, "type": "select_card",
            "message": "useAbility", "cardInstanceIds": ["look-1"],
            "minSelect": 0, "maxSelect": 1,
        })
        game["myPlayer"]["revealedCards"] = [games.card("look-1", "10-71")]
        payload = await render_game_state(game, catalog)
        assert payload["me"]["looking_at"][0]["instance_id"] == "look-1"
        assert "look-1" in payload["prompt_cards"]
        assert "look-1" not in payload["prompt_cards_unknown"]
        assert "Cards you are looking at" in game_state_markdown(payload)

    # --- the Coconut has a name -----------------------------------------
    async def test_a_coconut_is_named_from_the_pool(self, catalog):
        """/api/cards does not serve Coconuts; they rendered as bare ids."""
        payload = await render_game_state(games.coconut_table(), catalog)
        mine = payload["me"]["coconut"][0]
        assert mine["card"] == "Mr. Incredible - Super Strong"
        assert mine.get("text")
        assert "**Mr. Incredible - Super Strong**" in game_state_markdown(payload)

    # --- sing restrictions ----------------------------------------------
    async def test_sing_restrictions_are_handed_over(self, catalog):
        """Ursula - Sea Witch Queen: other characters can't exert to sing.
        The shape has never been seen with a value, so it is passed through."""
        game = games.playing()
        game["activeSingRestrictions"] = [{"source": "ursula"}]
        payload = await render_game_state(game, catalog)
        assert payload["sing_restrictions"] == {"active": [{"source": "ursula"}]}
        assert "Singing is restricted" in game_state_markdown(payload)

    async def test_no_restriction_says_nothing(self, catalog):
        payload = await render_game_state(games.playing(), catalog)
        assert payload["sing_restrictions"] is None


class TestScenario:
    """A playground is a scenario, and a scenario is not a game.

    It holds the Coconut on the board but does not grant its ability: the
    wire sends `availableActions.cards[<coconut>] = {}` while a cost-2
    character in hand still reports `playBlockedReason: "Need 2 ink"`, even
    though the Coconut reads "you may play a character with cost 2 or less
    for free". Saying so keeps the sandbox from reading as a broken game.
    """

    async def test_a_scenario_is_announced(self, catalog):
        game = games.coconut_table(players=2)
        game["isScenario"] = True
        text = game_state_markdown(await render_game_state(game, catalog))
        assert "scenario" in text
        assert "nothing here counts" in text
        # The old header blamed the sandbox for withholding Coconut abilities.
        # A real game did the same, so saying so would be a false claim.
        assert "format abilities" not in text

    async def test_a_real_game_is_not_called_a_scenario(self, catalog):
        text = game_state_markdown(await render_game_state(games.playing(), catalog))
        assert "scenario" not in text


class TestTelemetry:
    """Finding unread fields by watching a game you are playing does not work.

    Duels.ink allows one game socket per player and evicts the older with
    `4008 Stale connection`, so a watcher and the server's own connection take
    turns killing each other - the watcher only survives while the server is
    idle, which is while you are *not* playing. Reading what the server already
    received sidesteps the whole problem.
    """

    def test_it_is_off_unless_switched_on(self, tmp_path, monkeypatch):
        monkeypatch.delenv(telemetry.ENV_VAR, raising=False)
        target = tmp_path / "off.jsonl"
        telemetry.note({"turnNumber": 1, "surpriseKey": 1})
        assert not target.exists()

    def test_an_unread_top_level_key_is_recorded(self, tmp_path, monkeypatch):
        target = tmp_path / "seen.jsonl"
        monkeypatch.setenv(telemetry.ENV_VAR, str(target))
        telemetry.note({"turnNumber": 3, "somethingNobodyReads": True})
        rows = target.read_text(encoding="utf-8").splitlines()
        assert len(rows) == 1
        assert "somethingNobodyReads" in rows[0]

    def test_a_fully_understood_state_writes_nothing(self, tmp_path, monkeypatch):
        """Otherwise the file is a log of every turn rather than of surprises."""
        target = tmp_path / "quiet.jsonl"
        monkeypatch.setenv(telemetry.ENV_VAR, str(target))
        telemetry.note({"id": "g", "status": "playing", "turnNumber": 2,
                        "myPlayer": {"lore": 0, "hand": []}})
        assert not target.exists()

    def test_the_stock_fixture_is_not_fully_understood(self, tmp_path, monkeypatch):
        """`hasInkedThisTurn` rides on every real state and nothing reads it.

        Recorded here rather than added to the known set, because pretending to
        read a field is how it stays unread.
        """
        target = tmp_path / "fixture.jsonl"
        monkeypatch.setenv(telemetry.ENV_VAR, str(target))
        telemetry.note(games.playing())
        assert "hasInkedThisTurn" in target.read_text(encoding="utf-8")

    def test_unread_card_and_prompt_keys_are_found(self, tmp_path, monkeypatch):
        target = tmp_path / "cards.jsonl"
        monkeypatch.setenv(telemetry.ENV_VAR, str(target))
        game = games.playing()
        game["myPlayer"]["field"][0]["mysteryCardField"] = 1
        game["pendingPrompts"] = [{"id": "p", "type": "boolean", "cardBadges": {}}]
        telemetry.note(game)
        row = target.read_text(encoding="utf-8")
        assert "mysteryCardField" in row and "cardBadges" in row

    def test_a_broken_path_never_breaks_a_turn(self, monkeypatch):
        """A telemetry file is not worth losing a move in a timed game."""
        monkeypatch.setenv(telemetry.ENV_VAR, "/nowhere/at/all/x.jsonl")
        telemetry.note({"turnNumber": 1, "surprise": 1})

    def test_the_summary_counts_repeats(self, tmp_path, monkeypatch):
        target = tmp_path / "many.jsonl"
        monkeypatch.setenv(telemetry.ENV_VAR, str(target))
        for _ in range(3):
            telemetry.note({"turnNumber": 1, "repeatedUnknown": True})
        assert telemetry.summarise(str(target))["top"]["repeatedUnknown"] == 3

    async def test_reading_a_state_records_through_the_renderer(
        self, catalog, tmp_path, monkeypatch
    ):
        target = tmp_path / "wired.jsonl"
        monkeypatch.setenv(telemetry.ENV_VAR, str(target))
        game = games.playing()
        game["somethingBrandNew"] = 1
        await render_game_state(game, catalog)
        assert "somethingBrandNew" in target.read_text(encoding="utf-8")
