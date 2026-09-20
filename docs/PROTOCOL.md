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

## Matchmaking

Joining is three concurrent things, not one request:

```
POST /api/matchmaking/join       {deckId, deckCardIds, queueId, competitiveOnly}
GET  /api/matchmaking/events     Server-Sent Events, carries the pairing
POST /api/matchmaking/heartbeat  about once a second, for as long as you wait
```

And the pairing itself has to be accepted, inside a window:

```
match_found    {matchId, expiresAt}      <- about 15 seconds to answer
POST /api/matchmaking/accept {matchId}   -> {success, bothAccepted}
opponent_ready -> match_accepted -> game_starting {gameId}
```

The event sequence recorded end to end, from a real queue:

```
init            {inQueue, position, pendingMatchId, pendingMatchExpiresAt, activeGameId}
match_found     {matchId, expiresAt}
opponent_ready  {matchId}
match_accepted  {matchId}
game_starting   {gameId}
```

`init` arrives on connect and carries `activeGameId` when reconnecting to a
queue that has already produced a game - worth reading rather than waiting for
a `game_starting` that has already been and gone. `match_cancelled` means the
pairing fell through (usually the other side let the window lapse) and you are
still queued, so it is not a reason to stop waiting.

**There is no way to accept late.** Fifteen seconds is shorter than a round
trip through a caller that has to be asked, so the client accepts as soon as
`match_found` lands. A recording showed an `opponent_ready` for a pairing this
side never answered: a person had accepted and was left waiting on a client
that was only listening for a game that would never start.

**The heartbeat is what makes the entry real.** Without it `join` still
returns `{success, position, estimatedWait, queueId}` and calling it again
still reports a position, but nobody is ever paired with you. This is not
visible from the API alone - it took watching the site to find.

Re-POSTing `join` is not a way to poll: it re-enters the queue, moving you to
the back, and can undo a pairing already under way. On a match the client
POSTs `leave` itself and opens the game, so leaving is part of the success
path too. The pairing then shows up in `/api/account/active-games`, which is
enough to detect it without an SSE client - the events stream was seen
returning 503 mid-queue while the queue itself stayed healthy.

Queue ids do not match the UI labels: "Quick Play: Core BO1" is `quick-play`,
while `core-bo1` is the ranked "Core BO1 - Set 13" beside it.

## Prompts

`game.pendingPrompts` is a list. Every response nests `promptId` **inside**
`response`; putting it at the action's top level answers "Prompt not found".

```json
{"type": "RESPOND_TO_PROMPT", "response": {"promptId": "<id>", "type": "<kind>", ...}}
```

| Prompt `type` | Response `type` | Extra fields |
|---|---|---|
| `select_trigger` | `select_trigger` (accept) / `skip_trigger` | `triggerId` |
| `boolean` | `boolean` | `value` (bool) |
| `select_target` | `select_target` | `targetInstanceIds` (list) |
| `select_card` | `select_card` | `cardInstanceIds` (list) |
| `order_cards` | `order_cards` | `orderedCardInstanceIds` (list, ordered) |
| `select_numeric` | `select_numeric` | `numericValue` |

Prompt objects carry what is needed to answer: `select_target` has
`validTargets`, `minSelect`, `maxSelect`, `intent`; `boolean` has `yesLabel`,
`noLabel`, `yesDescription`, `noDescription`, `recommendedChoice`;
`select_trigger` has `triggers[]` with `abilityName` and `abilityDescription`;
`select_card` and `order_cards` list their options under `cardInstanceIds`.

A prompt with `minSelect: 0` can be declined, and sometimes must be: Support
with no friendly character left offers only the opponent's, and accepting
buffs them. Declining is an empty `targetInstanceIds` / `cardInstanceIds`.

Accepting a trigger echoes the prompt's own type, `select_trigger`; only
declining has a verb of its own, `skip_trigger`. The symmetrical-looking
`resolve_trigger` does not exist and is rejected as "Invalid prompt
response" - which hid the problem for a while, since declining worked and
only acceptance was broken.

`select_card` answers under `cardInstanceIds`, **not** `selectedCardIds` - that
key belongs to `MULLIGAN`, and sending it here is simply ignored: no
acknowledgement, no error, the prompt just stays pending until the call times
out. Both keys look alike in the logs, so this one cost a turn to find.

`order_cards` offers its cards under `cardInstanceIds` but answers under
`orderedCardInstanceIds` - the same ids back, in the order wanted. Answering
under `cardInstanceIds` is rejected. It took about fifteen payload variants to
land on, and the cost of each wrong one is high: the prompt cannot be cleared,
and anything queued behind it then refuses with "Cannot select trigger while
another ability is resolving", which wedges the turn rather than failing it.

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
computable, so outside combat it is simply absent.

There is nowhere else to get it. A card in play carries only `definitionId`,
`instanceId`, `damage`, `exerted`, `justPlayed`, `appliedEffects`,
`cardsUnder` and `hasQuestedThisTurn` - no strength field of any kind. Base
stats come from the catalogue via `definitionId`, temporary buffs sit in
`appliedEffects` and Boost-style ones in `cardsUnder`. When the engine does
not compute the total, the parts are all there is, so all three are
rendered.

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

## The clock (human games only)

Games against people are timed; bot games are not. `roomView.timerPreset` says
which - `"none"` in every bot game seen so far. Nothing in `gameState` carries a
countdown, so the only evidence of the clock is the game log:

```
[P2] Your timer started (2:00)
[P1] Opponent gained 45 seconds (turn end)
```

So it is a chess clock: a per-turn budget, observed at two minutes, plus about
45 seconds credited back when a turn is ended. `turnGateState` is *not* this -
it holds per-turn counters (inked yet, cards played) and says nothing about
time.

The practical consequence is that a caller cannot see its own clock from the
state at all, and a slow turn can lose a won game without any warning. Reading
the tail of the log is the only way to check.

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
