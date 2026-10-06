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
