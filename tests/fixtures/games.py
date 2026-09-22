"""Game state fixtures, one per phase.

Shapes follow docs/PROTOCOL.md, which was written from live traffic. Builders
rather than literals, so a test can tweak one field without copying 5 KB of
JSON around.
"""

from typing import Any, Optional

GAME_ID = "01a0bc5a-896c-715e-a447-eb939dc3707a"

# Instance ids are arbitrary but stable, so assertions can name them.
HAND_FLOTSAM = "inst-hand-flotsam"
HAND_SONG = "inst-hand-song"
HAND_MUSHU = "inst-hand-mushu"
FIELD_ELSA = "inst-field-elsa"
FIELD_RAPUNZEL = "inst-field-rapunzel"
OPP_PETE = "inst-opp-pete"
LOCATION_CORONA = "inst-loc-corona"


def card(instance_id: str, definition_id: str, **overrides: Any) -> dict:
    """One card instance as the game engine reports it."""
    return {
        "definitionId": definition_id,
        "instanceId": instance_id,
        "damage": 0,
        "exerted": False,
        "justPlayed": False,
        "appliedEffects": [],
        "cardsUnder": [],
        **overrides,
    }


def base_game(**overrides: Any) -> dict:
    """A minimal but structurally complete game state."""
    game: dict[str, Any] = {
        "id": GAME_ID,
        "stateVersion": 12,
        "currentPlayer": 1,
        "turnNumber": 3,
        "hasInkedThisTurn": False,
        "status": "playing",
        "winner": None,
        "viewingAs": 1,
        "isBotGame": True,
        "playerNames": {"1": "You", "2": "Bot (Normal)"},
        "firstPlayer": 1,
        "pendingPrompts": [],
        "opponentHasPendingPrompts": False,
        "myPlayer": {
            "hand": [
                card(HAND_FLOTSAM, "10-71"),
                card(HAND_SONG, "11-97"),
                card(HAND_MUSHU, "10-103"),
            ],
            "field": [
                card(FIELD_ELSA, "10-45"),
                card(FIELD_RAPUNZEL, "13-80", exerted=True, damage=1),
            ],
            "items": [card(LOCATION_CORONA, "13-12")],
            "inkwell": [card("ink-1", "12-133"), card("ink-2", "12-133")],
            # Duplicado de propósito: o descarte agrupa por carta com contagem.
            "discard": [
                card("disc-1", "10-103"),
                card("disc-2", "10-103"),
                card("disc-3", "12-77"),
            ],
            "lore": 4,
            "inkDrops": 0,
            "deckCount": 48,
        },
        "opponent": {
            "handCount": 5,
            "deckCount": 50,
            "field": [card(OPP_PETE, "5-195", exerted=True)],
            "items": [],
            "inkwell": [card("opp-ink-1", "12-133")],
            "discard": [card("opp-disc-1", "5-195")],
            "lore": 2,
        },
        "availableActions": {
            "canInk": True,
            "canEndTurn": True,
            "availableInk": 2,
            "inkDrops": 0,
            "remainingInkActions": 1,
            "canInkFromDiscard": False,
            "canPlayFromDiscard": False,
            "cards": {
                HAND_FLOTSAM: {"canInk": True, "canPlay": False, "playBlockedReason": "Need 3 ink",
                               "canAffordInkCost": False},
                HAND_SONG: {"canInk": True, "canPlay": False, "playBlockedReason": "Need 4 ink",
                            "canSing": True, "validSingers": [FIELD_ELSA], "canAffordInkCost": False},
                HAND_MUSHU: {"canInk": False, "canPlay": False, "playBlockedReason": "Need 5 ink"},
                FIELD_ELSA: {"canQuest": True, "canChallenge": True, "canBeSinger": True},
                FIELD_RAPUNZEL: {"canQuest": False, "canChallenge": False},
                LOCATION_CORONA: {},
            },
        },
    }
    game.update(overrides)
    return game


def coin_toss(*, mine: bool = True) -> dict:
    """The coin toss, either my choice or the opponent's."""
    game = base_game(status="coin_toss", turnNumber=1, stateVersion=3)
    game["coinToss"] = {
        "isAnimating": False,
        "result": {"winner": 1 if mine else 2, "result": "heads", "timestamp": 1789868537244},
        "youWonToss": mine,
        "chooser": 1 if mine else 2,
        "isYourChoice": mine,
    }
    return game


def mulligan(*, my_turn_to_act: bool = True) -> dict:
    """The mulligan, either waiting on me or on the opponent."""
    game = base_game(status="mulligan", turnNumber=1, stateVersion=5)
    game["mulliganState"] = {
        "currentMulliganPlayer": 1,
        "myDone": not my_turn_to_act,
        "opponentDone": False,
        "canSubmit": True,
    }
    return game


def playing(**overrides: Any) -> dict:
    """Mid-game, my turn, several things playable."""
    return base_game(**overrides)


def opponents_turn() -> dict:
    return base_game(currentPlayer=2)


def with_prompt(prompt: dict) -> dict:
    game = base_game()
    game["pendingPrompts"] = [prompt]
    return game


def finished(*, winner: int = 1) -> dict:
    return base_game(status="finished", winner=winner)


def stuck() -> dict:
    """My turn, but nothing playable and the turn cannot be ended.

    Happens while the server is still resolving something. The renderer must
    still offer a next step instead of an empty list.
    """
    game = base_game()
    game["availableActions"] = {
        "canInk": False,
        "canEndTurn": False,
        "availableInk": 0,
        "remainingInkActions": 0,
        "cards": {},
    }
    return game


def coconut_table(
    *,
    players: int = 4,
    eliminated: tuple[int, ...] = (),
    **overrides: Any,
) -> dict:
    """A Coconut table, which seats anywhere from two to four players.

    Coconut is a format, not a player count: a table of two is as legal as a
    table of four, and the only thing that changes is how long `opponents` is.
    The wire keeps `opponent` pointing at the first of them, which is exactly
    why reading the singular looked like it worked.
    """
    names = {"1": "You", "2": "Joe", "3": "Ewaldo", "4": "DRobb"}
    game = base_game(gameVariant="coconut", isBotGame=False, playerNames=names)
    game["myPlayer"]["coconutCard"] = card("my-coconut", "coconut-011")

    seats = []
    for number in range(2, players + 1):
        seats.append(
            {
                "playerNumber": number,
                "name": names[str(number)],
                "handCount": 4,
                "deckCount": 47,
                "field": [card(f"opp{number}-pete", "5-195")],
                "items": [],
                "inkwell": [card(f"opp{number}-ink", "12-133")],
                "discard": [card(f"opp{number}-disc", "5-195")],
                "lore": number * 3,
                "eliminated": number in eliminated,
                "coconutCard": card(f"opp{number}-coconut", "coconut-017"),
            }
        )
    game["opponents"] = seats
    game["opponent"] = seats[0]
    game.update(overrides)
    return game


def all_phases() -> dict[str, dict]:
    """Every phase, for tests that sweep across all of them."""
    from . import prompts

    return {
        "coin_toss_mine": coin_toss(mine=True),
        "coin_toss_theirs": coin_toss(mine=False),
        "mulligan_mine": mulligan(my_turn_to_act=True),
        "mulligan_waiting": mulligan(my_turn_to_act=False),
        "playing": playing(),
        "opponents_turn": opponents_turn(),
        "prompt_boolean": with_prompt(prompts.BOOLEAN),
        "prompt_trigger": with_prompt(prompts.SELECT_TRIGGER),
        "prompt_target": with_prompt(prompts.SELECT_TARGET),
        "stuck": stuck(),
        "finished": finished(),
    }
