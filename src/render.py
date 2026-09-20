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


async def render_game_state(game: dict, catalog: CardCatalog) -> dict:
    """Produce the compact, agent-facing view of a game state."""
    me = game.get("myPlayer") or {}
    opponent = game.get("opponent") or {}
    available = game.get("availableActions") or {}
    action_map = available.get("cards") or {}

    viewing_as = game.get("viewingAs")
    current = game.get("currentPlayer")

    hand = await _describe_zone(catalog, me.get("hand"), action_map)
    field = await _describe_zone(catalog, me.get("field"), action_map)
    items = await _describe_zone(catalog, me.get("items"), action_map)
    opp_field = await _describe_zone(catalog, opponent.get("field"), action_map)
    opp_items = await _describe_zone(catalog, opponent.get("items"), action_map)

    payload: dict[str, Any] = {
        "game_id": game.get("id"),
        "status": game.get("status"),
        "turn_number": game.get("turnNumber"),
        "player_number": viewing_as,
        "my_turn": viewing_as is not None and viewing_as == current,
        "state_version": game.get("stateVersion"),
        "is_bot_game": game.get("isBotGame"),
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
        },
        "opponent": {
            "name": (game.get("playerNames") or {}).get(str(3 - (viewing_as or 1)))
            if game.get("playerNames")
            else None,
            "lore": opponent.get("lore"),
            "ink_total": len(opponent.get("inkwell") or []),
            "hand_count": opponent.get("handCount"),
            "deck_count": opponent.get("deckCount"),
            "discard_count": len(opponent.get("discard") or []),
            "discard": await _describe_zone(catalog, opponent.get("discard")),
            "field": opp_field,
            "items": opp_items,
        },
        "legal_moves": _legal_moves(
            game,
            hand,
            [*field, *items],
            {
                c["instance_id"]: c["card"]
                for c in [*opp_field, *opp_items]
                if c.get("instance_id") and c.get("card")
            },
        ),
    }

    if game.get("winner") is not None:
        payload["winner"] = game["winner"]
        payload["i_won"] = game["winner"] == viewing_as
    if game.get("pendingPrompts"):
        payload["pending_prompts"] = game["pendingPrompts"]
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
    opp = payload.get("opponent") or {}

    turn = "YOUR TURN" if payload.get("my_turn") else "opponent's turn"
    header = [
        f"# Game {payload.get('game_id')}",
        f"**{payload.get('status')}** - turn {payload.get('turn_number')} - {turn}",
        "",
        f"| | Lore | Hand | Deck | Ink | Discard |",
        f"|---|---|---|---|---|---|",
        f"| **You** | {me.get('lore')} | {me.get('hand_count')} | {me.get('deck_count')} | "
        f"{me.get('ink_available')}/{me.get('ink_total')} | {me.get('discard_count')} |",
        f"| {opp.get('name') or 'Opponent'} | {opp.get('lore')} | {opp.get('hand_count')} | "
        f"{opp.get('deck_count')} | {opp.get('ink_total')} | {opp.get('discard_count')} |",
        "",
    ]

    if payload.get("winner") is not None:
        header.append(
            f"## Game over - {'you won' if payload.get('i_won') else 'you lost'} "
            f"(winner: player {payload['winner']})\n"
        )

    if payload.get("pending_prompts"):
        header.append(
            "## Prompt waiting\nA decision is pending. Answer it with `duels_respond_to_prompt` "
            "before taking any other action.\n"
        )
        header.append("```json")
        header.append(str(payload["pending_prompts"])[:2000])
        header.append("```\n")

    if payload.get("mulligan"):
        header.append(f"## Mulligan\n{payload['mulligan']}\n")

    # Shared across every zone so a card's rules text is printed once per state.
    seen: set = set()

    sections = []
    if me.get("field"):
        sections.append(
            "## Your board\n" + "\n".join(_card_line(c, seen=seen) for c in me["field"])
        )
    if me.get("items"):
        sections.append(
            "## Your items/locations\n"
            + "\n".join(_card_line(c, seen=seen) for c in me["items"])
        )
    if opp.get("field"):
        sections.append(
            "## Opponent board\n"
            + "\n".join(_card_line(c, show_actions=False, seen=seen) for c in opp["field"])
        )
    if opp.get("items"):
        sections.append(
            "## Opponent items/locations\n"
            + "\n".join(_card_line(c, show_actions=False, seen=seen) for c in opp["items"])
        )
    if me.get("hand"):
        sections.append("## Your hand\n" + "\n".join(_card_line(c, seen=seen) for c in me["hand"]))

    # Discards last: they inform what has been spent, but nothing in them acts.
    if me.get("discard"):
        sections.append(
            f"## Discard - you ({len(me['discard'])})\n" + _discard_line(me["discard"])
        )
    if opp.get("discard"):
        sections.append(
            f"## Discard - opponent ({len(opp['discard'])})\n" + _discard_line(opp["discard"])
        )

    moves = payload.get("legal_moves") or []
    if moves:
        lines = ["## Legal moves right now"]
        for m in moves:
            lines.append(f"- {m['why']}\n  `{m['tool']}` with `{m['args']}`")
        sections.append("\n".join(lines))
    else:
        sections.append(
            "## Legal moves right now\nNone - it is not your turn, or the game is over. "
            "Use `duels_wait_for_my_turn` to block until it is your turn again."
        )

    return join_lines([*header, *sections])
