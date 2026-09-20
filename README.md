# MCP Lorcana — Duels.ink

Play and manage **Disney Lorcana** on [Duels.ink](https://duels.ink) from an AI
assistant. 43 tools covering matches, decks, the card catalog, statistics,
replays and the social side of the site.

> Unofficial. Not affiliated with, endorsed by, or connected to Duels.ink,
> Ravensburger or Disney. Duels.ink is a fan-made community simulator.

## Why it works

Duels.ink computes the legal moves server-side. Every game state comes back
with an explicit list of what can be played right now — and when a card cannot
be played, the reason why:

```
## Your hand
- **Flotsam - Slippery as an Eel** (3 ink, 4/2, 1 lore, emerald) -> can: canInk, canPlay
  `01a0bc5a-c80f-7855-b0d4-862293642cde`
- **Education or Elimination** (4 ink, amethyst) -> blocked: Need 4 ink
  `01a0bc5a-c80f-7855-b0d4-861e0989d27d`

## Legal moves right now
- Quest with this character for lore: Flotsam - Slippery as an Eel
  `duels_quest` with `{'game_id': '...', 'card_instance_id': '...'}`
- End your turn
  `duels_end_turn` with `{'game_id': '...'}`
```

The assistant never has to know Lorcana's rules. It reads the board, picks from
`legal_moves`, and plays.

## Getting started

### 1. Play a game right now — no account, no setup

Bot games work anonymously. Ask your assistant:

> "Find a community deck and play a game of Lorcana against the bot"

That is the whole setup. Under the hood it runs:

```
duels_browse_public_decks   → 1000+ community lists, no auth
duels_start_bot_game        → returns the opening position
duels_get_game_state        → the board, and the moves that are legal
duels_quest / duels_play_card / duels_end_turn → play
```

Every state comes back with a `legal_moves` list, so the assistant plays by
picking from what the server allows rather than guessing at the rules.

### 2. Connect your account — for your own decks and ranked play

Optional, and only needed for anything tied to *you*: your saved decks, ranked
matchmaking, private tables, friends and match history.

Duels.ink signs in through Discord or Google and has no API keys, so the
credential is your session cookie:

1. Sign in at [duels.ink](https://duels.ink).
2. Open DevTools — <kbd>F12</kbd>, or right-click → Inspect.
3. Go to **Application** → **Cookies** → `https://duels.ink`.
4. Copy the value of **`__Secure-better-auth.session_token`**.
5. Paste it as `DUELS_SESSION_COOKIE` in your MCPize server settings.

Sessions last about 30 days. When it expires, tools that need your account
start failing with a message telling you to refresh it — run `duels_whoami` to
confirm, then repeat the steps above.

The cookie is only ever used to make requests to Duels.ink as you. Remove it at
any time and bot games and the card catalog keep working.

### 3. Check it worked

> "Am I signed in to Duels.ink, and which decks do I have?"

`duels_whoami` reports whether the cookie was accepted, and
`duels_list_my_decks` lists your decks once it is.

## Tools

**Identity** — `duels_whoami`, `duels_get_account_stats`

**Cards** — `duels_search_cards`, `duels_get_card`, `duels_resolve_cards`

Search the full ~3,200-card catalog by name, rules text, set, ink colour, cost,
rarity and format legality. No authentication required.

**Decks** — `duels_list_my_decks`, `duels_browse_public_decks`, `duels_get_deck`,
`duels_create_deck`, `duels_update_deck`, `duels_delete_deck`,
`duels_import_decklist`

`duels_import_decklist` takes a pasted text list ("4 Flotsam - Slippery as an
Eel") and resolves the names against the catalog, reporting anything it could
not match rather than dropping it silently.

**Matches** — `duels_start_bot_game`, `duels_create_table`, `duels_get_table`,
`duels_configure_table`, `duels_join_matchmaking`, `duels_leave_matchmaking`,
`duels_list_active_games`

**In game** — `duels_get_game_state`, `duels_get_legal_moves`,
`duels_get_game_log`, `duels_wait_for_my_turn`, `duels_ink_card`,
`duels_play_card`, `duels_quest`, `duels_challenge`, `duels_end_turn`,
`duels_respond_to_prompt`, `duels_send_game_action`, `duels_concede`

`duels_play_card` handles songs (paid by exerting singers), Shift, and costs
that require discarding. `duels_wait_for_my_turn` blocks on the live connection
instead of polling, so it returns the moment the opponent finishes.

**History** — `duels_get_match_history`, `duels_get_replay`,
`duels_get_leaderboard`, `duels_get_seasons`

**Other modes** — `duels_get_puzzle`, `duels_list_draft_decks`,
`duels_create_sealed`, `duels_create_playground`

The playground is a sandbox: spawn cards, set lore, damage and exertion, and
test an interaction without playing a real game into that position.

**Social** — `duels_list_friends`, `duels_send_friend_request`,
`duels_list_pending_invites`

## Resources

| URI | Contents |
|---|---|
| `duels://cards/{definition_id}` | One card's full record |
| `duels://game/{game_id}/state` | A live game's current state |
| `duels://game/{game_id}/log` | A live game's narrated log |
| `duels://deck/{deck_id}` | A deck, with every card resolved |

## Example prompts

- "Search for Evasive characters that cost 3 or less"
- "Show me my decks, then start a bot game with the ruby one"
- "What's the board? What should I play?"
- "Import this decklist and call it Ramp v2: ..."
- "How did my last five ranked games go?"
- "Who's at the top of the leaderboard this season?"

## Notes

Every tool returns human-readable Markdown by default; pass
`response_format="json"` for machine-readable output. List tools paginate with
`limit` and `offset` and report `has_more` and `next_offset`.

Tools that change something are marked as such, and the destructive ones
(`duels_delete_deck`, `duels_concede`) say so in their annotations so a client
can prompt before running them.

Ranked matchmaking pairs you against real people. Decide deliberately before
letting an assistant play unattended in ranked queues.
