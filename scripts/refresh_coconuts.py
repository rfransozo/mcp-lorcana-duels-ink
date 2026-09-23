"""Refresh src/coconuts.json from the site's data chunk.

Run it when a new set adds Coconuts; the server reads the file at start.

The pool is not in `/api/cards` - `duels_get_card('coconut-011')` finds nothing,
which is why a Coconut renders as a bare id in our own game states. The real
records are JSON inside the client's `cards-data-*.js`, one object per card:

    {"id":"coconut-011","name":"Mr. Incredible","subtitle":"Super Strong",
     "full_name":"...","colors":["ruby"],"associated_card":"...",
     "associated_card_ids":["12-127","12-234"],"max_copies_of_associated":4,
     "translations":{"en":{"rules_text":"..."}}}

`max_copies_of_associated` is the format rule that explains why a singleton
precon lists `4x` of its namesake card.
"""

import asyncio
import json
import pathlib
import re

import httpx

BASE = "https://duels.ink"
OUT = pathlib.Path(__file__).resolve().parent.parent / "src" / "coconuts.json"


async def chunk(client: httpx.AsyncClient) -> str:
    home = await client.get(BASE)
    srcs = set(re.findall(r'"(/assets/[^"]+\.js)"', home.text))
    entry = sorted(s for s in srcs if "/index-" in s)
    if entry:
        js = (await client.get(BASE + entry[0])).text
        srcs |= {"/assets/" + n for n in re.findall(r'"\./([^"]+\.js)"', js)}
    data = [s for s in srcs if "cards-data" in s]
    return (await client.get(BASE + data[0])).text if data else ""


def balanced(js: str, start: int) -> str:
    """The JSON object literal beginning at `start`, brace-matched."""
    depth, i, in_str, esc = 0, start, False, False
    while i < len(js):
        ch = js[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return js[start:i + 1]
        i += 1
    return ""


async def main() -> None:
    async with httpx.AsyncClient(timeout=240, follow_redirects=True) as client:
        js = await chunk(client)
    if not js:
        print("no cards-data chunk")
        return
    print(f"{len(js):,} bytes")

    pool: dict[str, dict] = {}
    for match in re.finditer(r'\{"id":"(coconut-\d{3})"', js):
        cid = match.group(1)
        if cid in pool:
            continue
        try:
            record = json.loads(balanced(js, match.start()))
        except ValueError:
            continue
        pool[cid] = {
            "name": record.get("full_name"),
            "colors": record.get("colors"),
            "text": ((record.get("translations") or {}).get("en") or {}).get("rules_text"),
            "associated_card": record.get("associated_card"),
            "max_copies": record.get("max_copies_of_associated"),
        }

    OUT.write_text(json.dumps(pool, indent=2, ensure_ascii=False), encoding="utf-8")
    for cid in sorted(pool):
        c = pool[cid]
        inks = "/".join(c["colors"] or [])
        print(f"\n{cid}  {c['name']}  [{inks}]  x{c['max_copies']} {c['associated_card']}")
        print(f"    {c['text']}")
    print(f"\n{len(pool)} coconuts -> {OUT}")


asyncio.run(main())
