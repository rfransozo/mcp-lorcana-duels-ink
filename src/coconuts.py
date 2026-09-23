"""The Coconut pool, which the card API does not serve.

`GET /api/cards/coconut-011` is a 404, so a Coconut in play used to render as
the bare id "coconut-018" - the one card on the table whose rules nobody could
read. The records live in the site's own client bundle; `scripts/
refresh_coconuts.py` extracts them into `coconuts.json` beside this file.
"""

import json
import pathlib
from functools import lru_cache
from typing import Optional

POOL_PATH = pathlib.Path(__file__).with_name("coconuts.json")


@lru_cache(maxsize=1)
def pool() -> dict:
    try:
        data = json.loads(POOL_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def record(definition_id: Optional[str]) -> Optional[dict]:
    """A Coconut as a catalog-shaped record, or None for anything else."""
    if not definition_id or not str(definition_id).startswith("coconut-"):
        return None
    entry = pool().get(definition_id)
    if not isinstance(entry, dict) or not entry.get("name"):
        return None
    return {
        "fullName": entry["name"],
        "type": "coconut",
        "colors": entry.get("colors") or [],
        "rulesText": entry.get("text"),
    }
