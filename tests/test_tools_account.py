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
    # The shape /api/me/match-history really sends - every key as seen on
    # 2026-09-23, with names and ids made up. The formatter used to read
    # opponentName, finishedAt and id, which this endpoint has never sent,
    # and printed "vs ?" and "None" for every game. The test it replaced
    # passed, because it was written from the same guess.
    RANKED_LOSS = {
        "game_id": "g-1",
        "match_id": None,
        "match_format": "bo1",
        "match_game_number": None,
        "mode": "matchmaking",
        "queue_id": "core-bo1",
        "queue_name": "Core Set 13 BO1",
        "ranked": True,
        "season_id": None,
        "season_name": "Core BO1 - Set 13",
        "started_at": "2026-09-23T19:31:58.402Z",
        "ended_at": "2026-09-23T19:42:34.423Z",
        "duration_seconds": 636,
        "turns": 12,
        "result": "loss",
        "end_reason": "lore",
        "your_player": 1,
        "went_first": False,
        "your_lore": 6,
        "opp_lore": 21,
        "mmr_before": 798,
        "mmr_after": 792,
        "mmr_delta": -6,
        "is_placement": False,
        "placement_number": None,
        "your_user_id": "u-me",
        "your_deck_id": "starter-set-9-standout-headliners",
        "your_deck_colors": "Emerald/Ruby",
        "your_decklist": [{"cardId": "9-96", "count": 3}, {"cardId": "9-133", "count": 2}],
        "opp_display_name": "Ariel",
        "opp_guest": False,
        "opp_is_bot": False,
        "opp_deck_colors": "Amethyst/Steel",
        "replay_id": "r-1",
        "replay_filename": "g-1_p1.replay.gz",
        "replay_url": "https://duels.ink/r/r-1",
        "gamelog_id": "g-1",
        "gamelog_filename": "g-1.logs.gz",
        "gamelog_url": "https://duels.ink/g/g-1",
    }
    QUICK_PLAY_LOSS = {
        **RANKED_LOSS,
        "game_id": "g-2",
        "queue_id": "quick-play",
        "queue_name": "Quick Play: Core BO1",
        "ranked": False,
        "season_name": "Quick Play: Core BO1",
        "your_lore": 15,
        "mmr_before": None,
        "mmr_after": None,
        "mmr_delta": None,
        "opp_display_name": "Ursula",
    }
    ABANDONED_BOT_GAME = {
        **QUICK_PLAY_LOSS,
        "game_id": "g-3",
        "mode": "bot",
        "queue_id": None,
        "queue_name": None,
        "season_name": None,
        "result": "abandoned",
        "end_reason": "abandoned",
        "your_lore": 0,
        "opp_lore": 0,
        "opp_display_name": "Bot (Normal)",
        "opp_is_bot": True,
        "replay_id": None,
    }

    async def history(self, router, client, *games) -> str:
        router.json_on("/api/me/match-history", {"games": list(games)})
        return await call_text(client, "duels_get_match_history")

    async def test_needs_a_cookie(self, mcp_client):
        text = await call_text(mcp_client, "duels_get_match_history")
        assert text.startswith("Error:")

    async def test_no_matches_points_at_how_to_get_one(self, router, authed_mcp_client):
        router.json_on("/api/me/match-history", {"games": []})
        text = await call_text(authed_mcp_client, "duels_get_match_history")
        assert "Play a ranked or table game first" in text

    async def test_a_ranked_game_reads_in_full(self, router, authed_mcp_client):
        text = await self.history(router, authed_mcp_client, self.RANKED_LOSS)
        assert "**loss** 6-21 vs Ariel" in text
        assert "ended by lore" in text and "Core Set 13 BO1" in text
        assert "MMR 798 -> 792 (-6)" in text
        assert "2026-09-23T19:31:58.402Z" in text
        assert "`g-1`" in text
        assert "vs ?" not in text and "None" not in text

    async def test_an_unranked_game_names_its_queue_and_no_rating(
        self, router, authed_mcp_client
    ):
        text = await self.history(router, authed_mcp_client, self.QUICK_PLAY_LOSS)
        assert "**loss** 15-21 vs Ursula" in text and "Quick Play: Core BO1" in text
        assert "MMR" not in text and "None" not in text

    async def test_an_abandoned_bot_game_keeps_its_zero_score(self, router, authed_mcp_client):
        """Lore is 0 in an abandoned game, and a bot or table game has no
        queue - only a mode. The site's bot already says it is one."""
        text = await self.history(router, authed_mcp_client, self.ABANDONED_BOT_GAME)
        assert "**abandoned** 0-0 vs Bot (Normal) - ended by abandoned - bot -" in text
        assert "(bot)" not in text and "None" not in text

    async def test_a_bot_is_flagged_when_its_name_does_not_say_so(
        self, router, authed_mcp_client
    ):
        bot = {**self.ABANDONED_BOT_GAME, "opp_display_name": "Merlin"}
        text = await self.history(router, authed_mcp_client, bot)
        assert "vs Merlin (bot)" in text

    async def test_a_rating_gain_carries_its_sign(self, router, authed_mcp_client):
        win = {
            **self.RANKED_LOSS,
            "result": "win",
            "your_lore": 20,
            "opp_lore": 14,
            "mmr_before": 792,
            "mmr_after": 804,
            "mmr_delta": 12,
        }
        text = await self.history(router, authed_mcp_client, win)
        assert "**win** 20-14" in text and "MMR 792 -> 804 (+12)" in text

    async def test_a_sparse_row_prints_no_none(self, router, authed_mcp_client):
        """What broke before was a missing key turning into "None"."""
        text = await self.history(router, authed_mcp_client, {"result": "win"})
        assert "**win** vs ?" in text and "None" not in text

    async def test_the_cursor_is_handed_back_ready_to_use(self, router, authed_mcp_client):
        """Pagination is useless if the caller has to guess the argument."""
        router.json_on(
            "/api/me/match-history", {"games": [{"game_id": "g-1"}], "next_cursor": "abc|def"}
        )
        text = await call_text(authed_mcp_client, "duels_get_match_history")
        assert "cursor='abc|def'" in text

    async def test_a_cursor_is_forwarded_to_the_api(self, router, authed_mcp_client):
        router.json_on("/api/me/match-history", {"games": [{"game_id": "g-1"}]})
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
