# Duels.ink private protocol notes

Everything here was observed on the wire against the live site. Duels.ink
publishes no API documentation, so this file is the reference for maintaining
the server when something breaks.

Build id observed while mapping this: `caee107` (`GET /api/version`).

## Authentication

| Mode | Credential | Unlocks |
|---|---|---|
| Anonymous | none | card catalog, public decks, **bot games** |
| Anonymous game | `sessionId` from `create-bot-game`, passed as `?sessionId=` | that game's `ws-token` |
| Signed in | better-auth session **cookie** | decks, tables, matchmaking, friends, history |

`Authorization: Bearer <session token>` is **rejected with 401** - the cookie is
the only accepted credential. Cookie names: `__Secure-better-auth.session_token`
(HTTPS) or `better-auth.session_token`. Sessions last ~30 days.

## Starting a bot game

```
POST /api/game/create-bot-game   {"playerDeckCardIds": ["11-81", "11-81", ...]}
  -> {"gameId": "<uuid>", "sessionId": "<uuid>|null"}
```

* `sessionId` is non-null only for anonymous callers; it is that game's credential.
* Only **one bot game at a time**: a second call answers
  `409 {"error":"bot_game_in_progress","existingGameId":"<uuid>"}`.
* Deck validity is not strictly enforced here (a 96-card deck was accepted).

## WebSocket

```
GET /api/game/{id}/ws-token[?sessionId=...]  -> {"token": "<jwt>", "wsUrl": "wss://wsN.duels.ink"}
connect to: {wsUrl}/game/{id}?token={token}
```

The host is **sharded and unstable** - `ws`, `ws0`, `ws1`, `ws3` and `ws4` have
all been observed. Always use the `wsUrl` from the response; never hardcode.
(`ws1` serves `/presence`, which is a different socket entirely.)

### Client to server
```json
{"type": "action", "action": {...}, "requestId": "<uuid>"}
{"type": "ping"}
```

### Server to client
| type | payload |
|---|---|
| `init` | `{game, buildId}` - **complete** state; this is what makes reconnection a full resync |
| `game_update` | `{game}` |
| `game_log` | `{logs[], fromIndex, isGameFinished}` |
| `action_result` | `{success, ...}` |
| `heartbeat` | `{timestamp, stateVersion, buildId}` |
| `pong`, `player_disconnected`, `player_connected` | - |

## Action payloads (verified)

```json
{"type": "CHOOSE_STARTING_PLAYER", "choice": "play"}        // or "draw"
{"type": "MULLIGAN", "selectedCardIds": []}                  // [] keeps the hand
{"type": "ADD_TO_INK", "cardInstanceId": "<uuid>"}
{"type": "PLAY_CARD",  "cardInstanceId": "<uuid>"}           // + shiftTargetInstanceId,
                                                             //   singerInstanceIds, discardedCardIds
{"type": "QUEST",      "cardInstanceId": "<uuid>"}
{"type": "ATTACK",     "attackerInstanceId": "<uuid>", "targetInstanceId": "<uuid>"}
{"type": "END_TURN",   "expectedTurnNumber": N, "expectedCurrentPlayer": N}
{"type": "ABANDON_BOT_GAME"}
{"type": "CONCEDE"}

// board actions - field names differ from the ones above
{"type": "MOVE_TO_LOCATION", "characterInstanceId": "<uuid>", "locationInstanceId": "<uuid>"}
{"type": "ACTIVATE_ABILITY",  "cardInstanceId": "<uuid>", "abilityName": "<name>"}
{"type": "BOOST",             "cardInstanceId": "<uuid>"}
```

`MOVE_TO_LOCATION` takes **`characterInstanceId`**, not `cardInstanceId`;
sending the wrong one is rejected with "Character not on field!".

`choice` for the coin toss is `"play"` / `"draw"` - *not* "first"/"second".

## Prompts

`game.pendingPrompts` is a list. Every response nests `promptId` **inside**
`response`; putting it at the action's top level answers "Prompt not found".

```json
{"type": "RESPOND_TO_PROMPT", "response": {"promptId": "<id>", "type": "<kind>", ...}}
```

| Prompt `type` | Response `type` | Extra fields |
|---|---|---|
| `select_trigger` | `skip_trigger` / `resolve_trigger` | `triggerId` |
| `boolean` | `boolean` | `value` (bool) |
| `select_target` | `select_target` | `targetInstanceIds` (list) |
| `select_card` | `select_card` | `cardInstanceIds` (list) |
| `select_numeric` | `select_numeric` | `numericValue` |

Prompt objects carry what is needed to answer: `select_target` has
`validTargets`, `minSelect`, `maxSelect`, `intent`; `boolean` has `yesLabel`,
`noLabel`, `yesDescription`, `noDescription`, `recommendedChoice`;
`select_trigger` has `triggers[]` with `abilityName` and `abilityDescription`;
`select_card` lists its options under `cardInstanceIds`.

A prompt with `minSelect: 0` can be declined, and sometimes must be: Support
with no friendly character left offers only the opponent's, and accepting
buffs them. Declining is an empty `targetInstanceIds` / `cardInstanceIds`.

Still unresolved: answering an **optional** `select_trigger` with
`resolve_trigger` was rejected as "Invalid prompt response" in a live game,
while `skip_trigger` worked. Only one observation so far, so the accept verb
for optional triggers is not yet confirmed.

`select_card` answers under `cardInstanceIds`, **not** `selectedCardIds` - that
key belongs to `MULLIGAN`, and sending it here is simply ignored: no
acknowledgement, no error, the prompt just stays pending until the call times
out. Both keys look alike in the logs, so this one cost a turn to find.

## Card capability flags

`game.availableActions.cards[instanceId]` booleans observed in play:
`canInk`, `canPlay`, `canQuest`, `canChallenge`, `canSing`, `canMove`,
`canBoost`, plus the non-actions `canAffordInkCost`, `canBeSinger`,
`hasSingTogether` and `hasQuestAbility`.

Anything with a `has` prefix describes the card, never a move that can be made
now: `hasQuestAbility` appears on a character whose ink is still wet and which
therefore cannot quest at all.

`canMove` and `canBoost` are conditional on the board, which is why both were
once wrongly written off as nonexistent. `canMove` needs a location in play and
is answered with `MOVE_TO_LOCATION`. `canBoost` needs a Boost character *and*
enough ink; it carries `boostCost` and, when spent, `boostBlockedReason:
"Already used this turn"`. It is answered with `BOOST` carrying
`cardInstanceId`.

`effectiveStrength` and `effectiveLore` are the values after buffs, and they
are what the board actually uses - a boosted Hercules prints 0/3 in the
catalogue and hits for 3. `effectiveStrength` is only sent when a challenge is
computable, so the printed value is the fallback; `cardsUnder` is the other
hint that a character is bigger than its card.

Every refusal is explained, not just unplayable cards: `playBlockedReason`
("Need 3 ink"), `questBlockedReason` / `challengeBlockedReason` ("Ink dry (no
Rush)") and `inkBlockedReason` ("Already inked this turn").

### Activated abilities

Items and locations carry no `canActivate` flag. They carry a structured list
with the costs already resolved:

```json
"activatedAbilities": [
  {"name": "OUT OF SIGHT", "inkCost": 3, "exertCost": false, "banishCost": false,
   "discardCost": 0, "canActivate": false, "blockedReason": "Not enough ink (need 3)"}
]
```

Answered with `ACTIVATE_ABILITY` carrying `cardInstanceId` and `abilityName`.

### Challenge maths

A character that can challenge carries `challengeTargetInfo[]`, one entry per
legal target, with the outcome already computed: `damageToTarget`,
`willBanishTarget`, `damageToAttacker`, `willBanishAttacker`,
`targetResistReduction`, `attackerResistReduction`, `hasBodyguard`,
`hasEvasive`, `hasWard`, `isLocationTarget`. Alongside it: `effectiveStrength`,
`effectiveLore` and `challengerBonus`.

### Sing Together

A song with the keyword reports `hasSingTogether: true` from the first turn -
it describes the card, not an available move. What decides playability is
`singTogetherCost` against `totalAvailableSingerCost`, with `singerCosts`
giving each candidate's contribution. When they add up, `canSing` flips to true
and `validSingers` fills; the song is then played with `PLAY_CARD` carrying
**every** singer in `singerInstanceIds`, not just one.

## Game log

```json
{"id","timestamp","turnNumber","player","type","message","cardRefs":[{"id","name"}],"data"}
```

`message` contains `{card:N}` placeholders indexing into `cardRefs`.

## Decks

`GET /api/decks/{id}` returns `deck.cardIds` - a **flat list of 60 definitionIds**
with repeats (plus `deckEntries` grouped by quantity).

## Card catalog

`GET /api/cards?limit=100&offset=N` is public, ~3,178 cards, and supports `q`
and `set`. Its `id` is exactly the `definitionId` used by the game engine.
