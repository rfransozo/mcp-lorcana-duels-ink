"""The account-shaped tools: who you are, who you know, what you have played.

These all sit on one REST call or two and spend most of their code reshaping
the answer, so that reshaping is what is tested: which field wins when
Duels.ink reports a name three different ways, what an empty list should say
instead of printing nothing, and what survives when half the account endpoints
are down.
"""

import httpx
import pytest

from tests.conftest import call_json, call_text

pytestmark = pytest.mark.anyio


FRIENDS = {
    "friends": [
        {"id": "u-1", "name": "Ariel"},
        {"userId": "u-2", "userName": "Ursula"},
    ],
    "incoming": [{"from": "u-9", "name": "Flounder"}],
    "outgoing": [],
}
PRESENCE = {"presence": {"u-1": True, "u-2": False}, "lastSeen": {"u-2": "2026-09-19T10:00:00Z"}}


class TestFriends:
    async def test_needs_a_cookie(self, mcp_client):
        text = await call_text(mcp_client, "duels_list_friends")
        assert text.startswith("Error:") and "Listing friends" in text

    async def test_presence_is_merged_onto_the_roster(self, router, authed_mcp_client):
        """The roster and the presence map are two calls; neither is useful
        alone."""
        router.json_on("/api/friends", FRIENDS)
        router.json_on("/api/friends/presence", PRESENCE)
        data = await call_json(authed_mcp_client, "duels_list_friends")
        by_id = {f["id"]: f for f in data["friends"]}
        assert by_id["u-1"]["online"] is True
        assert by_id["u-2"]["online"] is False
        assert by_id["u-2"]["last_seen"] == "2026-09-19T10:00:00Z"

    async def test_either_spelling_of_id_and_name_is_accepted(self, router, authed_mcp_client):
        """Duels.ink reports a friend as id/name or userId/userName depending
        on the endpoint, and dropping one would silently lose half the list."""
        router.json_on("/api/friends", FRIENDS)
        router.json_on("/api/friends/presence", PRESENCE)
        data = await call_json(authed_mcp_client, "duels_list_friends")
        assert {f["name"] for f in data["friends"]} == {"Ariel", "Ursula"}

    async def test_online_only_filters(self, router, authed_mcp_client):
        router.json_on("/api/friends", FRIENDS)
        router.json_on("/api/friends/presence", PRESENCE)
        data = await call_json(authed_mcp_client, "duels_list_friends", online_only=True)
        assert [f["name"] for f in data["friends"]] == ["Ariel"]

    async def test_no_friends_at_all_suggests_adding_one(self, router, authed_mcp_client):
        router.json_on("/api/friends", {"friends": []})
        router.json_on("/api/friends/presence", {})
        text = await call_text(authed_mcp_client, "duels_list_friends")
        assert "duels_send_friend_request" in text

    async def test_nobody_online_does_not_suggest_adding_one(self, router, authed_mcp_client):
        """An empty filtered view is not an empty friends list, and saying so
        would be wrong."""
        router.json_on("/api/friends", FRIENDS)
        router.json_on("/api/friends/presence", {"presence": {}})
        text = await call_text(authed_mcp_client, "duels_list_friends", online_only=True)
        assert "online right now" in text
        assert "duels_send_friend_request" not in text

    async def test_requests_are_surfaced(self, router, authed_mcp_client):
        router.json_on("/api/friends", FRIENDS)
        router.json_on("/api/friends/presence", PRESENCE)
        text = await call_text(authed_mcp_client, "duels_list_friends")
        assert "Incoming requests (1)" in text and "Flounder" in text

    async def test_sending_a_request_confirms_the_target(self, router, authed_mcp_client):
        router.json_on("/api/friends/request", {"success": True})
        text = await call_text(authed_mcp_client, "duels_send_friend_request", user_id="u-7")
        assert "u-7" in text
        assert router.calls[-1].url.path == "/api/friends/request"


class TestInvites:
    async def test_nothing_waiting_says_so(self, router, authed_mcp_client):
        router.json_on("/api/me/pending-invites", {"invites": [], "outgoing": []})
        text = await call_text(authed_mcp_client, "duels_list_pending_invites")
        assert "pending invites" in text

    async def test_incoming_and_outgoing_are_separated(self, router, authed_mcp_client):
        router.json_on(
            "/api/me/pending-invites",
            {"invites": [{"tableId": "t-1"}], "outgoing": [{"tableId": "t-2"}]},
        )
        text = await call_text(authed_mcp_client, "duels_list_pending_invites")
        assert "Incoming (1)" in text and "Outgoing (1)" in text
        assert "t-1" in text and "t-2" in text

    async def test_needs_a_cookie(self, mcp_client):
        text = await call_text(mcp_client, "duels_list_pending_invites")
        assert text.startswith("Error:")


class TestMatchHistory:
    async def test_needs_a_cookie(self, mcp_client):
        text = await call_text(mcp_client, "duels_get_match_history")
        assert text.startswith("Error:")

    async def test_no_matches_points_at_how_to_get_one(self, router, authed_mcp_client):
        router.json_on("/api/me/match-history", {"games": []})
        text = await call_text(authed_mcp_client, "duels_get_match_history")
        assert "Play a ranked or table game first" in text

    async def test_result_and_opponent_fall_back_across_spellings(
        self, router, authed_mcp_client
    ):
        router.json_on(
            "/api/me/match-history",
            {
                "games": [
                    {"id": "g-1", "result": "win", "opponentName": "Ariel", "finishedAt": "d1"},
                    {"gameId": "g-2", "outcome": "loss", "opponent": "Ursula", "createdAt": "d2"},
                ]
            },
        )
        text = await call_text(authed_mcp_client, "duels_get_match_history")
        assert "**win** vs Ariel" in text and "**loss** vs Ursula" in text
        assert "g-1" in text and "g-2" in text

    async def test_the_cursor_is_handed_back_ready_to_use(self, router, authed_mcp_client):
        """Pagination is useless if the caller has to guess the argument."""
        router.json_on(
            "/api/me/match-history", {"games": [{"id": "g-1"}], "next_cursor": "abc|def"}
        )
        text = await call_text(authed_mcp_client, "duels_get_match_history")
        assert "cursor='abc|def'" in text

    async def test_a_cursor_is_forwarded_to_the_api(self, router, authed_mcp_client):
        router.json_on("/api/me/match-history", {"games": [{"id": "g-1"}]})
        await call_text(authed_mcp_client, "duels_get_match_history", cursor="abc|def")
        assert "cursor=abc" in str(router.calls[-1].url)


class TestReplay:
    async def test_a_replay_is_dumped_whole(self, router, mcp_client):
        router.json_on("/api/replay/replay-0001", {"moves": [1, 2, 3]})
        text = await call_text(mcp_client, "duels_get_replay", replay_id="replay-0001")
        assert "Replay replay-0001" in text and "moves" in text

    async def test_a_missing_replay_is_an_error(self, router, mcp_client):
        router.json_on("/api/replay/replay-9999", {"error": "gone"}, status=404)
        text = await call_text(mcp_client, "duels_get_replay", replay_id="replay-9999")
        assert text.startswith("Error:")


class TestLeaderboard:
    BOARD = {
        "seasonInfo": {"name": "Set 13"},
        "currentUserRank": 42,
        "leaderboard": [
            {"rank": 1, "name": "Ariel", "rating": 2000},
            {"userName": "Ursula", "points": 1900},
            {"displayName": "Hades", "score": 1800},
        ],
    }

    async def test_it_works_without_a_cookie(self, router, mcp_client):
        """The leaderboard is public; requiring auth would be wrong."""
        router.json_on("/api/leaderboard", self.BOARD)
        text = await call_text(mcp_client, "duels_get_leaderboard")
        assert not text.startswith("Error:")
        assert "Set 13" in text

    async def test_every_spelling_of_name_and_rating_renders(self, router, mcp_client):
        router.json_on("/api/leaderboard", self.BOARD)
        text = await call_text(mcp_client, "duels_get_leaderboard")
        for who in ("Ariel", "Ursula", "Hades"):
            assert who in text
        for score in ("2000", "1900", "1800"):
            assert score in text

    async def test_a_row_without_a_rank_gets_its_position(self, router, mcp_client):
        router.json_on("/api/leaderboard", self.BOARD)
        text = await call_text(mcp_client, "duels_get_leaderboard")
        assert "2. **Ursula**" in text

    async def test_your_own_rank_is_called_out(self, router, mcp_client):
        router.json_on("/api/leaderboard", self.BOARD)
        text = await call_text(mcp_client, "duels_get_leaderboard")
        assert "Your rank: 42" in text

    async def test_limit_trims_the_rows(self, router, mcp_client):
        router.json_on("/api/leaderboard", self.BOARD)
        data = await call_json(mcp_client, "duels_get_leaderboard", limit=2)
        assert data["count"] == 2


class TestSeasons:
    async def test_needs_a_cookie(self, mcp_client):
        text = await call_text(mcp_client, "duels_get_seasons")
        assert text.startswith("Error:")

    async def test_results_and_lifetime_stats_are_joined(self, router, authed_mcp_client):
        router.json_on("/api/account/season-results", {"seasons": [{"id": 13}]})
        router.json_on("/api/account/history", {"stats": {"wins": 5}, "meta": {"total": 9}})
        data = await call_json(authed_mcp_client, "duels_get_seasons")
        assert data["season_results"] == {"seasons": [{"id": 13}]}
        assert data["history_stats"] == {"wins": 5}
        assert data["meta"] == {"total": 9}


class TestWhoami:
    async def test_anonymous_explains_what_still_works(self, mcp_client):
        text = await call_text(mcp_client, "duels_whoami")
        assert "Not configured" in text
        assert "bot games work" in text

    async def test_a_rejected_cookie_is_not_reported_as_anonymous(
        self, router, authed_mcp_client
    ):
        """An expired cookie and no cookie are different problems with
        different fixes, and the default session route returns null - which is
        exactly what an expired one looks like."""
        text = await call_text(authed_mcp_client, "duels_whoami")
        assert "expired" in text and "DevTools" in text

    async def test_a_good_cookie_names_the_user(self, router, authed_mcp_client):
        router.json_on(
            "/api/auth/get-session", {"user": {"name": "Ariel", "email": "a@sea"}}
        )
        text = await call_text(authed_mcp_client, "duels_whoami")
        assert "Signed in as **Ariel**" in text and "a@sea" in text


class TestAccountStats:
    async def test_needs_a_cookie(self, mcp_client):
        text = await call_text(mcp_client, "duels_get_account_stats")
        assert text.startswith("Error:")

    async def test_all_three_sections_are_fetched(self, router, authed_mcp_client):
        router.json_on("/api/account/profile", {"name": "Ariel"})
        router.json_on("/api/account/personal-stats", {"wins": 5})
        router.json_on("/api/account/season-results", {"seasons": []})
        data = await call_json(authed_mcp_client, "duels_get_account_stats")
        assert data["profile"] == {"name": "Ariel"}
        assert data["stats"] == {"wins": 5}
        assert data["seasons"] == {"seasons": []}

    async def test_one_dead_endpoint_does_not_lose_the_other_two(
        self, router, authed_mcp_client
    ):
        """Partial data is still worth having, so a failure is recorded in
        place rather than thrown."""
        router.json_on("/api/account/profile", {"name": "Ariel"})
        router.on("/api/account/personal-stats", lambda _r: httpx.Response(500, json={}))
        router.json_on("/api/account/season-results", {"seasons": []})
        data = await call_json(authed_mcp_client, "duels_get_account_stats")
        assert data["profile"] == {"name": "Ariel"}
        assert "unavailable" in data["stats"]
