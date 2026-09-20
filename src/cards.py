"""Lorcana card catalog backed by GET /api/cards.

The catalog is public (no authentication) and paginated at 100 cards per page,
with roughly 3200 cards in total. The catalog `id` is the same string the game
engine uses as `definitionId` (for example "10-71"), which is what lets a raw
game state be turned into readable card names.

Loading all 32 pages is slow, so cards are cached **per set**: an id like
"10-71" only needs set 10 (about 3 pages). Ids that do not follow the
`<set>-<number>` shape (a handful of promos) fall back to a full load.
"""

import asyncio
import logging
import time
from typing import Any, Iterable, Optional

from .client import DuelsClient, DuelsError

log = logging.getLogger("duels_mcp.cards")

PAGE_SIZE = 100
CACHE_TTL_SECONDS = 24 * 60 * 60

# Fields kept when summarising a card for an agent. The full record carries
# market prices, image variants and thumbhashes that would only burn context.
SUMMARY_FIELDS = (
    "id",
    "fullName",
    "type",
    "cost",
    "inkable",
    "colors",
    "strength",
    "willpower",
    "lore",
    "moveCost",
    "rarity",
    "subtypes",
    "rulesText",
    # Keyword and named abilities arrive already structured - abilities as
    # [{"ability": "Resist", "value": 1}] and specialAbilities as
    # [{"name": "LOW BATTERIES", "effect": "..."}] - so the renderer can show
    # them without parsing rulesText.
    "abilities",
    "specialAbilities",
)


def summarise(card: dict) -> dict:
    """Reduce a full card record to the fields that matter for play and search."""
    out = {k: card.get(k) for k in SUMMARY_FIELDS if card.get(k) not in (None, "", [])}
    out["id"] = card.get("id")
    out["fullName"] = card.get("fullName") or card.get("name")
    return out


def parse_set_number(definition_id: str) -> Optional[int]:
    """Return the set number encoded in a definitionId, or None if unusual."""
    head = str(definition_id).split("-", 1)[0]
    return int(head) if head.isdigit() else None


class CardCatalog:
    """Lazily-populated, TTL-expiring index of the Lorcana card catalog."""

    def __init__(self, client: DuelsClient) -> None:
        self._client = client
        self._by_id: dict[str, dict] = {}
        self._loaded_sets: dict[int, float] = {}
        self._all_loaded_at: Optional[float] = None
        self._total: Optional[int] = None
        self._lock = asyncio.Lock()

    # -----------------------------------------------------------------
    # Loading
    # -----------------------------------------------------------------
    @staticmethod
    def _fresh(stamp: Optional[float]) -> bool:
        return stamp is not None and (time.time() - stamp) < CACHE_TTL_SECONDS

    async def _fetch_page(self, offset: int, set_number: Optional[int] = None) -> dict:
        params: dict[str, Any] = {"limit": PAGE_SIZE, "offset": offset}
        if set_number is not None:
            params["set"] = set_number
        data = await self._client.get("/api/cards", params=params, authed=False)
        if not isinstance(data, dict) or "cards" not in data:
            raise DuelsError(
                "Unexpected response from the Duels.ink card catalog. "
                "The site's private API may have changed."
            )
        return data

    def _index(self, cards: Iterable[dict]) -> None:
        for card in cards:
            cid = card.get("id")
            if cid:
                self._by_id[str(cid)] = card

    async def _load_pages(self, set_number: Optional[int]) -> None:
        """Page through the catalog (optionally one set) and index everything."""
        offset = 0
        while True:
            page = await self._fetch_page(offset, set_number)
            cards = page.get("cards") or []
            self._index(cards)
            meta = page.get("meta") or {}
            if set_number is None:
                self._total = meta.get("total", self._total)
            if not meta.get("hasMore") or not cards:
                break
            offset += len(cards)

    async def ensure_set(self, set_number: int) -> None:
        """Make sure every card of one set is cached."""
        if self._fresh(self._loaded_sets.get(set_number)) or self._fresh(self._all_loaded_at):
            return
        async with self._lock:
            if self._fresh(self._loaded_sets.get(set_number)) or self._fresh(self._all_loaded_at):
                return
            await self._load_pages(set_number)
            self._loaded_sets[set_number] = time.time()

    async def ensure_all(self) -> None:
        """Make sure the entire catalog is cached (about 32 requests)."""
        if self._fresh(self._all_loaded_at):
            return
        async with self._lock:
            if self._fresh(self._all_loaded_at):
                return
            log.info("Loading the full Duels.ink card catalog")
            await self._load_pages(None)
            self._all_loaded_at = time.time()
            self._loaded_sets.clear()

    # -----------------------------------------------------------------
    # Lookup
    # -----------------------------------------------------------------
    async def get(self, definition_id: str) -> Optional[dict]:
        """Return the full record for one definitionId, or None if unknown."""
        definition_id = str(definition_id)
        if definition_id in self._by_id:
            return self._by_id[definition_id]

        set_number = parse_set_number(definition_id)
        if set_number is not None:
            await self.ensure_set(set_number)
            if definition_id in self._by_id:
                return self._by_id[definition_id]

        await self.ensure_all()
        return self._by_id.get(definition_id)

    async def resolve_many(self, definition_ids: Iterable[str]) -> dict[str, Optional[dict]]:
        """Resolve several definitionIds, loading only the sets actually needed."""
        wanted = [str(i) for i in definition_ids]
        missing = [i for i in wanted if i not in self._by_id]

        sets_needed = {parse_set_number(i) for i in missing}
        if None in sets_needed:
            await self.ensure_all()
        else:
            for set_number in sorted(s for s in sets_needed if s is not None):
                await self.ensure_set(set_number)

        return {i: self._by_id.get(i) for i in wanted}

    async def name_of(self, definition_id: str) -> str:
        """Best-effort display name; falls back to the raw id when unknown."""
        card = await self.get(definition_id)
        if not card:
            return str(definition_id)
        return card.get("fullName") or card.get("name") or str(definition_id)

    # -----------------------------------------------------------------
    # Search
    # -----------------------------------------------------------------
    async def search(
        self,
        query: Optional[str] = None,
        set_number: Optional[int] = None,
        card_type: Optional[str] = None,
        color: Optional[str] = None,
        cost: Optional[int] = None,
        inkable: Optional[bool] = None,
        rarity: Optional[str] = None,
        legality: Optional[str] = None,
        limit: int = 25,
        offset: int = 0,
    ) -> tuple[list[dict], int]:
        """Search the catalog.

        `query` and `set_number` are pushed to the API (it filters server-side);
        every other filter is applied locally to that result. Returns the page
        of cards plus the total number of matches.
        """
        extra_filters = any(
            f is not None for f in (card_type, color, cost, inkable, rarity, legality)
        )

        # Without local filters the API can paginate for us.
        if not extra_filters:
            params: dict[str, Any] = {"limit": limit, "offset": offset}
            if query:
                params["q"] = query
            if set_number is not None:
                params["set"] = set_number
            page = await self._fetch_page_raw(params)
            cards = page.get("cards") or []
            self._index(cards)
            total = (page.get("meta") or {}).get("total", len(cards))
            return cards, total

        # With local filters we need the whole candidate pool first.
        pool = await self._collect(query, set_number)
        matched = [
            c
            for c in pool
            if self._matches(c, card_type, color, cost, inkable, rarity, legality)
        ]
        return matched[offset : offset + limit], len(matched)

    async def _fetch_page_raw(self, params: dict) -> dict:
        data = await self._client.get("/api/cards", params=params, authed=False)
        if not isinstance(data, dict) or "cards" not in data:
            raise DuelsError(
                "Unexpected response from the Duels.ink card catalog. "
                "The site's private API may have changed."
            )
        return data

    async def _collect(self, query: Optional[str], set_number: Optional[int]) -> list[dict]:
        """Pull every card matching the server-side filters."""
        collected: list[dict] = []
        offset = 0
        while True:
            params: dict[str, Any] = {"limit": PAGE_SIZE, "offset": offset}
            if query:
                params["q"] = query
            if set_number is not None:
                params["set"] = set_number
            page = await self._fetch_page_raw(params)
            cards = page.get("cards") or []
            self._index(cards)
            collected.extend(cards)
            meta = page.get("meta") or {}
            if not meta.get("hasMore") or not cards:
                break
            offset += len(cards)
        return collected

    @staticmethod
    def _matches(
        card: dict,
        card_type: Optional[str],
        color: Optional[str],
        cost: Optional[int],
        inkable: Optional[bool],
        rarity: Optional[str],
        legality: Optional[str],
    ) -> bool:
        if card_type and str(card.get("type", "")).lower() != card_type.lower():
            return False
        if color:
            colors = [str(c).lower() for c in (card.get("colors") or [])]
            if color.lower() not in colors:
                return False
        if cost is not None and card.get("cost") != cost:
            return False
        if inkable is not None and bool(card.get("inkable")) is not inkable:
            return False
        if rarity and str(card.get("rarity", "")).lower() != rarity.lower():
            return False
        if legality:
            legal = [str(l).lower() for l in (card.get("legality") or [])]
            if legality.lower() not in legal:
                return False
        return True
