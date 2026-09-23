"""Record what the wire carries that the renderer does not read.

Off unless `DUELS_TELEMETRY_PATH` names a file, and it only ever appends JSON
lines, so it cannot change how a game plays.

This exists because the obvious way to find unread fields - a second socket
watching the game - does not work while you are playing. Duels.ink allows one
game socket per player and closes the older one with `4008 Stale connection`,
so a watcher and the server's own connection take turns evicting each other:
the watcher survives only while the server is idle, which is to say only while
you are not playing. Reading what the server already received sidesteps that
entirely.

Every field named here was learned the same way, by playing and finding
something missing afterwards: `coconutCard`, `revealedCardsThisTurn`,
`locationInstanceId`, the nine timer capabilities, `victoryReason`.
"""

import json
import os
import pathlib
from typing import Any, Iterable

ENV_VAR = "DUELS_TELEMETRY_PATH"

# Everything render_game_state reads today. Anything else is a candidate.
PLAYER_KEYS = {
    "hand", "field", "items", "inkwell", "discard", "deckCount", "lore",
    "inkDrops", "handCount", "coconutCard", "eliminated", "name",
    "playerNumber", "seat", "player", "revealedCardsThisTurn", "revealedHand",
    "revealedCards", "singRestrictions",
}
TOP_KEYS = {
    "id", "status", "turnNumber", "viewingAs", "currentPlayer", "stateVersion",
    "isBotGame", "timerView", "gameVariant", "loreToWin", "myPlayer",
    "opponent", "opponents", "availableActions", "winner", "pendingPrompts",
    "mulliganState", "coinToss", "firstPlayer", "playerNames",
    "opponentHasPendingPrompts", "victoryReason", "promptSourceCard",
    "canRequestUndo", "allowFreeUndo", "undoTimeCost", "nextUndoHasRevealedInfo",
    "canUndoChoice", "canCancelInProgressAbility", "canRewindAbilityChoice",
    "undoDeclineLimitReached", "removalVoteCall", "isScenario",
    "turnGateState", "removalVoteTargets", "waitingForOpponent",
    "activeSingRestrictions", "opponentSingRestrictions",
}
CARD_KEYS = {
    "definitionId", "instanceId", "damage", "exerted", "justPlayed",
    "appliedEffects", "cardsUnder", "effects", "locationInstanceId",
    "hasQuestedThisTurn", "wasChallengedThisTurn", "lastDamageWasChallenge",
    "lastDamageSource",
}
PROMPT_KEYS = {
    "id", "player", "type", "message", "required", "optional", "triggers",
    "options", "cards", "cardInstanceIds", "minSelect", "maxSelect",
    "resolvingTriggerId", "validTargets", "zoneGroups", "nonSelectableCardIds",
    "sourceCardInstanceId", "sourceAbility", "params", "intent", "canDecline",
    "destinationLabel", "yesDisabled", "noDisabled",
}


def _unread(seen: Iterable[str], known: set) -> list[str]:
    return sorted(k for k in seen if k not in known)


def observe(game: dict) -> dict:
    """What this state carries that nothing reads. Pure; safe to call always."""
    players = [game.get("myPlayer") or {}]
    players += [o for o in (game.get("opponents") or []) if isinstance(o, dict)]

    cards: set = set()
    for player in players:
        for zone in ("hand", "field", "items", "discard"):
            for card in player.get(zone) or []:
                if isinstance(card, dict):
                    cards.update(card)

    prompts: set = set()
    for prompt in game.get("pendingPrompts") or []:
        if isinstance(prompt, dict):
            prompts.update(prompt)

    found = {
        "turn": game.get("turnNumber"),
        "variant": game.get("gameVariant"),
        "top": _unread(game, TOP_KEYS),
        "player": _unread({k for p in players for k in p}, PLAYER_KEYS),
        "card": _unread(cards, CARD_KEYS),
        "prompt": _unread(prompts, PROMPT_KEYS),
    }
    return {k: v for k, v in found.items() if v not in (None, [], "")}


def note(game: object) -> None:
    """Append one line of observations, if telemetry is switched on.

    Never raises: a telemetry file that cannot be written is not a reason to
    fail a move in a timed game.
    """
    path = os.getenv(ENV_VAR)
    if not path or not isinstance(game, dict):
        return
    try:
        found = observe(game)
        # Only surprises are worth a line. Counting keys was wrong: `variant`
        # drops out when it is None, so a genuine finding could land on the
        # same count as a quiet turn and be discarded.
        if not any(found.get(scope) for scope in ("top", "player", "card", "prompt")):
            return
        with pathlib.Path(path).open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(found, ensure_ascii=False) + "\n")
    except Exception:
        return


def summarise(path: str) -> dict[str, Any]:
    """Collapse a telemetry file into counts per unread key."""
    totals: dict[str, dict[str, int]] = {}
    file = pathlib.Path(path)
    if not file.exists():
        return totals
    for line in file.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        for scope in ("top", "player", "card", "prompt"):
            for key in row.get(scope) or []:
                bucket = totals.setdefault(scope, {})
                bucket[key] = bucket.get(key, 0) + 1
    return totals
