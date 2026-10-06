from dataclasses import dataclass
from types import SimpleNamespace
from pathlib import Path
import pytest
from botsdock_connector.providers.claude_agent_sdk import ClaudeAgentSdkProvider

@dataclass
class ToolUseBlock:
    id: str
    name: str
    input: dict

@dataclass
class ToolResultBlock:
    tool_use_id: str
    content: str
    is_error: bool = False

@dataclass
class AssistantMessage:
    content: list

@dataclass
class UserMessage:
    content: list

@pytest.mark.parametrize("tool,events", [("Bash", ["command.output", "command.completed"]), ("Edit", ["file.changed"]), ("Read", ["provider.debug"])])
def test_user_message_tool_results_complete_matching_activity(tool, events):
    provider = ClaudeAgentSdkProvider(cwd="/tmp")
    request = {"turn_id": "t"}
    provider.map_message(request, AssistantMessage([ToolUseBlock("i", tool, {"command": "pwd", "file_path": "/tmp/test"})]))
    results = provider.map_message(request, UserMessage([ToolResultBlock("i", "ok")]))
    assert [event.type for event in results] == events
    assert results[-1].payload["status"] == "completed"
    assert results[0].payload["text"] == "ok"
    if tool == "Edit":
        assert results[0].payload["path"] == "/tmp/test"

def test_tool_result_tracks_tools_per_turn():
    provider = ClaudeAgentSdkProvider(cwd="/tmp")
    for turn, tool in [("a", "Bash"), ("b", "Read")]:
        provider.map_message({"turn_id": turn}, AssistantMessage([ToolUseBlock("i", tool, {})]))
    assert provider.map_message({"turn_id": "b"}, UserMessage([ToolResultBlock("i", "ok")]))[0].type == "provider.debug"
    assert provider.map_message({"turn_id": "a"}, UserMessage([ToolResultBlock("i", "ok")]))[0].type == "command.output"

@dataclass
class StreamEvent:
    event: dict
    uuid: str = "event-1"
    session_id: str = "session-1"

@dataclass
class TextBlock:
    text: str

def test_partial_messages_are_enabled_and_mapped():
    provider = ClaudeAgentSdkProvider(cwd="/tmp")
    provider._sdk = SimpleNamespace(ClaudeAgentOptions=lambda **kwargs: kwargs)
    options = provider._build_options({}, cwd=Path("/tmp"), can_use_tool=None)
    assert options["include_partial_messages"] is True
    for delta, event in [({"text": "hello"}, "assistant.delta"), ({"thinking": "think"}, "reasoning.delta")]:
        result = provider.map_message({}, StreamEvent({"type": "content_block_delta", "delta": delta}))
        assert result[0].type == event

def test_snapshot_block_ids_are_distinct():
    provider = ClaudeAgentSdkProvider(cwd="/tmp")
    message = AssistantMessage([TextBlock("first"), TextBlock("second")])
    message.uuid = "transcript-1"
    events = provider.map_message({}, message)
    assert [e.provider_event_id for e in events] == ["transcript-1:0", "transcript-1:1"]

def test_current_sdk_options_and_tool_result_types():
    sdk = pytest.importorskip('claude_agent_sdk')
    from claude_agent_sdk.types import AssistantMessage, ToolUseBlock, UserMessage, ToolResultBlock
    provider = ClaudeAgentSdkProvider(cwd='/tmp')
    provider._sdk = sdk
    options = provider._build_options({'reasoning_effort': 'xhigh'}, cwd=Path('/tmp'), can_use_tool=None)
    assert isinstance(options, sdk.ClaudeAgentOptions)
    assert options.effort == 'xhigh'
    assert options.include_partial_messages is True
    request = {'turn_id': 't'}
    provider.map_message(request, AssistantMessage(content=[ToolUseBlock('sdk-tool', 'Bash', {'command': 'pwd'})], model='claude'))
    result = provider.map_message(request, UserMessage(content=[ToolResultBlock('sdk-tool', 'ok')]))
    assert [e.type for e in result] == ['command.output', 'command.completed']
