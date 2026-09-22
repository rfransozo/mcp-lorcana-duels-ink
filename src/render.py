"""Turn a raw Duels.ink game state into something an agent can actually read.

The wire format is about 5 KB of JSON keyed by UUIDs, with card identity hidden
behind `definitionId` strings like "10-71". Dropping that into a model's context
is both unreadable and wasteful, so everything here exists to produce a compact
view with real card names and an explicit list of playable moves.

The single most useful thing Duels.ink gives us is `availableActions`: the
server already decides what is legal, including a human-readable
`playBlockedReason` such as "Need 3 ink". An agent therefore never has to
implement Lorcana's rules - it just picks from what is offered.
"""

from typing import Any, Optional

from .cards import CardCatalog
from . import telemetry
from .formatting import join_lines

# Per-card capability flags from availableActions.cards[instanceId], mapped onto
# the tool that performs them.
#
# Only flags actually observed on the wire are listed. Duels.ink reports
# canInk, canPlay, canQuest, canChallenge, canSing, canMove and canBoost (plus
# the non-actions below). The last two are conditional on the board: canMove
# needs a location in play and canBoost needs both a Boost character and the
# ink to pay for it, so neither shows up until the game reaches that state.
# Both were once written off as invented for exactly that reason.
#
# There is no top-level canActivate: activated abilities arrive as a separate
# `activatedAbilities` list on the card, handled in _activated_moves.
CAPABILITY_TOOLS = {
    "canInk": ("duels_ink_card", "Put this card into the inkwell"),
    "canPlay": ("duels_play_card", "Play this card"),
    "canQuest": ("duels_quest", "Quest with this character for lore"),
    "canChallenge": ("duels_challenge", "Challenge an opposing character"),
    "canSing": ("duels_play_card", "Sing this song using an exerted singer"),
    "canMove": ("duels_send_game_action", "Move this character to one of your locations"),
    "canBoost": ("duels_send_game_action", "Boost this character"),
}

HAND_CAPABILITIES = {"canInk", "canPlay", "canSing"}
BOARD_CAPABILITIES = {"canQuest", "canChallenge", "canMove", "canBoost"}

# Flags that describe a property rather than an action the agent can take.
#
# Everything with a `has` prefix describes the card. hasSingTogether says the
# song carries the keyword - reported true from turn one, with no singers on
# the board. hasQuestAbility says questing will trigger something, and shows
# up on a character whose ink is still wet and which therefore cannot quest at
# all. Both were once advertised as unmapped capabilities, each offering a
# move that did not exist. The actionable flags are canSing and canQuest.
NON_ACTION_FLAGS = {
    "canAffordInkCost",
    "canBeSinger",
    "hasSingTogether",
    "hasQuestAbility",
}


async def _describe_card(
    catalog: CardCatalog,
    card: dict,
    actions: Optional[dict] = None,
) -> dict:
    """Merge a card instance with its catalog record and its legal actions."""
    definition_id = card.get("definitionId")
    record = await catalog.get(definition_id) if definition_id else None

    out: dict[str, Any] = {
        "instance_id": card.get("instanceId"),
        "card": (record or {}).get("fullName") or definition_id,
        "definition_id": definition_id,
    }

    if record:
        for src, dst in (
            ("type", "type"),
            ("cost", "cost"),
            ("inkable", "inkable"),
            ("strength", "strength"),
            ("willpower", "willpower"),
            ("lore", "lore"),
        ):
            value = record.get(src)
            if value not in (None, "", []):
                out[dst] = value
        colors = record.get("colors")
        if colors:
            out["ink"] = "/".join(str(c) for c in colors)
        rules = record.get("rulesText")
        if rules:
            out["text"] = rules

        # Keywords decide who may challenge whom, so they belong on the card
        # line itself rather than buried in prose. "Resist" and "Singer" carry
        # a value; "Evasive" and "Bodyguard" do not.
        keywords = []
        for ability in record.get("abilities") or []:
            name = ability.get("ability") if isinstance(ability, dict) else None
            if not name:
                continue
            value = ability.get("value")
            keywords.append(f"{name} {value:+d}" if name == "Resist" and isinstance(value, int)
                            else (f"{name} {value}" if value is not None else str(name)))
        if keywords:
            out["keywords"] = keywords

        named = [
            {"name": a.get("name"), "effect": a.get("effect")}
            for a in record.get("specialAbilities") or []
            if isinstance(a, dict) and a.get("name")
        ]
        if named:
            out["named_abilities"] = named

    if card.get("damage"):
        out["damage"] = card["damage"]
        if record and record.get("willpower") is not None:
            out["willpower_remaining"] = record["willpower"] - card["damage"]
    if card.get("exerted"):
        out["exerted"] = True
    if card.get("justPlayed"):
        out["just_played"] = True
    if card.get("appliedEffects"):
        out["effects"] = card["appliedEffects"]
    if card.get("cardsUnder"):
        out["cards_under"] = len(card["cardsUnder"])
    # Being at a location changes what a character can do and what happens to
    # it there - one board had five locations granting Evasive, +1/+1 and free
    # movement, and none of it was visible because this id was never read.
    if card.get("locationInstanceId"):
        out["location_instance_id"] = card["locationInstanceId"]
    if card.get("hasQuestedThisTurn"):
        out["quested_this_turn"] = True

    if actions:
        can = [k for k, v in actions.items() if v is True and k not in NON_ACTION_FLAGS]
        if can:
            out["can"] = can
        # The engine explains every refusal, not just the unplayable ones:
        # "Already inked this turn", "Ink dry (no Rush)". Only the play reason
        # used to reach the agent, so the rest were rediscovered by failing.
        blocked = next(
            (
                actions[k]
                for k in ("playBlockedReason", "questBlockedReason",
                          "challengeBlockedReason", "inkBlockedReason")
                if actions.get(k)
            ),
            None,
        )
        if blocked:
            out["blocked"] = blocked
        # A boosted or buffed character is not what the catalogue says it is.
        # Hercules reads 0/3 on the card and hits for 3 once boosted.
        if actions.get("effectiveStrength") is not None:
            out["effective_strength"] = actions["effectiveStrength"]
        if actions.get("effectiveLore") is not None:
            out["effective_lore"] = actions["effectiveLore"]
        if actions.get("boostCost") is not None:
            out["boost_cost"] = actions["boostCost"]
        abilities = [a for a in actions.get("activatedAbilities") or [] if a.get("name")]
        if abilities:
            out["activated_abilities"] = abilities
        if actions.get("hasSingTogether"):
            # How much singer cost is on the board against how much the song
            # needs - the gap is what decides whether it is worth holding.
            out["sing_together"] = {
                "cost": actions.get("singTogetherCost"),
                "available": actions.get("totalAvailableSingerCost") or 0,
            }
        if actions.get("validSingers"):
            out["valid_singers"] = actions["validSingers"]
        if actions.get("validTargets"):
            out["valid_targets"] = actions["validTargets"]
        if actions.get("challengeTargetInfo"):
            # Duels.ink resolves every possible challenge before we ask: who
            # dies, how much damage lands, which keywords apply. Passing it
            # through turns "pick an opposing character" into a real choice.
            out["challenge_targets"] = actions["challengeTargetInfo"]

    return out


async def _describe_zone(
    catalog: CardCatalog,
    cards: Optional[list],
    action_map: Optional[dict] = None,
) -> list[dict]:
    if not cards:
        return []
    out = []
    for card in cards:
        actions = (action_map or {}).get(card.get("instanceId"))
        out.append(await _describe_card(catalog, card, actions))
    return out


def _effect_label(effect: object) -> str:
    """A readable name for one applied effect, whatever shape it arrives in.

    The engine sends appliedEffects as a list and the board was always empty
    when it was inspected, so the element shape is not pinned down. Read the
    usual keys when it is a mapping and fall back to str() otherwise, rather
    than guessing a schema and dropping anything that does not match.
    """
    if isinstance(effect, dict):
        for key in ("name", "abilityName", "description", "type", "effect"):
            value = effect.get(key)
            if value:
                return str(value)
        return ", ".join(f"{k}={v}" for k, v in effect.items() if v is not None) or "effect"
    return str(effect)


def _challenge_outcome(info: dict, target: str) -> str:
    """One line saying what this challenge actually does to both sides."""
    to_them = info.get("damageToTarget")
    to_me = info.get("damageToAttacker")
    them = f"deals {to_them}" if to_them is not None else "deals damage"
    if info.get("willBanishTarget"):
        them += ", banishing it"
    mine = f"takes {to_me} back" if to_me is not None else "takes damage back"
    if info.get("willBanishAttacker"):
        mine += " and is banished"

    notes = [k for k, flag in (
        ("Bodyguard", "hasBodyguard"), ("Evasive", "hasEvasive"), ("Ward", "hasWard")
    ) if info.get(flag)]
    if info.get("isLocationTarget"):
        notes.append("location")
    suffix = f" [{', '.join(notes)}]" if notes else ""
    return f"Challenge {target}: {them}; {mine}{suffix}"


def _activated_moves(game_id: str, entries: list[dict]) -> list[dict]:
    """One move per activated ability that can be used right now.

    These never appear as a `can` flag - they arrive as a structured list on
    the card, with the costs already worked out by the engine.
    """
    moves = []
    for entry in entries:
        for ability in entry.get("activated_abilities") or []:
            if not ability.get("canActivate"):
                continue
            moves.append(
                {
                    "tool": "duels_send_game_action",
                    "why": f"Activate {ability['name']} on {entry['card']}",
                    "args": {
                        "game_id": game_id,
                        "action_type": "ACTIVATE_ABILITY",
                        "payload": {
                            "cardInstanceId": entry["instance_id"],
                            "abilityName": ability["name"],
                        },
                    },
                }
            )
    return moves


def _legal_moves(
    game: dict,
    described_hand: list[dict],
    described_field: list[dict],
    opponent_names: Optional[dict] = None,
) -> list[dict]:
    """Build the concrete list of callable moves for the current position.

    This is phase-aware on purpose. `availableActions` is populated even during
    the coin toss and the mulligan, so taking it at face value would advertise
    inking and ending the turn before the game has actually started.
    """
    available = game.get("availableActions") or {}
    game_id = game.get("id")
    status = game.get("status")
    moves: list[dict] = []

    # --- Phase gates ------------------------------------------------
    if status == "coin_toss":
        toss = game.get("coinToss") or {}
        if toss.get("isYourChoice"):
            for choice, label in (
                ("play", "Go first (on the play) - you skip your first draw"),
                ("draw", "Go second (on the draw) - you draw on turn one"),
            ):
                moves.append(
                    {
                        "tool": "duels_send_game_action",
                        "why": label,
                        "args": {
                            "game_id": game_id,
                            "action_type": "CHOOSE_STARTING_PLAYER",
                            "payload": {"choice": choice},
                        },
                    }
                )
        else:
            moves.append(
                {
                    "tool": "duels_wait_for_my_turn",
                    "why": "Your opponent won the toss and is choosing who goes first",
                    "args": {"game_id": game_id},
                }
            )
        return moves

    if status == "mulligan":
        mulligan = game.get("mulliganState") or {}
        if mulligan.get("canSubmit") and not mulligan.get("myDone"):
            moves.append(
                {
                    "tool": "duels_send_game_action",
                    "why": (
                        "Submit your mulligan. selectedCardIds is the list of hand "
                        "instanceIds to put back; an empty list keeps the whole hand"
                    ),
                    "args": {
                        "game_id": game_id,
                        "action_type": "MULLIGAN",
                        "payload": {"selectedCardIds": []},
                    },
                }
            )
        else:
            moves.append(
                {
                    "tool": "duels_wait_for_my_turn",
                    "why": "Waiting for your opponent to finish their mulligan",
                    "args": {"game_id": game_id},
                }
            )
        return moves

    if game.get("pendingPrompts"):
        return [
            {
                "tool": "duels_respond_to_prompt",
                "why": "A prompt is waiting - it must be answered before anything else",
                "args": {"game_id": game_id},
            }
        ]

    viewing_as, current = game.get("viewingAs"), game.get("currentPlayer")
    if viewing_as is not None and current is not None and viewing_as != current:
        return [
            {
                "tool": "duels_wait_for_my_turn",
                "why": "It is your opponent's turn",
                "args": {"game_id": game_id},
            }
        ]

    if status in ("finished", "complete", "ended") or game.get("winner") is not None:
        return []

    # --- Normal play ------------------------------------------------
    for entries, allowed in (
        (described_hand, HAND_CAPABILITIES),
        (described_field, BOARD_CAPABILITIES),
    ):
        for entry in entries:
            for flag in entry.get("can", []):
                mapped = CAPABILITY_TOOLS.get(flag)

                if mapped and flag not in allowed:
                    # A board-only capability on a card still in hand, or the
                    # reverse. Duels.ink reports flags per instance without
                    # saying which zone they apply to.
                    continue

                if not mapped:
                    # An unrecognised capability: surface it so a new Duels.ink
                    # feature degrades into a hint rather than vanishing, while
                    # making clear the action type still has to be supplied.
                    moves.append(
                        {
                            "tool": "duels_send_game_action",
                            "why": (
                                f"{entry['card']} reports the capability '{flag}', which "
                                "this server does not map yet - supply the matching "
                                "action_type"
                            ),
                            "args": {
                                "game_id": game_id,
                                "action_type": "<ACTION TYPE>",
                                "payload": {"cardInstanceId": entry["instance_id"]},
                            },
                        }
                    )
                    continue

                tool, why = mapped
                if flag == "canBoost":
                    cost = entry.get("boost_cost")
                    moves.append(
                        {
                            "tool": tool,
                            "why": (
                                f"Boost {entry['card']}"
                                + (f" for {cost} ink" if cost is not None else "")
                            ),
                            "args": {
                                "game_id": game_id,
                                "action_type": "BOOST",
                                "payload": {"cardInstanceId": entry["instance_id"]},
                            },
                        }
                    )
                    continue

                if flag == "canMove":
                    for loc in [e for e in described_field if e.get("type") == "location"]:
                        moves.append(
                            {
                                "tool": tool,
                                "why": f"Move {entry['card']} to {loc['card']}",
                                "args": {
                                    "game_id": game_id,
                                    "action_type": "MOVE_TO_LOCATION",
                                    "payload": {
                                        "characterInstanceId": entry["instance_id"],
                                        "locationInstanceId": loc["instance_id"],
                                    },
                                },
                            }
                        )
                    continue

                if flag == "canChallenge":
                    # duels_challenge names its arguments differently. When the
                    # engine sent the per-target maths, offer each target as its
                    # own move with the outcome spelled out; otherwise fall back
                    # to the placeholder so nothing is silently lost.
                    targets = entry.get("challenge_targets") or []
                    if targets:
                        for info in targets:
                            target_id = info.get("instanceId")
                            if not target_id:
                                continue
                            name = (opponent_names or {}).get(target_id, target_id)
                            moves.append(
                                {
                                    "tool": tool,
                                    "why": (
                                        f"{_challenge_outcome(info, name)}"
                                        f" - with {entry['card']}"
                                    ),
                                    "args": {
                                        "game_id": game_id,
                                        "attacker_instance_id": entry["instance_id"],
                                        "target_instance_id": target_id,
                                    },
                                }
                            )
                        continue
                    args: dict[str, Any] = {
                        "game_id": game_id,
                        "attacker_instance_id": entry["instance_id"],
                        "target_instance_id": "<pick an opposing character>",
                    }
                else:
                    args = {"game_id": game_id, "card_instance_id": entry["instance_id"]}
                    if flag == "canSing" and entry.get("valid_singers"):
                        # Sing Together needs enough singers to cover the cost;
                        # an ordinary song only ever needs one.
                        together = entry.get("sing_together") or {}
                        needed = together.get("cost")
                        args["singer_instance_ids"] = (
                            list(entry["valid_singers"])
                            if needed is not None
                            else entry["valid_singers"][:1]
                        )

                moves.append({"tool": tool, "why": f"{why}: {entry['card']}", "args": args})

    moves.extend(_activated_moves(game_id, described_field))

    if available.get("canEndTurn"):
        moves.append(
            {
                "tool": "duels_end_turn",
                "why": "End your turn",
                "args": {"game_id": game_id},
            }
        )

    if not moves:
        # Nothing playable and the turn cannot be ended yet - usually an ability
        # still resolving on the server. Never hand back an empty list while the
        # game is live, or the caller has no next step to take.
        moves.append(
            {
                "tool": "duels_wait_for_my_turn",
                "why": (
                    "Nothing is playable right now and the turn cannot be ended yet - "
                    "the server is still resolving something. Wait, then re-read the state"
                ),
                "args": {"game_id": game_id},
            }
        )

    return moves


LOW_CLOCK_MS = 30_000


def _mmss(ms: object) -> str:
    """Milliseconds as m:ss, floored, never negative."""
    try:
        seconds = max(0, int(ms) // 1000)
    except (TypeError, ValueError):
        return "?"
    return f"{seconds // 60}:{seconds % 60:02d}"


def _clock(game: dict) -> Optional[dict]:
    """The per-turn chess clock, which only games against people have.

    Those games are timed - two minutes a turn, with about 45 seconds credited
    back on ending one - and running out loses the game. Bot games are not.
    None of this is in `availableActions`, nor in `turnGateState`, which holds
    per-turn counters and no time at all; it lives in `timerView`. Until this
    was read the caller could not see its own clock, which is how a game was
    played to 15-21 with no warning that a clock existed.

    Both fields are read defensively: whether an untimed game omits `timerView`
    or carries it full of nulls, the answer is the same - no clock to show.
    """
    view = game.get("timerView") or {}
    mine = view.get("myTimeRemainingMs")
    theirs = view.get("opponentTimeRemainingMs")
    # The capabilities count as a clock even with no times on it: a pre-game
    # victory is claimable before either clock has started, and hiding that
    # because the numbers are absent loses the one thing worth knowing.
    claimable = any(
        view.get(k)
        for k in ("canDeclareVictory", "canClaimAfkVictory",
                  "canClaimPreGameVictory", "canPingOpponent", "wasAfkPinged")
    )
    if mine is None and theirs is None and not claimable:
        return None
    return {
        "my_ms": mine,
        "opponent_ms": theirs,
        "my_clock_running": bool(view.get("myTimerTicking")),
        "opponent_clock_running": bool(view.get("opponentTimerTicking")),
        # With three opponents "the opponent's clock" is ambiguous; this says
        # whose it is.
        "active_player": view.get("activePlayer"),
        # A game can be over without anybody moving. The server says when it
        # is claimable and we never looked, so a table where somebody had
        # walked away was simply waited out.
        "opponents_out_of_time": view.get("opponentZeroCount"),
        "can_claim_timeout": bool(view.get("canDeclareVictory")),
        "can_claim_afk": bool(view.get("canClaimAfkVictory")),
        "can_claim_pregame": bool(view.get("canClaimPreGameVictory")),
        "can_ping": bool(view.get("canPingOpponent")),
        "was_pinged": bool(view.get("wasAfkPinged")),
    }


def _opponent_seats(game: dict) -> list[dict]:
    """Every opponent, in a shape that does not depend on the player count.

    A duel sends `opponent` alone. A table sends `opponents` as a list and
    keeps `opponent` as one of them, so reading only the singular showed a
    four-player Coconut game as a duel against one stranger - no error, no
    missing key, just two of the three opponents quietly absent from the board
    and from the lore race. The list wins whenever it exists.
    """
    seats = game.get("opponents")
    if isinstance(seats, list) and seats:
        return [seat for seat in seats if isinstance(seat, dict)]
    one = game.get("opponent")
    return [one] if isinstance(one, dict) and one else []


def _player_number(seat: dict) -> Optional[int]:
    """The seat's player number, however this payload happens to spell it."""
    for key in ("playerNumber", "player", "seat"):
        value = seat.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return None


def _coconut_zone(player: dict) -> list[dict]:
    """The Coconut as a one-card zone, or nothing outside the format.

    It is a permanent that never sat in the deck and never leaves play, and the
    wire sends it as a plain card object rather than a list. Wrapping it lets
    it go through _describe_zone like any other zone.
    """
    card = player.get("coconutCard")
    return [card] if isinstance(card, dict) and card else []


def _lore_to_win(game: dict) -> int:
    """How much lore actually wins this game.

    Twenty is only the default. Coconut needs 25 and Pack Rush 15, and a table
    may set its own, so the number is never safe to assume - a Coconut game was
    played as if 8 lore were nearly half the race when it was under a third.
    `loreToWin` is sent when a table overrides it; otherwise the variant says.
    """
    explicit = game.get("loreToWin")
    if isinstance(explicit, int) and not isinstance(explicit, bool) and explicit > 0:
        return explicit
    variant = "".join(c for c in str(game.get("gameVariant") or "") if c.isalpha()).lower()
    return {"coconut": 25, "packrush": 15}.get(variant, 20)


def _undo(game: dict) -> Optional[dict]:
    """What can still be taken back, and what it would cost.

    A table sets its own undo rules, so the answer is per game rather than per
    player: `disabled` tables report nothing, `timed` ones charge the clock.
    None of this was read, so a misplay was final even where the table said it
    need not be.
    """
    out = {
        "can_request": bool(game.get("canRequestUndo")),
        "free": bool(game.get("allowFreeUndo")),
        # Milliseconds, like every other duration on this wire. Named
        # `_seconds` once, it rendered "30000s off your clock".
        "time_cost_ms": game.get("undoTimeCost"),
        "would_reveal_information": bool(game.get("nextUndoHasRevealedInfo")),
        "can_undo_choice": bool(game.get("canUndoChoice")),
        "can_cancel_ability": bool(game.get("canCancelInProgressAbility")),
        "can_rewind_choice": bool(game.get("canRewindAbilityChoice")),
        "declines_exhausted": bool(game.get("undoDeclineLimitReached")),
    }
    return out if any(v for v in out.values()) else None


def _removal_vote(game: dict) -> Optional[dict]:
    """A table's way of ejecting somebody who stopped playing.

    Only multiplayer has it: with one opponent there is nobody left to vote
    with, so the key is absent and this is None.
    """
    called = game.get("removalVoteCalled")
    if not called:
        return None
    if isinstance(called, dict):
        return {
            "target_player": called.get("targetPlayer") or called.get("target"),
            "votes_for": called.get("votesFor") or called.get("accepted"),
            "votes_needed": called.get("votesNeeded") or called.get("required"),
            "i_have_voted": bool(called.get("hasVoted") or called.get("myVote") is not None),
            "called_by": called.get("calledBy"),
        }
    return {"target_player": called}


def _revealed(player: dict) -> list[dict]:
    """Cards this player has turned face up this turn.

    A zone of its own, and the only place the cards behind a "choose one of
    these" prompt exist: Imperial Invitation shows four off the top of the
    deck, and without this they are four bare UUIDs to pick between blindly.
    """
    cards = player.get("revealedCardsThisTurn")
    return [c for c in cards if isinstance(c, dict)] if isinstance(cards, list) else []


def _prompt_ids(prompts: list) -> set:
    """Every card instance a prompt refers to, whatever field it arrived in."""
    found: set = set()
    for prompt in prompts or []:
        if not isinstance(prompt, dict):
            continue
        for key in ("cardInstanceIds", "validTargets", "validCards",
                    "nonSelectableCardIds", "selectedCardIds"):
            value = prompt.get(key)
            if isinstance(value, list):
                found.update(v for v in value if isinstance(v, str))
        for group in prompt.get("zoneGroups") or []:
            if isinstance(group, dict):
                found.update(
                    v for v in group.get("cardInstanceIds") or [] if isinstance(v, str)
                )
        source = prompt.get("sourceCardInstanceId")
        if isinstance(source, str):
            found.add(source)
    return found


async def render_game_state(game: dict, catalog: CardCatalog) -> dict:
    """Produce the compact, agent-facing view of a game state."""
    telemetry.note(game)
    me = game.get("myPlayer") or {}
    available = game.get("availableActions") or {}
    action_map = available.get("cards") or {}

    viewing_as = game.get("viewingAs")
    current = game.get("currentPlayer")

    hand = await _describe_zone(catalog, me.get("hand"), action_map)
    field = await _describe_zone(catalog, me.get("field"), action_map)
    items = await _describe_zone(catalog, me.get("items"), action_map)

    # One entry per opponent, so a duel and a four-player table differ only in
    # the length of this list. `or [{}]` keeps the shape when there is nobody
    # to describe yet - during the coin toss, say - instead of dropping the key.
    seats = _opponent_seats(game) or [{}]
    names = game.get("playerNames") or {}
    opponents: list[dict] = []
    for seat in seats:
        number = _player_number(seat)
        if number is None and len(seats) == 1 and isinstance(viewing_as, int):
            # A duel names its seats but does not number them.
            number = 3 - viewing_as
        opponents.append(
            {
                "name": seat.get("name") or names.get(str(number)),
                "player_number": number,
                "lore": seat.get("lore"),
                "eliminated": bool(seat.get("eliminated")),
                "ink_total": len(seat.get("inkwell") or []),
                "hand_count": seat.get("handCount"),
                "deck_count": seat.get("deckCount"),
                "discard_count": len(seat.get("discard") or []),
                "discard": await _describe_zone(catalog, seat.get("discard")),
                "field": await _describe_zone(catalog, seat.get("field"), action_map),
                "items": await _describe_zone(catalog, seat.get("items"), action_map),
                "coconut": await _describe_zone(catalog, _coconut_zone(seat)),
                "revealed": await _describe_zone(catalog, _revealed(seat)),
            }
        )

    payload: dict[str, Any] = {
        "game_id": game.get("id"),
        "status": game.get("status"),
        "turn_number": game.get("turnNumber"),
        "player_number": viewing_as,
        "my_turn": viewing_as is not None and viewing_as == current,
        "state_version": game.get("stateVersion"),
        "is_bot_game": game.get("isBotGame"),
        "clock": _clock(game),
        "undo": _undo(game),
        "removal_vote": _removal_vote(game),
        "game_variant": game.get("gameVariant"),
        # A Coconut game without Coconuts plays like a plain singleton deck
        # and nothing says so. One was played to the end that way: the free
        # play never appeared in a single legal move, and the absence read
        # as "no ability available" rather than "the card is not there".
        "coconuts_in_play": None,
        "lore_to_win": _lore_to_win(game),
        "player_count": 1 + len(_opponent_seats(game)),
        "me": {
            "lore": me.get("lore"),
            "ink_available": available.get("availableInk"),
            "ink_total": len(me.get("inkwell") or []),
            "ink_actions_left": available.get("remainingInkActions"),
            "hand_count": len(me.get("hand") or []),
            "deck_count": me.get("deckCount"),
            "discard_count": len(me.get("discard") or []),
            "discard": await _describe_zone(catalog, me.get("discard")),
            "hand": hand,
            "field": field,
            "items": items,
            "coconut": await _describe_zone(catalog, _coconut_zone(me)),
            "revealed": await _describe_zone(catalog, _revealed(me)),
            "eliminated": bool(me.get("eliminated")),
        },
        # Kept as the first opponent so anything reading the old singular
        # contract still works; `opponents` is the one to read.
        "opponent": opponents[0],
        "opponents": opponents,
        "legal_moves": _legal_moves(
            game,
            hand,
            [*field, *items],
            {
                c["instance_id"]: c["card"]
                for seat in opponents
                for c in [*seat["field"], *seat["items"]]
                if c.get("instance_id") and c.get("card")
            },
        ),
    }

    variant = "".join(
        c for c in str(game.get("gameVariant") or "") if c.isalpha()
    ).lower()
    if variant == "coconut":
        payload["coconuts_in_play"] = sum(
            1 for seat in [payload["me"], *opponents] if seat.get("coconut")
        )
    else:
        payload.pop("coconuts_in_play", None)

    # A character carries the id of the location it is at; the name lives on
    # the location card, which is in somebody's items. Resolve it once here so
    # the card line can just print it.
    place = {
        card["instance_id"]: card["card"]
        for seat in [payload["me"], *opponents]
        for card in seat.get("items") or []
        if card.get("instance_id") and card.get("card")
    }
    for seat in [payload["me"], *opponents]:
        for card in seat.get("field") or []:
            where = place.get(card.get("location_instance_id"))
            if where:
                card["location"] = where

    if game.get("winner") is not None:
        payload["winner"] = game["winner"]
        payload["i_won"] = game["winner"] == viewing_as
        # Lore, a concession, a timeout and an absence all end a game and read
        # identically without this.
        if game.get("victoryReason"):
            payload["victory_reason"] = game["victoryReason"]
    if game.get("pendingPrompts"):
        payload["pending_prompts"] = game["pendingPrompts"]
        # A prompt lists instance ids and nothing else, so "choose one of
        # these four" arrived as four UUIDs and was answered by picking the
        # first. Every zone in the state is indexed here, including the
        # revealed cards and the card the prompt came from, so the choice can
        # be made on what the cards actually are.
        named: dict[str, str] = {}
        for seat in [payload["me"], *opponents]:
            for zone in ("hand", "field", "items", "discard", "coconut", "revealed"):
                for card in seat.get(zone) or []:
                    if card.get("instance_id") and card.get("card"):
                        named[card["instance_id"]] = card["card"]
        source = game.get("promptSourceCard")
        if isinstance(source, dict) and source.get("instanceId"):
            described = await _describe_card(catalog, source)
            if described.get("card"):
                named[source["instanceId"]] = described["card"]
        wanted = _prompt_ids(game["pendingPrompts"])
        payload["prompt_cards"] = {k: v for k, v in named.items() if k in wanted}
        payload["prompt_cards_unknown"] = sorted(wanted - set(named))
    if game.get("status") == "mulligan":
        payload["mulligan"] = game.get("mulliganState")
    if game.get("status") == "coin_toss":
        toss = game.get("coinToss") or {}
        payload["coin_toss"] = {
            "you_won_toss": toss.get("youWonToss"),
            "your_choice": toss.get("isYourChoice"),
            "result": (toss.get("result") or {}).get("result"),
        }
    if game.get("firstPlayer") is not None:
        payload["first_player"] = game["firstPlayer"]
    if game.get("opponentHasPendingPrompts"):
        payload["waiting_on_opponent_prompt"] = True
    if available.get("canInkFromDiscard"):
        payload["me"]["can_ink_from_discard"] = True
    if available.get("canPlayFromDiscard"):
        payload["me"]["can_play_from_discard"] = True

    return payload


# -----------------------------------------------------------------
# Markdown rendering
# -----------------------------------------------------------------
def _card_line(entry: dict, show_actions: bool = True, seen: Optional[set] = None) -> str:
    bits = [f"**{entry.get('card')}**"]
    stats = []
    if entry.get("cost") is not None:
        stats.append(f"{entry['cost']} ink")
    if entry.get("strength") is not None and entry.get("willpower") is not None:
        wp = entry.get("willpower_remaining", entry["willpower"])
        strength = entry.get("effective_strength", entry["strength"])
        stats.append(f"{strength}/{wp}")
        if strength != entry["strength"]:
            stats.append(f"base {entry['strength']}")
    lore = entry.get("effective_lore", entry.get("lore"))
    if lore:
        stats.append(f"{lore} lore")
    if entry.get("ink"):
        stats.append(entry["ink"])
    if stats:
        bits.append(f"({', '.join(stats)})")

    flags = []
    if entry.get("exerted"):
        flags.append("exerted")
    if entry.get("damage"):
        flags.append(f"{entry['damage']} dmg")
    if entry.get("just_played"):
        flags.append("just played (ink is dry next turn)")
    if entry.get("inkable") is False:
        flags.append("not inkable")
    if entry.get("cards_under"):
        flags.append(f"{entry['cards_under']} under")
    if entry.get("location"):
        flags.append(f"at {entry['location']}")
    if entry.get("quested_this_turn"):
        flags.append("already quested")
    for effect in entry.get("effects") or []:
        # A buff already applied to this character. The engine only computes
        # the resulting strength as effectiveStrength, and only when a
        # challenge is possible, so outside combat this line is the only sign
        # that the printed stats are not the real ones.
        flags.append(_effect_label(effect))
    if flags:
        bits.append(f"[{', '.join(flags)}]")

    if entry.get("keywords"):
        bits.append("{" + ", ".join(entry["keywords"]) + "}")

    together = entry.get("sing_together")
    if together and together.get("cost") is not None:
        bits.append(
            f"[Sing Together {together['cost']} - "
            f"singers ready {together.get('available', 0)}/{together['cost']}]"
        )

    if show_actions:
        if entry.get("can"):
            bits.append(f"-> can: {', '.join(entry['can'])}")
        elif entry.get("blocked"):
            bits.append(f"-> blocked: {entry['blocked']}")

    line = f"- {' '.join(bits)}"
    if entry.get("instance_id"):
        line += f"\n  `{entry['instance_id']}`"

    NEWLINE = chr(10)
    for ability in entry.get("activated_abilities") or []:
        costs = []
        if ability.get("inkCost"):
            costs.append(f"{ability['inkCost']} ink")
        if ability.get("exertCost"):
            costs.append("exert")
        if ability.get("banishCost"):
            costs.append("banish")
        if ability.get("discardCost"):
            costs.append(f"discard {ability['discardCost']}")
        cost = f" ({', '.join(costs)})" if costs else ""
        state = "ready" if ability.get("canActivate") else (
            ability.get("blockedReason") or "not available")
        line += NEWLINE + f"  [activate] {ability['name']}{cost} - {state}"

    for ability in entry.get("named_abilities") or []:
        if ability.get("name"):
            line += "\n  **" + str(ability["name"]) + "**"

    # Rules text. Two copies of the same card carry identical text, so it is
    # printed once per state and later copies point back at it - no information
    # is lost and the duplication is cut.
    text = entry.get("text")
    if text:
        definition_id = entry.get("definition_id")
        if seen is not None and definition_id in seen:
            line += "\n  _(text above)_"
        else:
            if seen is not None and definition_id:
                seen.add(definition_id)
            for paragraph in str(text).split("\n"):
                if paragraph.strip():
                    line += f"\n  {paragraph.strip()}"
    return line


def _discard_line(entries: list[dict]) -> str:
    """One compact line for a discard pile, grouped by card with counts.

    A discard grows all game and the cards in it can no longer act, so what
    matters is which cards have been spent - not one line per instance.
    """
    counts: dict[str, int] = {}
    for entry in entries:
        name = entry.get("card") or entry.get("definition_id") or "?"
        counts[name] = counts.get(name, 0) + 1
    return ", ".join(
        f"{count}x {name}" for name, count in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    )


def game_state_markdown(payload: dict) -> str:
    """Human-readable rendering of render_game_state's output."""
    me = payload.get("me") or {}
    opponents = payload.get("opponents") or [payload.get("opponent") or {}]

    def who(index: int, seat: dict) -> str:
        """What to call an opponent: their name, or their seat when unnamed."""
        name = seat.get("name") or (
            f"Opponent {index + 1}" if len(opponents) > 1 else "Opponent"
        )
        return f"{name} (ELIMINATED)" if seat.get("eliminated") else name

    turn = "YOUR TURN" if payload.get("my_turn") else "opponent's turn"
    # The goal belongs in the header because it is not always 20: Coconut wants
    # 25, Pack Rush 15, and a table may set its own. Getting it wrong is silent
    # - it just makes the whole lore race read as closer than it is.
    goal = f" - first to {payload['lore_to_win']} lore" if payload.get("lore_to_win") else ""
    variant = f" - {payload['game_variant']}" if payload.get("game_variant") else ""
    you = "**You** (ELIMINATED)" if me.get("eliminated") else "**You**"
    header = [
        f"# Game {payload.get('game_id')}",
        f"**{payload.get('status')}** - turn {payload.get('turn_number')} - {turn}"
        f"{variant}{goal}",
        "",
        "| | Lore | Hand | Deck | Ink | Discard |",
        "|---|---|---|---|---|---|",
        f"| {you} | {me.get('lore')} | {me.get('hand_count')} | {me.get('deck_count')} | "
        f"{me.get('ink_available')}/{me.get('ink_total')} | {me.get('discard_count')} |",
        *[
            f"| {who(i, o)} | {o.get('lore')} | {o.get('hand_count')} | "
            f"{o.get('deck_count')} | {o.get('ink_total')} | {o.get('discard_count')} |"
            for i, o in enumerate(opponents)
        ],
        "",
    ]

    clock = payload.get("clock")
    if clock:
        line = (
            f"**Clock** - you {_mmss(clock.get('my_ms'))} - "
            f"opponent {_mmss(clock.get('opponent_ms'))}"
        )
        if clock.get("my_clock_running"):
            mine = clock.get("my_ms")
            urgent = isinstance(mine, (int, float)) and mine <= LOW_CLOCK_MS
            line += (
                " - **your clock is nearly gone; hitting 0:00 eliminates you "
                "on the spot, board and all**"
                if urgent
                else " - your clock is running"
            )
        elif clock.get("opponent_clock_running"):
            line += " - opponent's clock is running"
        header.extend([line, ""])

        claims = [
            name
            for key, name in (
                ("can_claim_timeout", "timeout"),
                ("can_claim_afk", "absence"),
                ("can_claim_pregame", "never starting"),
            )
            if clock.get(key)
        ]
        if claims:
            header.extend([
                f"**You can claim this game right now** ({', '.join(claims)}) - "
                "`duels_claim_victory`. Waiting it out does nothing.",
                "",
            ])
        elif clock.get("can_ping"):
            header.extend([
                "_The opponent looks idle; `duels_claim_victory` with kind='ping' "
                "starts the clock on claiming it._",
                "",
            ])

    if payload.get("winner") is not None:
        winner = payload["winner"]
        named = next(
            (
                seat["name"]
                for seat in opponents
                if seat.get("name") and seat.get("player_number") == winner
            ),
            None,
        )
        beat_me = f"{named} (player {winner})" if named else f"player {winner}"
        why = f" by {payload['victory_reason']}" if payload.get("victory_reason") else ""
        header.append(
            f"## Game over{why} - {'you won' if payload.get('i_won') else 'you lost'} "
            f"(winner: {beat_me})\n"
        )

    vote = payload.get("removal_vote")
    if vote:
        target = vote.get("target_player")
        mine = " - you have already voted" if vote.get("i_have_voted") else ""
        header.extend([
            f"## A vote is running to remove player {target}{mine}",
            "Answer it with `duels_removal_vote`. A vote nobody answers keeps "
            "the table stuck on whoever left.",
            "",
        ])

    undo = payload.get("undo")
    if undo and undo.get("can_request"):
        cost = (
            "free"
            if undo.get("free")
            else f"{_mmss(undo['time_cost_ms'])} off your clock"
            if undo.get("time_cost_ms")
            else "allowed"
        )
        header.extend([f"_Your last move can be taken back ({cost}) - `duels_undo`._", ""])

    if payload.get("pending_prompts"):
        header.append(
            "## Prompt waiting\nA decision is pending. Answer it with `duels_respond_to_prompt` "
            "before taking any other action.\n"
        )
        header.append("```json")
        header.append(str(payload["pending_prompts"])[:2000])
        header.append("```\n")
        known = payload.get("prompt_cards") or {}
        if known:
            header.append("The cards it is asking about:")
            header.extend(f"- **{name}** `{inst}`" for inst, name in known.items())
            header.append("")
        unknown = payload.get("prompt_cards_unknown") or []
        if unknown:
            header.append(
                f"_{len(unknown)} of the ids above are not in any zone this state "
                "shows, so they cannot be named._\n"
            )

    if payload.get("mulligan"):
        header.append(f"## Mulligan\n{payload['mulligan']}\n")

    # Shared across every zone so a card's rules text is printed once per state.
    seen: set = set()

    sections = []
    # The Coconut leads: it is in play from turn one and never leaves, so it is
    # part of the board even though it was never in the deck.
    if me.get("coconut"):
        sections.append(
            "## Your Coconut\n" + "\n".join(_card_line(c, seen=seen) for c in me["coconut"])
        )
    elif payload.get("coconuts_in_play") == 0:
        sections.append(
            "## No Coconut is in play\n"
            "This game calls itself Coconut, but nobody has a Coconut card "
            "on the board - the wire sends no `coconutCard` at all. The "
            "format's whole engine is missing, so play the deck on its "
            "cards alone and do not wait for an ability that cannot arrive."
        )
    if me.get("field"):
        sections.append(
            "## Your board\n" + "\n".join(_card_line(c, seen=seen) for c in me["field"])
        )
    if me.get("items"):
        sections.append(
            "## Your items/locations\n"
            + "\n".join(_card_line(c, seen=seen) for c in me["items"])
        )
    for index, seat in enumerate(opponents):
        for title, zone in (("Coconut", "coconut"), ("board", "field"),
                            ("items/locations", "items")):
            if seat.get(zone):
                sections.append(
                    f"## {who(index, seat)} {title}\n"
                    + "\n".join(
                        _card_line(c, show_actions=False, seen=seen) for c in seat[zone]
                    )
                )
    if me.get("hand"):
        sections.append("## Your hand\n" + "\n".join(_card_line(c, seen=seen) for c in me["hand"]))

    # Discards last: they inform what has been spent, but nothing in them acts.
    if me.get("discard"):
        sections.append(
            f"## Discard - you ({len(me['discard'])})\n" + _discard_line(me["discard"])
        )
    for index, seat in enumerate(opponents):
        if seat.get("discard"):
            sections.append(
                f"## Discard - {who(index, seat)} ({len(seat['discard'])})\n"
                + _discard_line(seat["discard"])
            )

    sections.append(legal_moves_markdown(payload))

    return join_lines([*header, *sections])


def legal_moves_markdown(payload: dict) -> str:
    """Just the callable-moves block, for reuse outside the full state dump.

    A rejected action needs to show what *is* playable, and that is this list.
    """
    moves = payload.get("legal_moves") or []
    if not moves:
        return (
            "## Legal moves right now\nNone - it is not your turn, or the game is over. "
            "Use `duels_wait_for_my_turn` to block until it is your turn again."
        )
    lines = ["## Legal moves right now"]
    for m in moves:
        lines.append(f"- {m['why']}\n  `{m['tool']}` with `{m['args']}`")
    return "\n".join(lines)
