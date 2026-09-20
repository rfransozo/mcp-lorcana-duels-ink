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
| `test_client.py` | Cookie handling, HTTP status to actionable message, ws-token URL composition |
| `test_cards.py` | Per-set lazy loading, batch resolution, search filters |
| `test_render.py` | Phase gates, zone filtering, move arguments, board description |
| `test_gamews.py` | Connect, action round-trip, reconnection, registry - against a real socket |
| `test_toolkit.py` | Error conversion, progress pings |
| `test_tools_contract.py` | Naming, annotations, schemas and docstrings across all 42 tools |
| `test_tools_offline.py` | The tools end to end, plus the property test below |
| `test_server.py` | `/health` and transport selection - the MCPize deploy contract |
| `test_live_smoke.py` | Opt-in, read-only, anonymous checks against the real site |

Fixtures in `tests/fixtures/` are written by hand from the shapes verified in
[PROTOCOL.md](PROTOCOL.md), rather than recorded traffic. The cards are real and
their values match `evals/duels_eval.xml`.

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

`.mcp.json` at the repo root registers the server for this project:

```json
{
  "mcpServers": {
    "duels": {
      "command": "C:\venvs\mcp-lorcana-duels-ink\Scripts\python.exe",
      "args": ["<absolute path>\src\server.py"],
      "env": {}
    }
  }
}
```

It is **gitignored** — both paths are absolute and specific to one machine.

Claude Code asks for approval the first time it starts a project-scoped server.
Restart the session (or reconnect) after creating the file.

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
