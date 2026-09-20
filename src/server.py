#!/usr/bin/env python3
"""duels_mcp - MCP server for Duels.ink, an unofficial Disney Lorcana simulator.

Entrypoint. MCPize starts this with `python src/server.py`, which runs the file
as a script rather than as a package, so the project root is put on sys.path
before importing anything from `src`.
"""

import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from starlette.requests import Request  # noqa: E402
from starlette.responses import JSONResponse  # noqa: E402

from src.app import SERVER_NAME, log, mcp  # noqa: E402

# Importing the tool modules registers their @mcp.tool definitions.
from src.tools import (  # noqa: E402,F401
    analysis,
    cards,
    decks,
    history,
    identity,
    ingame,
    modes,
    play,
    social,
)
from src.tools import resources  # noqa: E402,F401


# -----------------------------------------------------------------
# Health route — obrigatório para passar o health probe do MCPize
# ⚠️  Sem essa rota, o deploy retorna 404/406 e nunca entra em running
# -----------------------------------------------------------------
@mcp.custom_route("/health", methods=["GET", "HEAD"])
async def health(_request: Request) -> JSONResponse:
    """Health check endpoint for MCPize probes."""
    return JSONResponse({"status": "ok", "service": SERVER_NAME})


# =================================================================
# Entrypoint
# ⚠️  Detectar modo nuvem via UPSTREAM_PORT_START
# =================================================================
def main() -> None:
    port_start = os.getenv("UPSTREAM_PORT_START")
    if port_start:
        port = int(port_start)
        log.info("Starting %s in HTTP mode on port %d", SERVER_NAME, port)
        mcp.run(
            transport="streamable-http",
            host="0.0.0.0",
            port=port,
            path="/mcp",
            json_response=True,
            stateless_http=True,
            show_banner=False,
        )
    else:
        log.info("Starting %s in stdio mode", SERVER_NAME)
        mcp.run(transport="stdio", show_banner=False)


if __name__ == "__main__":
    main()
