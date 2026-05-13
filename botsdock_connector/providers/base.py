from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Protocol


JsonDict = dict[str, Any]


@dataclass(frozen=True)
class AgentProviderCapabilities:
    can_resume_session: bool = False
    can_cancel_turn: bool = False
    can_request_approval: bool = False
    can_report_file_activity: bool = False
    event_types: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class ProviderEnvelope:
    type: str
    payload: JsonDict = field(default_factory=dict)
    raw_event: JsonDict | None = None
    provider_event_id: str | None = None
    provider_thread_id: str | None = None
    provider_turn_id: str | None = None
    provider_session_id: str | None = None


class AgentProvider(Protocol):
    name: str
    capabilities: AgentProviderCapabilities

    async def start(self) -> None:
        ...

    async def stop(self) -> None:
        ...

    async def start_turn(self, request: JsonDict) -> AsyncIterator[ProviderEnvelope]:
        ...

    async def cancel_turn(self, request: JsonDict) -> ProviderEnvelope:
        ...

    async def resolve_approval(self, request: JsonDict) -> ProviderEnvelope:
        ...

