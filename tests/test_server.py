"""Tests for the entrypoint and the deploy contract.

MCPize probes /health and will never mark the service `running` without it, so
this path is worth covering even though it is only a handful of lines.
"""

import importlib

import pytest


@pytest.fixture(scope="module")
def server_module():
    return importlib.import_module("src.server")


class TestHealthRoute:
    def test_health_is_registered(self, server_module):
        routes = server_module.mcp._get_additional_http_routes()
        paths = {getattr(r, "path", None) for r in routes}
        assert "/health" in paths

    def test_health_answers_get_and_head(self, server_module):
        """The probe may use either verb."""
        routes = server_module.mcp._get_additional_http_routes()
        health = next(r for r in routes if getattr(r, "path", None) == "/health")
        assert {"GET", "HEAD"} <= set(health.methods)

    async def test_health_payload_names_the_service(self, server_module):
        import json

        response = await server_module.health(None)
        assert json.loads(response.body) == {"status": "ok", "service": "duels_mcp"}


class TestTransportSelection:
    def test_cloud_mode_is_detected_from_the_injected_port(self, server_module, monkeypatch):
        """MCPize sets UPSTREAM_PORT_START in the cloud; without it we must run
        over stdio so the JSON-RPC channel works locally."""
        captured = {}

        def fake_run(**kwargs):
            captured.update(kwargs)

        monkeypatch.setattr(server_module.mcp, "run", lambda **kw: fake_run(**kw))
        monkeypatch.setenv("UPSTREAM_PORT_START", "8081")
        server_module.main()

        assert captured["transport"] == "streamable-http"
        assert captured["port"] == 8081
        assert captured["path"] == "/mcp"
        assert captured["json_response"] is True

    def test_local_mode_uses_stdio(self, server_module, monkeypatch):
        captured = {}
        monkeypatch.setattr(server_module.mcp, "run", lambda **kw: captured.update(kw))
        monkeypatch.delenv("UPSTREAM_PORT_START", raising=False)
        server_module.main()
        assert captured["transport"] == "stdio"


class TestServerIdentity:
    def test_name_follows_the_mcp_convention(self, server_module):
        """{service}_mcp - this is the name an agent sees."""
        assert server_module.SERVER_NAME == "duels_mcp"

    def test_instructions_warn_about_the_two_id_kinds(self, server_module):
        from src.app import INSTRUCTIONS

        assert "definitionId" in INSTRUCTIONS and "instanceId" in INSTRUCTIONS

    def test_instructions_say_rules_knowledge_is_not_needed(self, server_module):
        from src.app import INSTRUCTIONS

        assert "legal_moves" in INSTRUCTIONS
