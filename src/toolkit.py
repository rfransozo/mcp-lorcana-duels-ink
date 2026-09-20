"""Cross-cutting helpers shared by every tool module.

Tools return a readable error string rather than raising, so an agent that hits
a bad argument or an expired cookie is told what to do next and can keep
working instead of aborting the whole turn.
"""

import functools
import logging
from typing import Any, Awaitable, Callable

from .client import DuelsError

log = logging.getLogger("duels_mcp.tools")


async def progress(ctx: Any, fraction: float, message: str) -> None:
    """Best-effort progress ping.

    Progress notifications are cosmetic, and the client may not support them at
    all - a failure here must never take the tool down with it.
    """
    try:
        await ctx.report_progress(progress=fraction, message=message)
    except Exception:  # noqa: BLE001 - cosmetic only
        pass


def tool_errors(fn: Callable[..., Awaitable[str]]) -> Callable[..., Awaitable[str]]:
    """Convert DuelsError (and unexpected failures) into an actionable message."""

    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> str:
        try:
            return await fn(*args, **kwargs)
        except DuelsError as exc:
            return f"Error: {exc}"
        except ValueError as exc:
            return f"Error: invalid argument - {exc}"
        except Exception as exc:  # noqa: BLE001 - last line of defence
            log.exception("Unhandled error in %s", fn.__name__)
            # Never leak internals to the client; log them server-side instead.
            return (
                f"Error: {fn.__name__} failed unexpectedly ({type(exc).__name__}). "
                "If this repeats, the Duels.ink private API may have changed."
            )

    return wrapper
