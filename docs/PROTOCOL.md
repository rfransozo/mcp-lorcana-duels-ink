# Duels.ink private protocol notes

Everything here was observed on the wire against the live site. Duels.ink
publishes no API documentation, so this file is the reference for maintaining
the server when something breaks.

Build id observed while mapping this: `0df7355` (`GET /api/version`).
Re-verified against the live site on 2026-09-22: table config keys and
their enums, the lobby socket, the victory and undo actions, and
`START_TABLE`.

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

Games against people are timed and bot games are not. The live clock is in
`gameState.timerView`:

```json
{"myTimeRemainingMs": 114623, "opponentTimeRemainingMs": 120000,
 "serverTimestamp": 1789944585109,
 "myTimerTicking": false, "opponentTimerTicking": false}
```

It is a chess clock: two minutes a turn, with about 45 seconds credited back on
ending one. Running out does not pass the turn - it **eliminates** the player,
board and all (see Multiplayer state). `roomView.timerPreset` names the setting
and is `"none"` in bot games.

`timerView` has two fields at a table as well as in a duel, so it shows your
clock and the clock of whoever is currently to move, not one per seat.

`turnGateState` is *not* this. It holds per-turn counters - whether the ink for
the turn has been used, what has been played - and carries no time at all.

The game log narrates the same clock, with the numbers under `data`:

```json
{"type": "TIMER_STARTED",   "player": 2, "data": {"timeRemainingMs": 120000},
 "message": "Your timer started (2:00)"}
{"type": "TIMER_INCREMENT", "player": 2, "message": "You gained 45 seconds (turn end)"}
```

Use `timerView` rather than these: it is the current value, while the log is a
record of events. Every log entry also carries `id`, `timestamp`, `turnNumber`
and a `data` object that `duels_get_game_log` does not surface.

## Multiplayer state

Everything below was read off one live four-player Coconut game, seat 2.

**`opponents` is the list; `opponent` is the first of it.** A duel sends only
the singular, a table sends both, and the singular keeps pointing at one real
player either way. So a reader that only knows `opponent` does not crash, does
not log, and does not miss a key - it silently renders a four-player game as a
duel, with two players' boards and lore simply absent. There is no way to tell
from the payload alone that anything is missing.

Per-player keys a two-player reader never needed:

| key | note |
|---|---|
| `name` | on `opponents[]` entries only - the singular `opponent` has none |
| `seat` / `playerNumber` | which seat this is |
| `coconutCard` | a plain card object, not a list |
| `coconutAbilitySpent` | whether its once-per-game ability is gone |
| `eliminated` | out, while the game carries on |
| `handCount`, `hasPendingPrompts` | |

`playerNames` holds only `{"1": ..., "2": ...}` even at a four-player table, so
seats 3 and 4 have to be named from `opponents[].name`.

**There is no `loreToWin` in the state.** The UI shows `/25` for Coconut and
the wire says nothing, so the threshold has to come from `gameVariant`
(`"coconut"` -> 25, Pack Rush -> 15, otherwise 20) unless a table overrides it.

### Elimination, and how a turn is lost

Running the clock to zero does **not** pass the turn. It eliminates the player
where they stand: `myPlayer` comes back with hand, deck, inkwell, discard and
field all empty and `lore` frozen at whatever it was, while the other seats
play on. A four-player game continued for three more turns after one seat
timed out at 8 lore.

`winner` is a **player number**, not a name - `4` at a table means the player
in `opponents[]` whose number is 4.

### Shift

`canShift` on a hand card is answered by the ordinary `PLAY_CARD` action with
`shiftTargetInstanceId` set to the board character being shifted onto. There is
no separate SHIFT action.

## Stalling, absence and undo

A stalled game does not end itself. `timerView` carries nine fields beyond the
two clocks, and every one of them was invisible while the state advertised it:

```json
{"activePlayer": 3, "opponentZeroCount": 1,
 "canDeclareVictory": true, "canClaimAfkVictory": false,
 "canClaimPreGameVictory": false, "canPingOpponent": true,
 "wasAfkPinged": false, "afkResponseExhausted": false,
 "opponentLastGameActionAt": 1790036586107}
```

`activePlayer` matters at a table: with three opponents, "the opponent's
clock" names nobody.

The action names are not guessable and were read out of the site's own
JavaScript (a Vite build, `/assets/*.js`; `game-engine-*.js` holds most of it):

| capability | action |
|---|---|
| `canDeclareVictory` | `DECLARE_VICTORY` |
| `canClaimAfkVictory` | `CLAIM_AFK_VICTORY` |
| `canClaimPreGameVictory` | `CLAIM_PREGAME_VICTORY` |
| `canPingOpponent` | `PING_OPPONENT` |
| `wasAfkPinged` | `RESPOND_TO_AFK_PING` - **ignoring it hands them the claim** |

### Undo and the removal vote

The same bundle carries the send sites, so these are payloads rather than
guesses:

```js
{type:"REQUEST_UNDO", scope}          {type:"RESPOND_TO_UNDO", accept}
{type:"CANCEL_UNDO"}                  {type:"CANCEL_ABILITY"}
{type:"REWIND_ABILITY_CHOICE"}        {type:"QUICK_CHAT", messageId}
{type:"CALL_REMOVAL_VOTE", targetPlayer}
{type:"RESPOND_TO_REMOVAL_VOTE", accept}
{type:"CANCEL_REMOVAL_VOTE"}
```

**`FREE_UNDO` and `CHOICE_UNDO` are not actions.** They appear alongside the
others in the bundle and only ever in the game-log switch, so tools for them
would send types nothing handles - the same 200-and-nothing failure as the bare
`SET_READY`.

Both answers ride as a plain `accept` boolean; there is no RESPOND_TO_UNDO_YES.

State: `canRequestUndo`, `allowFreeUndo`, `undoTimeCost`,
`nextUndoHasRevealedInfo`, `canUndoChoice`, `canCancelInProgressAbility`,
`canRewindAbilityChoice`, `undoDeclineLimitReached`, and `removalVoteCalled`
while a vote is live. A finished game also carries `victoryReason`, without
which a win on lore and a win on a timeout read identically.

## The Coconut pool

**Coconut cards are not in `/api/cards`.** `GET /api/cards/coconut-011` is a
404 and `duels_get_card` finds nothing, which is why a Coconut renders as a
bare id. The records live in the client's `cards-data-*.js`, one JSON object
per card:

```json
{"id":"coconut-011","name":"Mr. Incredible","subtitle":"Super Strong",
 "full_name":"Mr. Incredible - Super Strong","colors":["ruby"],
 "associated_card":"Mr. Incredible - Super Strong",
 "associated_card_ids":["12-127","12-234"],
 "max_copies_of_associated":4,
 "translations":{"en":{"rules_text":"Whenever you play a Super character..."}}}
```

There are 25 of them. `scripts/` has no extractor; the one used lives in the
session notes.

**`max_copies_of_associated` is a deckbuilding exception**, and it explains a
detail that otherwise looks like a bug: a singleton Coconut precon lists `4x`
of one card. That card is the **associated card** (`12-127`), an ordinary card
you draw and play. It is *not* the Coconut. The Coconut (`coconut-011`) is a
separate permanent that is never in the 60, never drawn and never played.

### A Coconut game can start without Coconuts

Seen in a real 1v1: `gameVariant: "coconut"`, `loreToWin: 25`, both seats
reporting `hasCoconut: true` with a `coconutCardId` - and **no `coconutCard`
key on any player at all**. Not null; absent. 340 log entries never mentioned
one, and the format's ability never appeared in a single legal move.

The same diagnosis against a four-player game found `opponent.coconutCard`
present and populated, with `justPlayed: false` - so in a working game the
Coconut is simply in play from the first state, before the mulligan.

The decks were identical to the precons they were cloned from (same 60 ids,
same `coconutCardId`, same `4x`), so this is not a deckbuilding fault. The
remaining difference is the game itself; the table had been started with two of
its four seats filled. Unconfirmed, and worth re-testing before trusting a
Coconut table.

The renderer now counts Coconuts in play and says plainly when a Coconut game
has none, because the absence is otherwise indistinguishable from "the ability
is not available right now".

A Coconut **playground** since built both sides' Coconuts without trouble, and
our own renderer showed them. So neither the format nor the renderer is at
fault, and the 1v1 stands as the anomaly it looked like.

## The playground

A sandbox where you control both seats. The only way to exercise a format
without finding opponents, and the only way to reach a position on purpose.

```http
POST /api/playground/create
{"firstPlayer": 1, "name": "...", "skipSetup": false, "deckOutWin": false,
 "player1DeckCardIds": ["10-71", ...], "player2DeckCardIds": [...],
 "gameVariant": "coconut",
 "player1CoconutCardId": "coconut-011", "player2CoconutCardId": "coconut-002"}
```

It takes **card lists, not deck ids**, and `skipSetup` must be true when
neither side has a deck - there is nothing to mulligan. `GET
/api/playground/access` answers `{"hasAccess": bool}`; the entitlement is per
account.

**`/api/playground/create-from-spec` is a different endpoint.** It is the
replay-analysis path and wants a serialised position under `seed`. Our tool was
pointed at it, so every playground it ever tried to create was refused with
`Missing or invalid "seed"` - a tool that had never once worked.

Arrange the board with the `PLAYGROUND_*` actions. Each carries `actingAs`,
the seat you are speaking for:

| Action | Payload |
|---|---|
| `PLAYGROUND_SPAWN_CARD` | `{cardId, zone, targetPlayer, variant, actingAs}` |
| `PLAYGROUND_REMOVE_CARD` | `{cardInstanceId, actingAs}` |
| `PLAYGROUND_MOVE_CARD` | `{cardInstanceId, toZone, toPlayer, actingAs}` |
| `PLAYGROUND_SET_DAMAGE` | `{cardInstanceId, damage, actingAs}` |

Spawning takes the **catalog** id (`cardId: "10-16"`), not `definitionId` and
not an instance id. Zones are `hand`, `inkwell`, `field`, `items`, `discard`
and `deck`.

### A playground holds the Coconut but does not grant its ability

It **does** put a Coconut in play for each side, from the first state, before
the mulligan - read back through our own tools.

It does **not** grant the ability, and that was tested rather than assumed.
`coconut-004` is Stitch - Rock Star: *"Once during your turn, you may play a
character with cost 2 or less for free."* With a cost-2 character in hand and
**zero** available ink, the wire itself said:

```json
"cards": {
  "<hand card>": {"canPlay": false, "playBlockedReason": "Need 2 ink", ...},
  "<coconut-004>": {}
}
```

Nothing on the Coconut, and the free play not even considered. That is not our
renderer filtering: the raw frame carries no move we drop. The whole state
mentions a coconut in exactly three places - `myPlayer.coconutCard`,
`opponent.coconutCard`, `opponents[n].coconutCard` - and nowhere else. There is
no `coconutAbilitySpent`, no lock, no per-turn counter; the bundle's
`coconutAbilitySpent` is a locale string, not a field. So there is no flag we
are failing to read.

The likely reason, unproven: **a playground reports `isScenario: true`**, and a
scenario appears not to run the format's own engine. The renderer now says so
in the header, because a sandbox that silently withholds an ability otherwise
reads as a broken game.

The consequence for testing: a playground can prove a Coconut is **present**
and can exercise everything the normal tools do. It cannot exercise the
format's ability. That still needs a real Coconut table.

Playground results are not games: they do not count, rank or appear in history.

## Variants

The client carries the whole table, so these are the real numbers rather than
inferred ones:

| id | hand | lore to win | deck out | starting ink | OTD ink drops |
|---|---|---|---|---|---|
| `standard` | 7 | 20 | lose | 0 | 0 |
| `pack_rush` | 5 | 15 | **recycle** | 2 | 0 |
| `coconut` | 7 | **25** | lose | 0 | 0 |
| `ink_drop` | 7 | 20 | lose | 0 | **1** |

`ink_drop` was not on our list: the player going second starts with one free
ink drop. The site's own API docs call it an experiment, 1v1 constructed only,
and not combinable with Coconut. `deckOutBehavior: "recycle"` means Pack Rush
does not lose on an empty deck - it reshuffles.

## One socket per player

Duels.ink allows **one game socket per player** and closes the older one with
`4008 Stale connection`. That makes a second connection a fight rather than a
failure: a watcher evicts the server's socket, the server's next call
reconnects and evicts the watcher.

So a watcher survives exactly as long as the MCP server stays idle - while
spectating, after elimination, or once the game is over. It dies within
seconds of you playing a turn. Watching a game *you* are playing therefore
needs no socket at all: `src/telemetry.py` records what the server already
received, behind `DUELS_TELEMETRY_PATH`.

## Prompts name nothing

A prompt lists instance ids and stops there. `chooseCardFromRevealed` offers
four ids; nothing in the prompt says what the cards are, so a choice made from
the prompt alone is a choice made blind - which is how two picks were made by
taking the first id in a ranked game.

The names are in the state, in three places the renderer had to learn:

* **`myPlayer.revealedCardsThisTurn`** (and the same on each opponent) - a
  zone of its own, and the only place the cards behind a reveal prompt exist.
* **`promptSourceCard`** at the top level - the card the prompt came from,
  which is not necessarily in any zone.
* **`zoneGroups`** on a `select_card` prompt - the same ids split by the zone
  they came from.

The site's own client builds exactly this index before it draws the dialog.

**`select_card` answered with the wrong field is answered, not refused.** Sent
`targetInstanceIds` instead of `cardInstanceIds`, the server resolves the
prompt as "chose nothing" and moves on - the four cards go to the bottom of the
deck and the choice is gone. With `minSelect: 0` nothing anywhere reports a
problem.

`undoTimeCost` is in **milliseconds**, like every other duration here. Read as
seconds it renders "30000s off your clock".

## Locations

A character at a location carries `locationInstanceId`; the location itself is
a card in that player's `items`, and the name only exists there. One board held
five locations granting Evasive, +1/+1 and free movement to whoever stood on
them, and none of it was visible because the id was never read. Characters also
carry `hasQuestedThisTurn`, `usedAbilitiesThisTurn`, `lastDamageSource` and
`lastDamageWasChallenge`.

## Game log

```json
{"id","timestamp","turnNumber","player","type","message","cardRefs":[{"id","name"}],"data"}
```

`message` contains `{card:N}` placeholders indexing into `cardRefs`.

## Decks

`GET /api/decks/{id}` returns `deck.cardIds` - a **flat list of 60 definitionIds**
with repeats (plus `deckEntries` grouped by quantity).

## Tables

A table is a private lobby that seats 2 to 4 players. It is the only way into
the Coconut format and the only way into a game with more than one opponent:
neither has a queue, and neither runs against the bot.

```
POST /api/table/create              {applyDefaultPreset, gameFormat, timerPreset,
                                     loreToWin, visibility, ...}   <- top level
                                    -> {tableId, url, view}
GET  /api/table/{id}/view           -> {"view": {...}}      <- note the envelope
POST /api/table/{id}/action         {"action": {...}}
GET  /api/table/{id}/ws-token       -> {"token": "..."}     <- no host, see below
GET  /api/home/data                 -> {openTables, liveGames, stats}
```

The `config` that comes back is **sparse**: a default table carries three keys
and the rest appear only once set, so an absent key means the default rather
than false.

**Creation takes its settings at the top level.** A nested `config` object is
accepted and silently dropped, so a table created "as Coconut" that way comes
out Core. `url` is the shareable invite link. `seats` is the exception: no
creation field sets it, so it needs an `UPDATE_SETTINGS` straight after.

**The view arrives one level down**, as `{"view": {...}}`, and an action's
answer does the same alongside `success`. Reading the outer object yields a
status of `None` and a table that looks empty rather than unreachable.

### Actions

| type | body | note |
|---|---|---|
| `JOIN_TABLE` | `{username}` for guests | takes a free seat |
| `LEAVE_TABLE` | | |
| `SET_DECK` | `{deckId, deckCardIds, coconutCardId}` | see below |
| `SET_READY` | `{ready: true\|false}` | see below |
| `ADD_BOT_SEAT` | | |
| `KICK_SEAT` | `{seatIndex}` | also how a bot is removed |
| `UPDATE_SETTINGS` | `{config: {...}}` | partial patch |
| `START_TABLE` | | **not** `START_GAME`, which is refused as *"Invalid table action"* |
| `CANCEL_TABLE` | | |

`SET_READY` carries its value - there is no `SET_UNREADY`, and sending the
bare type is answered `200 {"success": true}` while the seat stays unready.

### Config

Every value below was set against a live table and read back. The server names
its refusals, under `code: "invalid_setting"`, so the accepted sets are
enumerated rather than guessed.

| key | accepted | note |
|---|---|---|
| `gameFormat` | `CoreConstructed`, `InfinityConstructed`, `Coconut` | bare `Core`/`Infinity`/`NoLimit` are refused as *"Invalid format"* |
| `gameMode` | `constructed`, `sealed`, `pack_rush`, `draft` | **stored as `deckType`**; lowercase and snake_case only - `Sealed`, `PackRush` and `Draft` are all refused |
| `matchFormat` | `bo1`, `bo3` | "Best of 1/3". `bo1` is the default and **clears the key** rather than storing it |
| `privateUndoConfig` | `{mode: unlimited\|timed\|disabled}` | "Free Undo". `timed` fills in `undoTimeCostSeconds: 30` itself; sending the seconds alongside is *"Invalid undo settings"* |
| `openSeats` | 2 to 4 | the only seat lever there is |
| `maxSeats` | - | **ignored.** Every value from 1 to 8 answers 200 and none is stored; it stays 4 |
| `visibility` | `public`, `private` | `unlisted`/`friends`/`invite` are refused |
| `timerPreset` | `none`, `casual`, `standard`, `blitz` | |
| `loreToWin` | at least 1 to 100 | barely validated - 1 is accepted, so a table can be a one-lore race |
| `allowSpectators`, `revealHands`, `revealHandsToSpectators`, `afkPingEnabled`, `allowGuestInvites` | booleans | |

Setting `gameMode` materialises keys of its own: `sealed` adds `deckType`
and `sealedRules` `{minDeckSize: 40, maxInkColors, maxCopies}` and **clears
`gameFormat`**; `pack_rush` adds `gameVariant: "pack_rush"` and `packRushSets`
`[1..11]`.

Keys outside the list - `isPublic`, `randomizeSeats`, `password`,
`startingHandSize`, `spectatorChat`, `turnTimeSeconds`, `name`, `description` -
are answered 200 and never stored.

**The panel's labels are not the wire's values.** Table settings offers
*"Core"*, which is refused; on the wire it is `CoreConstructed`. It also offers
three things no spelling has reached: **No Limit** (`NoLimit`, `no_limit`,
`Unlimited` all *"Invalid format"*), **Ink Drop for Second Player**, and
**First player**. The last two are accepted-and-ignored under every name tried,
so either they travel outside `config` or the panel is ahead of the API.

**A bad value rejects the whole call.** `UPDATE_SETTINGS` is not a best-effort
patch: one unknown `gameMode` in a config of twenty keys returns
`400 {"error": "Invalid game mode"}` and applies none of them, which makes
every other key in that batch look unsupported. Send settings one at a time
when probing.

* The table's format decides which decks are legal in it: a three-ink Coconut
  deck is refused at a Core table with *"Deck has too many colors"*.
* Sealed and Draft are 1v1 - `openSeats: 3` at one is refused with *"This game
  mode is 1v1 only"* - so Constructed is what reaches three or four players.

### The lobby socket

```
{"type": "table_init",   "view": {...}}     on connect, the complete view
{"type": "table_update", "view": {...}}     on every change, also complete
{"type": "pong"}                            answers {"type": "ping"}
```

**The token endpoint names no host.** A game's token carries a `wsUrl` because
games are sharded across `ws`, `ws3`, `ws4`; a table's is a bare JWT and the
host is the unsharded `wss://ws.<site>`. Code that required `wsUrl` could never
open a lobby socket at all.

**Actions do not travel on it.** `{"type": "action", ...}` is accepted and does
nothing; every lobby action is a POST.

**The socket is the seat's presence.** `connected` on a seat means "holds a
socket", nothing more - measured on a live table, the seat read `false`, then
`true` while a socket was held, then `false` again a second after dropping it.
Without one, a seat that is named, decked and ready still looks abandoned to
the host.

### Seats

`{index, userId, username, connected, ready, hasDeck, hasCoconut,
coconutCardId, coconutCardVersion, deckId, deckCardIds, isBot, botDifficulty}`

**A seat does not infer the Coconut from the deck id.** `SET_DECK` with only
`deckId` and `deckCardIds` seats a Coconut deck without its Coconut: the seat
reports `hasCoconut: false` while still looking seated and ready, and the game
would begin without the card.

**A Coconut table cannot hold a bot.** Setting the format while one is seated
is refused with `400 {"code": "bot_no_coconut"}`, and the UI says *"Bots can't
play the Coconut format yet"*. Remove it with `KICK_SEAT` first.

The view also carries `mySeatIndex`, `isHost`, `isSpectator`, `spectators[]`,
`version`, and a `log[]` of every join and leave - one busy table had fifty
entries before anybody sat down, so it is not worth printing.

### Finding a table

`GET /api/home/data` returns `openTables[]` as
`{id, hostName, format, maxSeats, seatsFilled, timerPreset, createdAt,
lastActiveAt}`. Only tables set to `visibility: "public"` appear; the rest are
reachable by invite link only. There is no `/api/table/list` - every guess at
one is a 404.

## Coconut decks

A Coconut is a card chosen while deckbuilding that stays in play for the whole
game and is never part of the 60; at least one of its inks must be among the
deck's inks. It is the Coconut format, and it is in beta - the site says the
pool and the wording can still change.

The site states the format as: *"pick a Coconut card, singleton deck of 60+
cards in up to 3 inks, 25 lore to win"* - so the win threshold is **25, not
20**, and three inks are legal where two is the usual cap. Pack Rush is 15.
A table can also set its own `loreToWin`, so the number is never safe to
assume.

A deck is made Coconut-legal by one field:

```
PATCH /api/decks/{id}   {"coconutCardId": "coconut-011"}
  -> legalFormats becomes ["Coconut"], valid true
```

Three things about it are not guessable:

* **`POST /api/decks` accepts the field and drops it.** The deck comes back
  created, 201, with `coconutCardId: null`. It only takes on a PATCH, so
  creating a Coconut deck is always two requests.
* **Clearing is an explicit `null`**, which turns the deck back into
  `InfinityConstructed` - and invalid, if it has three inks, which Coconut
  decks generally do.
* **An unknown id is `400 {"error": "Unknown Coconut card"}`**, and the pool is
  not in `/api/cards`: `?type=coconut` returns nothing. The 25 ids run
  `coconut-001` to `coconut-025`, are rendered into `/cards/coconut` rather
  than served as JSON, and their art is at
  `cards.duels.ink/lorcana/en/coconut/`. There is no endpoint that lists them.

`/api/custom-cards/access` and `/api/cards/alpha-access` both report
`hasAccess: false` on an ordinary account and gate neither of these - Coconut
is not a permission.

## Card catalog

`GET /api/cards?limit=100&offset=N` is public, ~3,178 cards, and supports `q`
and `set`. Its `id` is exactly the `definitionId` used by the game engine.
