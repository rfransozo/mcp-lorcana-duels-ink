"""Check every action name we send against the ones Duels.ink actually sends.

Deliberately a script rather than a test: it needs the live site, and a test
suite that fails when somebody's wifi drops teaches people to ignore it.

The failure it guards against has no symptom. `START_GAME` reads perfectly,
was refused as "Invalid table action", and left the table in `assembling` -
which is indistinguishable from a table waiting for another player. It shipped,
and every test agreed with it, because the tests encoded the same guess.

    python scripts/action_audit.py

Exits non-zero when we send a name the site never does.
"""

import asyncio
import pathlib
import re
import sys

import httpx

BASE = "https://duels.ink"
SRC = pathlib.Path(__file__).resolve().parent.parent / "src"

# Uppercase literals that are ours rather than the wire's. Every `DUELS_`
# name is one of our environment variables - the site has never sent an action
# under that prefix - so they are excluded by rule, not one at a time.
NOT_ACTION_PREFIXES = ("DUELS_",)
NOT_ACTIONS = {
    "SERVER_NAME", "HEARTBEAT_SECONDS", "POLL_SECONDS", "INFO",
    "UPSTREAM_PORT_START", "MARKDOWN", "JSON", "GET", "POST", "PUT", "PATCH",
    "DELETE", "HEAD", "UTF",
}


def ours() -> dict[str, set[str]]:
    """Every uppercase literal in src/, and which file it came from."""
    found: dict[str, set[str]] = {}
    for path in SRC.rglob("*.py"):
        for name in re.findall(r'"([A-Z][A-Z0-9_]{3,})"', path.read_text(encoding="utf-8")):
            if name not in NOT_ACTIONS and not name.startswith(NOT_ACTION_PREFIXES):
                found.setdefault(name, set()).add(path.name)
    return found


async def theirs() -> set[str]:
    """Action types the site's own JavaScript sends.

    A Vite build: the HTML names the entry chunk and the entry imports the
    rest, so both are read. Only `{type:"..."}` counts - a bare string might be
    a log event, and FREE_UNDO is exactly that trap.
    """
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        home = await client.get(BASE)
        srcs = set(re.findall(r'"(/assets/[^"]+\.js)"', home.text))
        entry = sorted(s for s in srcs if "/index-" in s)
        if entry:
            js = (await client.get(BASE + entry[0])).text
            srcs |= {"/assets/" + n for n in re.findall(r'"\./([^"]+\.js)"', js)}

        sent: set[str] = set()
        for src in sorted(srcs):
            try:
                js = (await client.get(BASE + src)).text
            except Exception:
                continue
            sent |= set(re.findall(r'\{type:"([A-Z][A-Z0-9_]{2,})"', js))
        return sent


async def main() -> int:
    mine = ours()
    try:
        site = await theirs()
    except Exception as exc:
        print(f"Could not read the site's bundle ({type(exc).__name__}); skipping.")
        return 0
    if not site:
        print("The bundle yielded no action names - the build layout has changed.")
        return 1

    confirmed = sorted(a for a in mine if a in site)
    invented = sorted(a for a in mine if a not in site)

    print(f"{len(confirmed)} of our action names are ones the site sends.")
    if invented:
        print("\nNot sent by the site anywhere - either renamed, or never real:")
        for name in invented:
            print(f"  {name:26} in {', '.join(sorted(mine[name]))}")
        return 1
    print("Nothing invented.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
