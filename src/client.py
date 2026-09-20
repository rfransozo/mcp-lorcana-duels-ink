"""HTTP client for the Duels.ink private API.

Duels.ink has no public or documented API. Everything here was mapped from the
live application. Two facts drive the design:

* Authentication is a better-auth **session cookie**. A bearer token built from
  the same session value is rejected with 401 - only the cookie works.
* Most read-only catalog endpoints (cards, public decks, settings) need no
  authentication at all, so the server stays useful without a cookie.
"""

import logging
import os
from typing import Any, Optional

import httpx

log = logging.getLogger("duels_mcp.client")

DEFAULT_BASE_URL = "https://duels.ink"
REQUEST_TIMEOUT = 20.0

# better-auth uses the Secure-prefixed name over HTTPS and the plain name
# otherwise. The cookie is HttpOnly, so a user copying it out of DevTools may
# hand us either name, or just the bare value.
COOKIE_NAMES = (
    "__Secure-better-auth.session_token",
    "better-auth.session_token",
)

AUTH_HELP = (
    "Set DUELS_SESSION_COOKIE to your Duels.ink session cookie. To get it: sign in at "
    "https://duels.ink, open DevTools (F12) then Application > Cookies > https://duels.ink, "
    "and copy the value of the cookie named '__Secure-better-auth.session_token'. "
    "Sessions last about 30 days, so it needs refreshing periodically."
)


class DuelsError(RuntimeError):
    """An error worth showing to the agent, already phrased as a next step."""


class DuelsClient:
    """Async client for https://duels.ink/api/*.

    One instance lives for the whole server process (created in the FastMCP
    lifespan) so the connection pool and session cookie are reused.
    """

    def __init__(
        self,
        cookie: Optional[str] = None,
        base_url: Optional[str] = None,
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ) -> None:
        """
        Args:
            cookie: Duels.ink session cookie, or None for anonymous use.
            base_url: Override the site root (defaults to DUELS_BASE_URL or
                https://duels.ink).
            transport: Custom httpx transport. The test suite passes an
                httpx.MockTransport here so it can exercise the whole client
                without touching the network.
        """
        self.base_url = (base_url or os.getenv("DUELS_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
        self._cookie_header = self._build_cookie_header((cookie or "").strip())
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=REQUEST_TIMEOUT,
            follow_redirects=True,
            transport=transport,
            headers={
                "accept": "application/json",
                "user-agent": "duels-mcp/1.0",
            },
        )

    # -----------------------------------------------------------------
    # Auth
    # -----------------------------------------------------------------
    @staticmethod
    def _build_cookie_header(raw: str) -> str:
        """Turn whatever the user pasted into a usable Cookie header value.

        Accepts a full cookie string (name=value, or several separated by
        semicolons) or a bare token value, in which case both known better-auth
        cookie names are sent - the server ignores the one it does not know.
        """
        if not raw:
            return ""
        if "=" in raw:
            return raw
        return "; ".join(f"{name}={raw}" for name in COOKIE_NAMES)

    @property
    def authenticated(self) -> bool:
        """True when a session cookie is configured (not that it is still valid)."""
        return bool(self._cookie_header)

    def require_auth(self, what: str) -> None:
        """Fail fast with a helpful message instead of spending a doomed request."""
        if not self._cookie_header:
            raise DuelsError(f"{what} requires a signed-in Duels.ink account. {AUTH_HELP}")

    # -----------------------------------------------------------------
    # Transport
    # -----------------------------------------------------------------
    async def request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[dict] = None,
        json_body: Optional[dict] = None,
        authed: bool = True,
    ) -> Any:
        """Perform one API call and return the decoded JSON body.

        Args:
            method: HTTP verb.
            path: Path starting with /api/.
            params: Query string parameters.
            json_body: JSON request body.
            authed: Send the session cookie when one is configured.

        Raises:
            DuelsError: Always phrased as something the agent can act on.
        """
        headers: dict[str, str] = {}
        if authed and self._cookie_header:
            headers["cookie"] = self._cookie_header
        if json_body is not None:
            headers["content-type"] = "application/json"

        try:
            resp = await self._client.request(
                method,
                path,
                params=params,
                json=json_body,
                headers=headers or None,
            )
        except httpx.TimeoutException as exc:
            raise DuelsError(
                f"Duels.ink timed out after {REQUEST_TIMEOUT:.0f}s on {method} {path}. "
                "The site may be busy - try again."
            ) from exc
        except httpx.RequestError as exc:
            raise DuelsError(
                f"Could not reach Duels.ink ({type(exc).__name__}) on {method} {path}. "
                "Check network connectivity."
            ) from exc

        if resp.status_code >= 400:
            raise self._http_error(resp, method, path)

        if not resp.content:
            return {}
        try:
            return resp.json()
        except ValueError as exc:
            # An HTML body on an /api/ path means the route does not exist: the
            # SPA fallback served index.html instead.
            raise DuelsError(
                f"Duels.ink returned a non-JSON response for {method} {path}. This usually "
                "means the endpoint no longer exists - the site's private API may have "
                "changed. Run duels_whoami to see the current build id."
            ) from exc

    def _http_error(self, resp: httpx.Response, method: str, path: str) -> DuelsError:
        """Map an HTTP status onto a message that says what to do next."""
        status = resp.status_code
        detail = ""
        try:
            body = resp.json()
            if isinstance(body, dict):
                detail = str(body.get("error") or body.get("message") or "")
        except ValueError:
            detail = ""

        suffix = f" ({detail})" if detail else ""

        if status == 401:
            return DuelsError(f"Not authenticated for {path}{suffix}. {AUTH_HELP}")
        if status == 403:
            return DuelsError(
                f"Access denied for {path}{suffix}. "
                "Your account does not have permission for this resource."
            )
        if status == 404:
            return DuelsError(
                f"Not found: {path}{suffix}. Check the id is correct and still exists."
            )
        if status == 400:
            return DuelsError(
                f"Duels.ink rejected the request to {path}: {detail or 'bad request'}. "
                "Check the arguments against the tool's documented schema."
            )
        if status == 429:
            return DuelsError("Rate limited by Duels.ink. Wait a few seconds before retrying.")
        if status >= 500:
            return DuelsError(
                f"Duels.ink server error ({status}) on {method} {path}. "
                "This is on their side - retry shortly."
            )
        return DuelsError(f"Duels.ink returned HTTP {status} for {method} {path}{suffix}.")

    # -----------------------------------------------------------------
    # Convenience wrappers
    # -----------------------------------------------------------------
    async def get(self, path: str, params: Optional[dict] = None, authed: bool = True) -> Any:
        return await self.request("GET", path, params=params, authed=authed)

    async def post(self, path: str, json_body: Optional[dict] = None, authed: bool = True) -> Any:
        return await self.request("POST", path, json_body=json_body or {}, authed=authed)

    async def put(self, path: str, json_body: Optional[dict] = None, authed: bool = True) -> Any:
        return await self.request("PUT", path, json_body=json_body or {}, authed=authed)

    async def delete(self, path: str, authed: bool = True) -> Any:
        return await self.request("DELETE", path, authed=authed)

    # -----------------------------------------------------------------
    # Session / diagnostics
    # -----------------------------------------------------------------
    async def get_session(self) -> Optional[dict]:
        """Return the better-auth session payload, or None when anonymous."""
        data = await self.get("/api/auth/get-session")
        if not isinstance(data, dict) or not data.get("user"):
            return None
        return data

    async def build_id(self) -> Optional[str]:
        """Current site build id - used to detect that the private API drifted."""
        try:
            data = await self.get("/api/version", authed=False)
        except DuelsError:
            return None
        return data.get("buildId") if isinstance(data, dict) else None

    async def ws_token(
        self,
        kind: str,
        resource_id: str,
        session_id: Optional[str] = None,
    ) -> str:
        """Fetch a short-lived WebSocket URL for a game or table.

        The WebSocket host is sharded and varies per resource (ws, ws3 and ws4
        have all been observed), so the base URL returned by the API is always
        used rather than hardcoded.

        Args:
            kind: "game" or "table".
            resource_id: The game or table UUID.
            session_id: Anonymous session id, as returned by
                create-bot-game for players without an account. It is the only
                credential an anonymous game has - without it this endpoint
                answers 401.

        Returns:
            The fully composed wss:// URL including the token.
        """
        if kind not in ("game", "table"):
            raise DuelsError(f"ws_token kind must be 'game' or 'table', got {kind!r}.")
        params = {"sessionId": session_id} if session_id else None
        data = await self.get(f"/api/{kind}/{resource_id}/ws-token", params=params)
        token = data.get("token") if isinstance(data, dict) else None
        base = (data.get("wsUrl") or "").rstrip("/") if isinstance(data, dict) else ""
        if not token or not base:
            raise DuelsError(
                f"Duels.ink did not return a usable WebSocket token for {kind} {resource_id}."
            )
        return f"{base}/{kind}/{resource_id}?token={token}"

    async def aclose(self) -> None:
        await self._client.aclose()
