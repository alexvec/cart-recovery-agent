"""Outbound calling. The agent itself lives on ElevenLabs; this module only starts calls."""

import secrets
import uuid
from dataclasses import dataclass
from typing import Any, Protocol

import httpx
import structlog

from recovery.config import get_settings

log = structlog.get_logger()

OUTBOUND_CALL_URL = "https://api.elevenlabs.io/v1/convai/twilio/outbound-call"
BROWSER_CALL_PREFIX = "web-"


@dataclass(frozen=True)
class CallRequest:
    workflow_id: str
    to_number: str  # E.164
    dynamic_variables: dict[str, Any]


class Dialer(Protocol):
    async def dial(self, req: CallRequest) -> str:
        """Start a call and return the provider's conversation id."""
        ...


class ElevenLabsDialer:
    async def dial(self, req: CallRequest) -> str:
        s = get_settings()
        body = {
            "agent_id": s.elevenlabs_agent_id,
            "agent_phone_number_id": s.elevenlabs_phone_number_id,
            "to_number": req.to_number,
            "conversation_initiation_client_data": {"dynamic_variables": req.dynamic_variables},
            "telephony_call_config": {"ringing_timeout_secs": 30},
        }
        async with httpx.AsyncClient(timeout=20) as client:
            res = await client.post(
                OUTBOUND_CALL_URL, json=body, headers={"xi-api-key": s.elevenlabs_api_key.get_secret_value()}
            )
        res.raise_for_status()
        data = res.json()
        if not data.get("success") or not data.get("conversation_id"):
            raise RuntimeError(f"ElevenLabs refused the call: {data.get('message')}")
        return str(data["conversation_id"])


class FakeDialer:
    """Local development: no phone rings. Outcomes are simulated with scripts/simulate_call.py."""

    async def dial(self, req: CallRequest) -> str:
        conversation_id = f"fake-{uuid.uuid4().hex[:12]}"
        log.info("fake_call_placed", workflow_id=req.workflow_id, conversation_id=conversation_id)
        return conversation_id


class BrowserDialer:
    """Free end-to-end testing: the call "rings" on the local /phone page and runs over the browser mic.

    The returned id is a placeholder; the page swaps in the real ElevenLabs conversation id once it
    connects, so the post-call webhook and the rest of the workflow behave exactly as for a phone call.
    """

    async def dial(self, req: CallRequest) -> str:
        ring_id = f"{BROWSER_CALL_PREFIX}{secrets.token_urlsafe(16)}"
        log.info("browser_call_ringing", workflow_id=req.workflow_id, ring_id=ring_id)
        return ring_id


def get_dialer() -> Dialer:
    dialers: dict[str, type[Dialer]] = {
        "elevenlabs": ElevenLabsDialer,
        "browser": BrowserDialer,
        "fake": FakeDialer,
    }
    return dialers[get_settings().dialer]()
