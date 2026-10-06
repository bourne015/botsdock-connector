import pytest
from botsdock_connector.providers.codex_app_server import CodexConnector, normalize_appserver_notification

class FakeServer:
    def request(self, method, params):
        self.last = (method, params)
        return {"data": [], "turnId": "up-turn"}


def test_steer_uses_expected_turn_id():
    server = FakeServer()
    connector = CodexConnector(app_server=server, cwd="/tmp")
    connector._handle_turn_steer({"thread_id": "t", "turn_id": "u", "prompt": "continue", "app_server_thread_id": "up-thread", "app_server_turn_id": "up-turn"})
    assert server.last[1]["expectedTurnId"] == "up-turn"
    assert "turnId" not in server.last[1]
