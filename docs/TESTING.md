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
