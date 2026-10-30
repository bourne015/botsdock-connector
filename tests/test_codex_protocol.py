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

@pytest.mark.parametrize("sandbox,kind", [("read-only", "readOnly"), ("workspace-write", "workspaceWrite"), ("danger-full-access", "dangerFullAccess")])
def test_explicit_sandbox_is_preserved(sandbox, kind):
    connector = CodexConnector(app_server=FakeServer(), cwd="/tmp")
    assert connector._turn_start_params("t", "hello", {"sandbox": sandbox})["sandboxPolicy"]["type"] == kind

def test_missing_sandbox_inherits_runtime_thread_policy():
    connector = CodexConnector(app_server=FakeServer(), cwd="/tmp")
    assert "sandboxPolicy" not in connector._turn_start_params("t", "hello", {})

@pytest.mark.parametrize("method,event", [("item/plan/delta", "plan.delta"), ("item/reasoning/summaryTextDelta", "reasoning.delta"), ("item/reasoning/textDelta", "reasoning.delta")])
def test_current_item_deltas(method, event):
    kind, payload = normalize_appserver_notification(method, {"itemId": "i", "delta": "hello"})
    assert kind == event
    assert payload["item_id"] == "i"
    assert payload["text"] == "hello"

def test_history_requests_full_items():
    server = FakeServer()
    connector = CodexConnector(app_server=server, cwd="/tmp")
    connector._handle_thread_history({"app_server_thread_id": "up-thread"})
    assert server.last[1]["itemsView"] == "full"

@pytest.mark.parametrize("policy,expected,reviewer", [("auto-review", "on-request", "auto_review"), ("never", "never", "user"), ("always-allow", "never", "user"), ("on-failure", "on-request", "user")])
def test_permission_modes_use_official_fields(policy, expected, reviewer):
    connector = CodexConnector(app_server=FakeServer(), cwd="/tmp")
    params = connector._turn_start_params("t", "hello", {"approval_policy": policy})
    assert params["approvalPolicy"] == expected
    assert params["approvalsReviewer"] == reviewer

def test_resume_without_policy_does_not_override_local_reviewer():
    connector = CodexConnector(app_server=FakeServer(), cwd="/tmp")
    params = connector._turn_start_params("t", "hello", {})
    assert "approvalPolicy" not in params
    assert "approvalsReviewer" not in params

def test_account_snapshot_includes_paginated_runtime_models():
    class Server:
        def request(self, method, params=None):
            if method == "model/list":
                if params.get("cursor") == "page-2":
                    return {"data": [{"id": "new-2", "model": "new-2", "supportedReasoningEfforts": [{"reasoningEffort": "high"}]}]}
                return {"data": [{"id": "new-1", "model": "new-1", "isDefault": True}, {"id": "hidden", "hidden": True}], "nextCursor": "page-2"}
            return {"requiresOpenaiAuth": False}
    result = CodexConnector(app_server=Server(), cwd="/tmp")._handle_account_snapshot({})
    assert [model["id"] for model in result["models"]] == ["new-1", "new-2"]
    assert result["models"][1]["supportedReasoningEfforts"][0]["reasoningEffort"] == "high"


def test_connector_session_command_approval_matches_exact_scope():
    class Server(FakeServer):
        def __init__(self):
            self.responses = []

        def send_response(self, request_id, response):
            self.responses.append((request_id, response))

    server = Server()
    connector = CodexConnector(app_server=server, cwd='/tmp')
    method = 'item/commandExecution/requestApproval'
    params = {'threadId': 'thread', 'turnId': 'turn-1', 'itemId': 'item-1',
              'cwd': '/tmp', 'command': 'npm test', 'availableDecisions': ['accept', 'cancel']}
    event = connector._normalize_server_request({'id': 1, 'method': method, 'params': params}, method, params)
    assert event['payload']['available_decisions'] == ['accept', 'acceptForSession', 'cancel']
    assert event['payload']['session_approval_scope'] == 'exact_operation'
    connector._handle_approval_respond({'app_server_request_id': 1, 'response': {'decision': 'acceptForSession'}})
    assert server.responses == [(1, {'decision': 'accept'})]
    repeated = {**params, 'turnId': 'turn-2', 'itemId': 'item-2'}
    assert connector._normalize_server_request({'id': 2, 'params': repeated}, method, repeated) is None
    assert server.responses[-1] == (2, {'decision': 'accept'})
    for index, change in enumerate([
        {'threadId': 'other'}, {'cwd': '/other'}, {'command': 'npm publish'},
        {'additionalPermissions': {'network': True}}, {'availableDecisions': ['cancel']},
    ], 3):
        changed = {**repeated, **change}
        assert connector._normalize_server_request({'id': index, 'params': changed}, method, changed) is not None
    fresh = CodexConnector(app_server=server, cwd='/tmp')
    assert fresh._normalize_server_request({'id': 9, 'params': repeated}, method, repeated) is not None


def test_native_session_approval_is_forwarded_without_connector_rule():
    class Server(FakeServer):
        def send_response(self, request_id, response):
            self.response = response
    server = Server()
    connector = CodexConnector(app_server=server, cwd='/tmp')
    method = 'item/commandExecution/requestApproval'
    params = {'threadId': 'thread', 'cwd': '/tmp', 'command': 'npm test',
              'availableDecisions': ['accept', 'acceptForSession', 'cancel']}
    event = connector._normalize_server_request({'id': 1, 'method': method, 'params': params}, method, params)
    assert 'session_approval_scope' not in event['payload']
    connector._handle_approval_respond({'app_server_request_id': 1, 'response': {'decision': 'acceptForSession'}})
    assert server.response == {'decision': 'acceptForSession'}
    assert not connector._session_command_approvals
