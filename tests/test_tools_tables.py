"""Private tables: the only way into Coconut and into a game with 3 opponents.

Everything here was found by opening a real table, because almost none of it
is guessable and two of the calls answer 200 while doing nothing at all.
"""

import httpx
import pytest

from tests.conftest import call_json, call_text
from tests.fixtures import decks as deck_fixtures

pytestmark = pytest.mark.anyio

TABLE_ID = "01a0c1f2-2294-7687-b2ce-09e243231b16"
DECK_ID = deck_fixtures.DECK_ID
COCONUT = "coconut-011"


def view(**overrides) -> dict:
    """A table view in the envelope the API actually uses."""
    inner = {
        "id": TABLE_ID,
        "status": "assembling",
        "isHost": False,
        "mySeatIndex": 2,
        "config": {
            "gameFormat": "Coconut",
            "maxSeats": 4,
            "openSeats": 4,
            "visibility": "public",
            "timerPreset": "none",
        },
        "seats": [
            {"index": 0, "username": "Hazack", "ready": True, "hasDeck": True,
             "hasCoconut": True, "connected": False},
            {"index": 1, "username": "LorcanaLover", "ready": False, "hasDeck": False,
             "hasCoconut": False, "connected": True},
            {"index": 2, "username": "Fransozo", "ready": False, "hasDeck": True,
             "hasCoconut": True, "coconutCardId": COCONUT, "connected": True,
             "deckId": DECK_ID, "deckCardIds": ["10-71"] * 60},
        ],
        "spectators": [{"username": "Rayane"}],
        "log": [{"ts": 1, "type": "seat_joined", "name": "Fransozo"}],
    }
    inner.update(overrides)
    return {"view": inner}


def coconut_deck() -> dict:
    return {
        "deck": {
            **deck_fixtures.detail()["deck"],
            "coconutCardId": COCONUT,
            "legalFormats": ["Coconut"],
        }
    }


def routes(router, *, table=None, action=None):
    router.on(
        f"/api/table/{TABLE_ID}/view",
        lambda _r: httpx.Response(200, json=table if table is not None else view()),
    )
    router.on(
        f"/api/table/{TABLE_ID}/action",
        lambda _r: httpx.Response(200, json=action if action is not None else {"success": True}),
    )
    router.on(f"/api/decks/{DECK_ID}", lambda _r: httpx.Response(200, json=coconut_deck()))


def sent(router):
    return next(c for c in router.calls if c.url.path == f"/api/table/{TABLE_ID}/action").content


class TestTheViewEnvelope:
    """`GET /view` answers `{"view": {...}}`, one level down.

    Reading the outer dict gives a status of None and a table that looks empty
    rather than unreachable. That is what this did until a real table was
    opened and every seat came back blank.
    """

    async def test_the_table_is_unwrapped(self, router, mcp_client):
        routes(router)
        data = await call_json(mcp_client, "duels_get_table", table_id=TABLE_ID)
        assert data["status"] == "assembling"
        assert data["format"] == "Coconut"
        assert len(data["seats"]) == 3

    async def test_a_flat_view_still_reads(self, router, mcp_client):
        """Not every caller gets the envelope, and guessing wrong either way
        loses the table."""
        routes(router, table={"status": "playing", "gameId": "g-1", "seats": []})
        data = await call_json(mcp_client, "duels_get_table", table_id=TABLE_ID)
        assert data["status"] == "playing" and data["game_id"] == "g-1"

    async def test_an_action_ack_is_not_mistaken_for_a_table(
        self, router, authed_mcp_client
    ):
        """An action answers `{"success": true}` with no view. Treating that as
        the table drops the game id that only the real view carries."""
        routes(
            router,
            table=view(status="playing", gameId="g-9"),
            action={"success": True},
        )
        text = await call_text(
            authed_mcp_client, "duels_configure_table", table_id=TABLE_ID, action="start"
        )
        assert "g-9" in text and "Game started" in text


class TestSeatsAreReadable:
    async def test_seats_render_as_a_table_not_raw_json(self, router, mcp_client):
        """The raw view carries the join history and every seat's sixty card
        ids - thousands of tokens of nothing."""
        routes(router)
        text = await call_text(mcp_client, "duels_get_table", table_id=TABLE_ID)
        assert "| Seat | Player |" in text
        assert "Hazack" in text and "LorcanaLover" in text
        assert "seat_joined" not in text, "the lobby log should not be dumped"
        assert text.count("10-71") == 0, "deck contents should not be dumped"

    async def test_your_own_seat_is_marked(self, router, mcp_client):
        routes(router)
        text = await call_text(mcp_client, "duels_get_table", table_id=TABLE_ID)
        assert "**Fransozo** (you)" in text

    async def test_who_is_missing_a_deck_is_visible(self, router, mcp_client):
        """It is the usual reason a table will not start."""
        routes(router)
        data = await call_json(mcp_client, "duels_get_table", table_id=TABLE_ID)
        by_name = {s["player"]: s for s in data["seats"]}
        assert by_name["LorcanaLover"]["has_deck"] is False
        assert by_name["Hazack"]["ready"] is True

    async def test_the_coconut_on_a_seat_is_named(self, router, mcp_client):
        routes(router)
        text = await call_text(mcp_client, "duels_get_table", table_id=TABLE_ID)
        assert COCONUT in text

    async def test_free_seats_are_counted(self, router, mcp_client):
        routes(router)
        text = await call_text(mcp_client, "duels_get_table", table_id=TABLE_ID)
        assert "3/4" in text and "1 seat(s) still open" in text


class TestReadyCarriesItsValue:
    """SET_READY takes a boolean and there is no SET_UNREADY.

    Sending the bare type is answered 200 and changes nothing: the seat stays
    unready while the call reports success. Found by readying up through the
    API, watching the seat refuse to change, and then reading what the site's
    own button sends.
    """

    async def test_ready_sends_true(self, router, authed_mcp_client):
        routes(router)
        await call_text(
            authed_mcp_client, "duels_configure_table", table_id=TABLE_ID, action="ready"
        )
        assert b'"ready": true' in sent(router).replace(b'"ready":true', b'"ready": true')

    async def test_unready_sends_false_not_a_different_action(
        self, router, authed_mcp_client
    ):
        routes(router)
        await call_text(
            authed_mcp_client, "duels_configure_table", table_id=TABLE_ID, action="unready"
        )
        body = sent(router)
        assert b"SET_UNREADY" not in body, "that action does not exist"
        assert b"SET_READY" in body
        assert b'"ready": false' in body.replace(b'"ready":false', b'"ready": false')


class TestSeatingADeck:
    async def test_the_coconut_travels_with_the_deck(self, router, authed_mcp_client):
        """The seat does not infer it from the deck id: sending only the cards
        seats a Coconut deck without its Coconut, and the seat then reports
        hasCoconut false while still looking seated and ready."""
        routes(router)
        await call_text(
            authed_mcp_client,
            "duels_configure_table",
            table_id=TABLE_ID,
            action="set_deck",
            deck_id=DECK_ID,
        )
        body = sent(router)
        assert b"SET_DECK" in body and b"deckCardIds" in body
        assert COCONUT.encode() in body

    async def test_a_deck_without_one_sends_no_coconut_field(
        self, router, authed_mcp_client
    ):
        routes(router)
        router.on(
            f"/api/decks/{DECK_ID}", lambda _r: httpx.Response(200, json=deck_fixtures.detail())
        )
        await call_text(
            authed_mcp_client,
            "duels_configure_table",
            table_id=TABLE_ID,
            action="set_deck",
            deck_id=DECK_ID,
        )
        assert b"coconutCardId" not in sent(router)


class TestTableSettings:
    async def test_the_format_is_a_table_setting(self, router, authed_mcp_client):
        """A three-ink Coconut deck is refused at a Core table, so the format
        has to be set before anyone sits down."""
        routes(router)
        await call_text(
            authed_mcp_client,
            "duels_configure_table",
            table_id=TABLE_ID,
            action="set_format",
            game_format="Coconut",
        )
        body = sent(router)
        assert b"UPDATE_SETTINGS" in body and b"Coconut" in body

    async def test_set_format_without_one_lists_the_formats(self, authed_mcp_client):
        text = await call_text(
            authed_mcp_client,
            "duels_configure_table",
            table_id=TABLE_ID,
            action="set_format",
        )
        assert text.startswith("Error:")
        for fmt in ("Core", "Infinity", "Coconut"):
            assert fmt in text

    async def test_opening_seats_sets_only_the_one_that_works(
        self, router, authed_mcp_client
    ):
        """`maxSeats` is fixed at 4.

        Every value from 1 to 8 is answered 200 and none of them is stored, so
        sending it alongside `openSeats` only made the call look like it did
        more than it did. `openSeats` is the whole mechanism.
        """
        routes(router)
        await call_text(
            authed_mcp_client,
            "duels_configure_table",
            table_id=TABLE_ID,
            action="set_seats",
            seats=4,
        )
        body = sent(router)
        assert b"openSeats" in body and b"maxSeats" not in body

    async def test_going_public_is_a_setting(self, router, authed_mcp_client):
        routes(router)
        await call_text(
            authed_mcp_client,
            "duels_configure_table",
            table_id=TABLE_ID,
            action="make_public",
        )
        assert b"public" in sent(router)

    async def test_kicking_a_seat_needs_its_number(self, authed_mcp_client):
        """This is also the only way to remove a bot, which a Coconut table
        must do - the server refuses the format while one is seated."""
        text = await call_text(
            authed_mcp_client,
            "duels_configure_table",
            table_id=TABLE_ID,
            action="kick_seat",
        )
        assert text.startswith("Error:") and "seat_index" in text

    async def test_kicking_sends_the_index(self, router, authed_mcp_client):
        routes(router)
        await call_text(
            authed_mcp_client,
            "duels_configure_table",
            table_id=TABLE_ID,
            action="kick_seat",
            seat_index=1,
        )
        body = sent(router)
        assert b"KICK_SEAT" in body and b"seatIndex" in body

    async def test_the_new_actions_are_all_listed_when_one_is_wrong(
        self, authed_mcp_client
    ):
        text = await call_text(
            authed_mcp_client, "duels_configure_table", table_id=TABLE_ID, action="explode"
        )
        for action in ("set_format", "set_seats", "kick_seat", "make_public"):
            assert action in text


class TestMatchFormatAndUndo:
    """Two settings the panel offers that the wire spells its own way."""

    async def test_best_of_three_is_bo3(self, router, authed_mcp_client):
        routes(router)
        await call_text(
            authed_mcp_client,
            "duels_configure_table",
            table_id=TABLE_ID,
            action="set_match_format",
            match_format="bo3",
        )
        body = sent(router)
        assert b"matchFormat" in body and b"bo3" in body

    async def test_an_unset_match_format_reads_as_best_of_one(
        self, router, authed_mcp_client
    ):
        """The server stores nothing for the default, so absent means bo1."""
        routes(router)
        payload = await call_json(
            authed_mcp_client, "duels_get_table", table_id=TABLE_ID
        )
        assert payload["match_format"] == "bo1"

    async def test_a_bad_match_format_lists_the_two(self, authed_mcp_client):
        text = await call_text(
            authed_mcp_client,
            "duels_configure_table",
            table_id=TABLE_ID,
            action="set_match_format",
            match_format="bo5",
        )
        assert text.startswith("Error:") and "bo1" in text and "bo3" in text

    async def test_undo_sends_only_the_mode(self, router, authed_mcp_client):
        """Sending the seconds alongside is refused as 'Invalid undo settings';
        the server fills in undoTimeCostSeconds itself."""
        routes(router)
        await call_text(
            authed_mcp_client,
            "duels_configure_table",
            table_id=TABLE_ID,
            action="set_undo",
            undo_mode="timed",
        )
        body = sent(router)
        assert b"privateUndoConfig" in body and b"timed" in body
        assert b"undoTimeCostSeconds" not in body and b"seconds" not in body

    async def test_a_bad_undo_mode_lists_the_three(self, authed_mcp_client):
        text = await call_text(
            authed_mcp_client,
            "duels_configure_table",
            table_id=TABLE_ID,
            action="set_undo",
            undo_mode="sometimes",
        )
        assert text.startswith("Error:")
        for mode in ("unlimited", "timed", "disabled"):
            assert mode in text

    async def test_the_undo_mode_is_read_back(self, router, authed_mcp_client):
        table = view()
        table["view"]["config"]["privateUndoConfig"] = {"mode": "disabled"}
        routes(router, table=table)
        payload = await call_json(
            authed_mcp_client, "duels_get_table", table_id=TABLE_ID
        )
        assert payload["undo_mode"] == "disabled"

    async def test_open_hand_to_spectators_is_read_back(self, router, authed_mcp_client):
        table = view()
        table["view"]["config"]["revealHandsToSpectators"] = True
        routes(router, table=table)
        payload = await call_json(
            authed_mcp_client, "duels_get_table", table_id=TABLE_ID
        )
        assert payload["reveal_hands_to_spectators"] is True

    async def test_no_limit_is_not_offered(self, authed_mcp_client):
        """The panel lists it; every spelling of it is 'Invalid format'."""
        text = await call_text(
            authed_mcp_client,
            "duels_configure_table",
            table_id=TABLE_ID,
            action="set_format",
            game_format="NoLimit",
        )
        assert text.startswith("Error:")


class TestLeavingAndAbandonment:
    """A seat you cannot leave, at a table you cannot tell is dead."""

    async def test_leaving_sends_leave_table(self, router, authed_mcp_client):
        routes(router)
        await call_text(
            authed_mcp_client,
            "duels_configure_table",
            table_id=TABLE_ID,
            action="leave",
        )
        assert b"LEAVE_TABLE" in sent(router)

    async def test_leave_is_offered_by_name_when_the_action_is_wrong(
        self, authed_mcp_client
    ):
        text = await call_text(
            authed_mcp_client,
            "duels_configure_table",
            table_id=TABLE_ID,
            action="scarper",
        )
        assert text.startswith("Error:") and "leave" in text

    async def test_a_seat_says_whether_anyone_is_in_it(self, router, authed_mcp_client):
        """A seat keeps its name, deck and ready flag after its player leaves."""
        routes(router)
        text = await call_text(authed_mcp_client, "duels_get_table", table_id=TABLE_ID)
        assert "| Here |" in text or "Here" in text
        assert "**gone**" in text

    async def test_an_abandoned_table_says_so(self, router, authed_mcp_client):
        """Four seated, ready players and not one of them still connected."""
        dead = view()
        for seat in dead["view"]["seats"]:
            seat["connected"] = False
        routes(router, table=dead)
        text = await call_text(authed_mcp_client, "duels_get_table", table_id=TABLE_ID)
        assert "abandoned" in text

    async def test_a_live_table_does_not_cry_abandonment(self, router, authed_mcp_client):
        live = view()
        for seat in live["view"]["seats"]:
            seat["connected"] = True
        routes(router, table=live)
        text = await call_text(authed_mcp_client, "duels_get_table", table_id=TABLE_ID)
        assert "abandoned" not in text


class TestFindingATable:
    HOME = {
        "openTables": [
            {"id": "t-coconut-1", "hostName": "Hazack", "format": "Coconut",
             "seatsFilled": 3, "maxSeats": 4, "timerPreset": "none"},
            {"id": "t-coconut-full", "hostName": "Jourdon", "format": "Coconut",
             "seatsFilled": 4, "maxSeats": 4, "timerPreset": "standard"},
            {"id": "t-core-1", "hostName": "Azawhak", "format": "Core",
             "seatsFilled": 1, "maxSeats": 2, "timerPreset": "none"},
        ]
    }

    async def test_open_tables_are_listed(self, router, mcp_client):
        router.json_on("/api/home/data", self.HOME)
        data = await call_json(mcp_client, "duels_list_open_tables")
        assert {t["host"] for t in data["tables"]} == {"Hazack", "Azawhak"}

    async def test_full_tables_are_hidden_by_default(self, router, mcp_client):
        """A table with no seat is not somewhere you can go."""
        router.json_on("/api/home/data", self.HOME)
        data = await call_json(mcp_client, "duels_list_open_tables")
        assert all(t["seats_open"] > 0 for t in data["tables"])

    async def test_full_tables_can_be_asked_for(self, router, mcp_client):
        router.json_on("/api/home/data", self.HOME)
        data = await call_json(mcp_client, "duels_list_open_tables", with_space=False)
        assert data["count"] == 3

    async def test_filtering_by_format(self, router, mcp_client):
        """Coconut has no queue and no practice mode, so finding a table is
        the only way in."""
        router.json_on("/api/home/data", self.HOME)
        data = await call_json(mcp_client, "duels_list_open_tables", game_format="coconut")
        assert [t["host"] for t in data["tables"]] == ["Hazack"]

    async def test_nothing_open_says_how_to_start_one(self, router, mcp_client):
        router.json_on("/api/home/data", {"openTables": []})
        text = await call_text(mcp_client, "duels_list_open_tables", game_format="Coconut")
        assert "No open Coconut tables" in text
        assert "make_public" in text


class TestJoiningATable:
    async def test_joining_needs_a_cookie(self, mcp_client):
        text = await call_text(mcp_client, "duels_join_table", table_id=TABLE_ID)
        assert text.startswith("Error:")

    async def test_a_bare_join_takes_the_seat(self, router, authed_mcp_client):
        routes(router)
        await call_text(authed_mcp_client, "duels_join_table", table_id=TABLE_ID)
        assert b"JOIN_TABLE" in sent(router)

    async def test_joining_with_a_deck_seats_it_and_its_coconut(
        self, router, authed_mcp_client
    ):
        routes(router)
        await call_text(
            authed_mcp_client, "duels_join_table", table_id=TABLE_ID, deck_id=DECK_ID
        )
        bodies = [
            c.content
            for c in router.calls
            if c.url.path == f"/api/table/{TABLE_ID}/action"
        ]
        assert b"JOIN_TABLE" in bodies[0]
        assert b"SET_DECK" in bodies[1] and COCONUT.encode() in bodies[1]

    async def test_the_seat_number_comes_back(self, router, authed_mcp_client):
        """Knowing which seat you took is how the board is read later."""
        routes(router)
        data = await call_json(authed_mcp_client, "duels_join_table", table_id=TABLE_ID)
        assert data["my_seat"] == 2
