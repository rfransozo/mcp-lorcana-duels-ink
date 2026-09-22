# Testing

```bash
pip install -e ".[dev]"
pytest
```

The default run is **offline**: `httpx.MockTransport` stands in for the REST API
and a real WebSocket server started in-process stands in for the game socket.
No network, no account, no games created. It finishes in under ten seconds.

## Layout

| File | Covers |
|---|---|
| `test_formatting.py`, `test_models.py` | Pagination envelope, markdown helpers, shared parameter constraints |
| `test_client.py` | Cookie handling, HTTP status to actionable message, ws-token URL composition, SSE parsing |
| `test_tools_account.py` | Friends, invites, match history, replays, leaderboard, seasons, whoami |
| `test_tools_decks.py` | Building, renaming, deleting and importing decks; draft, sealed and playground |
| `test_tools_play.py` | Bot games, tables, and entering or leaving the matchmaking queue |
| `test_tools_tables.py` | Lobbies: the view envelope, seats, ready, formats, finding and joining |
| `test_tablews.py` | The lobby socket: connecting, resync, and holding the seat |
| `test_tools_ingame.py` | The game log, waiting for a turn, conceding, and the odd prompt shapes |
| `test_resources.py` | The `duels://` resources, which are addressed by URI rather than called |
| `test_matchmaking.py` | Queue heartbeat, accepting a pairing inside its window, falling back when the stream drops |
| `test_cards.py` | Per-set lazy loading, batch resolution, search filters |
| `test_render.py` | Phase gates, zone filtering, move arguments, board description |
| `test_gamews.py` | Connect, action round-trip, reconnection, registry - against a real socket |
| `test_toolkit.py` | Error conversion, progress pings |
| `test_tools_contract.py` | Naming, annotations, schemas and docstrings across all 50 tools |
| `test_tools_offline.py` | The tools end to end, plus the property test below |
| `test_server.py` | `/health` and transport selection - the MCPize deploy contract |
| `test_live_smoke.py` | Opt-in, read-only, anonymous checks against the real site |

Fixtures in `tests/fixtures/` are written by hand from the shapes verified in
[PROTOCOL.md](PROTOCOL.md), rather than recorded traffic. The cards are real and
their values match `evals/duels_eval.xml`.

## Coverage

```bash
pytest --cov=src --cov-report=json
python scripts/coverage_gate.py
```

The floor is **85% per module**, not 85% overall. A single total hides the one
case that matters: four modules sat under 45% - social, history, resources and
play - while the total read a comfortable 72% and nothing complained.
coverage.py only knows a global `fail_under`, so the per-module check is its
own script.

It is a script rather than a test because as a test it would fail whenever
someone ran a subset of the suite under coverage, which is a normal thing to do
and not a defect.

## The property test

`TestLegalMovesAreCallable` renders every game phase and checks that each move
the renderer advertises validates against the input schema of the tool it
names. Two shipped bugs came from moves an agent could not actually execute;
this is the test that makes that class of defect impossible to reintroduce.

## Live smoke

```bash
pytest -m live
```

Deselected by default. Only reads, only anonymous endpoints, never creates a
game - duels.ink is fan-made and community-run, and the suite should not put
load on it. `TestDrift` reports when the site's `buildId` has moved past the one
PROTOCOL.md was written against, which is the cue to re-verify the protocol
notes.

## Regression coverage

Every bug found while building the server has a test named after it. To confirm
the suite still catches them, reintroduce one deliberately (for example, add
`canMove` back to `CAPABILITY_TOOLS` in `src/render.py`) and check that `pytest`
fails. All seven known defects are covered.

## Dev dependencies

They live in `[project.optional-dependencies] dev` in `pyproject.toml`.
`requirements.txt` deliberately stays at the five runtime dependencies, because
that is the file MCPize installs during the Cloud Run build.

---

# Running it locally in Claude Code

To use the server from this repo instead of the deployed MCPize one — handy for
testing a change before it ships.

## Setup

The venv lives **outside** the repo on purpose. This folder is synced by Google
Drive, and a `desktop.ini` landing inside `site-packages` breaks
`jsonschema_specifications`, which takes FastMCP down with it. Keeping it out
also spares Drive from syncing thousands of package files.

```bash
python -m venv C:/venvs/mcp-lorcana-duels-ink
```

```bash
C:/venvs/mcp-lorcana-duels-ink/Scripts/python.exe -m pip install -r requirements.txt
```

## Wiring

Registered at **user scope**, in the `mcpServers` block of `~/.claude.json`:

```json
"duels": {
  "type": "stdio",
  "command": "C:\venvs\mcp-lorcana-duels-ink\Scripts\python.exe",
  "args": ["<absolute path>\src\server.py"],
  "env": { "DUELS_SESSION_COOKIE": "<optional>" }
}
```

User scope rather than a project `.mcp.json` on purpose: a project-scoped file
is only read when the session's own project root is this folder, so it silently
does nothing if the session happens to be rooted somewhere else. User scope
works from any directory.

Do not register it in both places under the same name.

Restart the Claude Code session after editing the config, and approve the
server when prompted.

## Using your own account

Anonymous mode works out of the box: bot games, the card catalog and public
decks. To unlock your decks, ranked play, tables, friends and history, put your
cookie in the `env` block:

```json
"env": { "DUELS_SESSION_COOKIE": "<__Secure-better-auth.session_token value>" }
```

The README's Getting Started has the click-by-click for finding it. Since
`.mcp.json` is gitignored, the cookie never reaches the repo — but it is
plain text on disk, so treat it like any other credential.

## Checking it works

Without starting Claude Code, this speaks the same protocol the client does:

```bash
C:/venvs/mcp-lorcana-duels-ink/Scripts/python.exe src/server.py
```

It waits on stdin. A clean run prints nothing to stdout — stdout is the
JSON-RPC channel, and anything else on it corrupts the protocol. Startup logs
go to stderr by design.

Once connected, `duels_whoami` is the fastest check: it reports whether a
cookie was accepted and which Duels.ink build the server is talking to.
