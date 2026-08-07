"""Agent2Agent (A2A) — the wire format for one agent calling another.

WHAT IS IMPLEMENTED, PRECISELY
    The core of the A2A interaction model:
      - Agent Card discovery at `/.well-known/agent-card.json`, describing the
        agent's identity, capabilities, and skills.
      - A JSON-RPC 2.0 `message/send` method that accepts a Message and returns
        a completed Task carrying the reply.

WHAT IS NOT IMPLEMENTED
    Streaming (`message/stream`), push notifications, multi-turn task state
    (`input-required`, `working`), task cancellation, and authenticated extended
    cards. Those are real parts of the spec and their absence is a limitation,
    not a simplification to gloss over.

    Concretely: this speaks enough A2A for one agent to discover another and
    delegate a unit of work to it. It is not a compliant A2A implementation, and
    the README says so.

WHY IMPLEMENTED DIRECTLY RATHER THAN VIA THE SDK
    The wire format is small and the value here is demonstrating that the
    *protocol shape* is understood — agent cards, skills, JSON-RPC tasks, the
    artifact/message distinction. Taking the SDK dependency would hide exactly
    the part worth showing, and could not be verified end to end in this
    environment anyway. Recorded in docs/design-decisions.md.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

A2A_PROTOCOL_VERSION = "0.3.0"


@dataclass(frozen=True)
class AgentSkill:
    """One capability an agent advertises.

    Skills are what make an agent card useful to another *agent* rather than to
    a human: the calling model reads these to decide whether delegation is
    worthwhile at all.
    """

    id: str
    name: str
    description: str
    tags: tuple[str, ...] = ()
    examples: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "tags": list(self.tags),
            "examples": list(self.examples),
        }


@dataclass(frozen=True)
class AgentCard:
    """The A2A discovery document."""

    name: str
    description: str
    url: str
    version: str
    skills: tuple[AgentSkill, ...] = ()
    default_input_modes: tuple[str, ...] = ("text/plain",)
    default_output_modes: tuple[str, ...] = ("text/plain",)
    capabilities: dict[str, Any] = field(
        # Advertised honestly: this agent does not stream and does not support
        # push notifications, so it says so rather than claiming the defaults.
        default_factory=lambda: {"streaming": False, "pushNotifications": False}
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocolVersion": A2A_PROTOCOL_VERSION,
            "name": self.name,
            "description": self.description,
            "url": self.url,
            "version": self.version,
            "capabilities": self.capabilities,
            "defaultInputModes": list(self.default_input_modes),
            "defaultOutputModes": list(self.default_output_modes),
            "skills": [s.to_dict() for s in self.skills],
        }


def build_message(text: str, *, role: str = "user") -> dict[str, Any]:
    """An A2A Message: a role plus a list of typed parts."""
    return {
        "role": role,
        "parts": [{"kind": "text", "text": text}],
        "messageId": uuid.uuid4().hex,
        "kind": "message",
    }


def build_send_request(text: str, *, request_id: str | None = None) -> dict[str, Any]:
    """A JSON-RPC 2.0 envelope for `message/send`."""
    return {
        "jsonrpc": "2.0",
        "id": request_id or uuid.uuid4().hex,
        "method": "message/send",
        "params": {"message": build_message(text)},
    }


def build_task_response(
    request_id: str, text: str, *, task_id: str | None = None
) -> dict[str, Any]:
    """A completed A2A Task carrying the agent's reply.

    The reply is returned as an ARTIFACT rather than a status message: in A2A,
    artifacts are the durable output of the work, while status messages are
    progress commentary. Delegated analysis is output.
    """
    tid = task_id or uuid.uuid4().hex
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "result": {
            "id": tid,
            "contextId": uuid.uuid4().hex,
            "status": {"state": "completed"},
            "artifacts": [
                {
                    "artifactId": uuid.uuid4().hex,
                    "name": "analysis",
                    "parts": [{"kind": "text", "text": text}],
                }
            ],
            "kind": "task",
        },
    }


def build_error_response(
    request_id: str | None, code: int, message: str
) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message},
    }


def extract_text(message: dict[str, Any]) -> str:
    """Pull the text out of an A2A Message's parts.

    Parts are typed and a message may carry several; non-text parts (files,
    structured data) are skipped rather than stringified into nonsense.
    """
    return "\n".join(
        part.get("text", "")
        for part in message.get("parts", [])
        if part.get("kind") == "text"
    ).strip()


def extract_task_text(response: dict[str, Any]) -> str:
    """Pull the reply out of a Task response, artifacts first.

    Falls back to a status message so a peer that reports progress-style output
    still yields something usable rather than an empty string.
    """
    result = response.get("result") or {}

    for artifact in result.get("artifacts") or []:
        text = "\n".join(
            part.get("text", "")
            for part in artifact.get("parts", [])
            if part.get("kind") == "text"
        ).strip()
        if text:
            return text

    status_message = (result.get("status") or {}).get("message")
    if status_message:
        return extract_text(status_message)

    return ""
