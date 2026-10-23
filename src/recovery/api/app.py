"""HTTP edge: Shopify webhooks, ElevenLabs webhooks and the agent's server tools.

Webhook handlers verify signatures, persist, hand off to Temporal and return fast.
Duplicate deliveries are harmless: delivery ids are deduplicated and workflow ids are deterministic.
"""

import hmac
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any

import httpx
import structlog
from elevenlabs.client import ElevenLabs
from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from temporalio.client import Client
from temporalio.service import RPCError

from recovery import pii
from recovery.config import get_settings
from recovery.db import CallAttempt, Checkout, DoNotCall, RecoveryEvent, Shop, WebhookDelivery, session
from recovery.log_config import configure_logging
from recovery.shopify.webhooks import parse_checkout, verify_hmac
from recovery.voice.dialer import BROWSER_CALL_PREFIX
from recovery.voice.variables import call_variables
from recovery.workflows.recovery import RecoveryWorkflow
from recovery.workflows.types import CallOutcome, OrderInfo, RecoveryInput, workflow_id_for

log = structlog.get_logger()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    configure_logging()
    s = get_settings()
    app.state.temporal = await Client.connect(s.temporal_address, namespace=s.temporal_namespace)
    yield


app = FastAPI(title="cart-recovery-agent", lifespan=lifespan)


def temporal(request: Request) -> Client:
    client: Client = request.app.state.temporal
    return client


async def _signal(client: Client, workflow_id: str, signal: str, arg: Any = None) -> None:
    """Signal a workflow that may legitimately not exist (e.g. an order with no tracked checkout)."""
    try:
        handle = client.get_workflow_handle(workflow_id)
        await (handle.signal(signal, arg) if arg is not None else handle.signal(signal))
    except RPCError as e:
        log.info("signal_skipped", workflow_id=workflow_id, signal=signal, error=str(e))


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


# Shopify ---------------------------------------------------------------------------------------


@app.post("/webhooks/shopify")
async def shopify_webhook(
    request: Request,
    client: Annotated[Client, Depends(temporal)],
    x_shopify_hmac_sha256: Annotated[str | None, Header()] = None,
    x_shopify_topic: Annotated[str, Header()] = "",
    x_shopify_shop_domain: Annotated[str, Header()] = "",
    x_shopify_webhook_id: Annotated[str, Header()] = "",
) -> dict[str, str]:
    raw = await request.body()
    if not verify_hmac(raw, x_shopify_hmac_sha256, get_settings().shopify_api_secret.get_secret_value()):
        raise HTTPException(401, "invalid hmac")
    payload = json.loads(raw)
    shop = x_shopify_shop_domain

    async with session() as s:
        inserted = await s.execute(
            insert(WebhookDelivery)
            .values(id=x_shopify_webhook_id, topic=x_shopify_topic, shop=shop)
            .on_conflict_do_nothing()
            .returning(WebhookDelivery.id)
        )
        if inserted.scalar_one_or_none() is None:
            return {"status": "duplicate"}
        await s.execute(insert(Shop).values(domain=shop).on_conflict_do_nothing())

    log.info("shopify_webhook", topic=x_shopify_topic, shop=shop)
    if x_shopify_topic in ("checkouts/create", "checkouts/update"):
        await _handle_checkout(client, shop, payload)
    elif x_shopify_topic == "orders/create":
        await _handle_order(client, shop, payload)
    elif x_shopify_topic == "app/uninstalled":
        async with session() as s:
            shop_row = await s.get(Shop, shop)
            if shop_row:
                shop_row.kill_switch, shop_row.access_token_enc = True, None
    elif x_shopify_topic == "customers/redact":
        await _redact_customer(shop, payload)
    elif x_shopify_topic == "shop/redact":
        await _redact_shop(shop)
    # customers/data_request: we hold no data beyond what Shopify already has; acknowledged and logged.
    return {"status": "ok"}


async def _handle_checkout(client: Client, shop: str, payload: dict[str, Any]) -> None:
    snap = parse_checkout(payload)
    wid = workflow_id_for(shop, snap.token)
    values = {
        "shop": shop,
        "checkout_id": snap.checkout_id,
        "phone_enc": pii.encrypt(snap.phone) if snap.phone else None,
        "phone_hash": pii.phone_hash(snap.phone) if snap.phone else None,
        "country": pii.phone_country(snap.phone) if snap.phone else None,
        "first_name_enc": pii.encrypt(snap.first_name) if snap.first_name else None,
        "currency": snap.currency,
        "total_price": snap.total_price,
        "total_price_usd": snap.total_price_usd,
        "line_items": snap.line_items,
        "recovery_url": snap.recovery_url,
        "consent": snap.consent,
        "consent_source": snap.consent_source,
        "shopify_updated_at": snap.updated_at,
        "completed_at": snap.completed_at,
    }
    async with session() as s:
        stmt = insert(Checkout).values(token=snap.token, **values)
        # Out-of-order deliveries: never let an older snapshot overwrite a newer one.
        await s.execute(
            stmt.on_conflict_do_update(
                index_elements=[Checkout.token],
                set_=values,
                where=Checkout.shopify_updated_at <= stmt.excluded.shopify_updated_at,
            )
        )

    if snap.completed_at:
        return  # the orders/create webhook closes the loop
    cfg = get_settings()
    await client.start_workflow(
        RecoveryWorkflow.run,
        RecoveryInput(
            shop, snap.token, cfg.call_delay_minutes, cfg.max_call_attempts, cfg.attribution_window_hours
        ),
        id=wid,
        task_queue=cfg.task_queue,
        start_signal="checkout_updated",  # signal-with-start: restarts the quiet period if already running
    )


async def _handle_order(client: Client, shop: str, payload: dict[str, Any]) -> None:
    token = payload.get("checkout_token")
    if not token:
        return
    async with session() as s:
        checkout = await s.get(Checkout, token)
        if checkout is None:
            return
        checkout.order_id = payload["id"]
        checkout.order_value = payload.get("total_price")
    await _signal(
        client,
        workflow_id_for(shop, token),
        "order_created",
        OrderInfo(order_id=payload["id"], value=str(payload.get("total_price"))),
    )


async def _redact_customer(shop: str, payload: dict[str, Any]) -> None:
    phone = pii.normalize_phone((payload.get("customer") or {}).get("phone"))
    if not phone:
        return
    async with session() as s:
        await s.execute(
            delete(Checkout).where(Checkout.shop == shop, Checkout.phone_hash == pii.phone_hash(phone))
        )


async def _redact_shop(shop: str) -> None:
    async with session() as s:
        for model in (Checkout, CallAttempt, DoNotCall, RecoveryEvent):
            await s.execute(delete(model).where(model.shop == shop))
        await s.execute(delete(Shop).where(Shop.domain == shop))


# ElevenLabs post-call webhook -----------------------------------------------------------------

FAILURE_OUTCOMES = {"busy": "busy", "no-answer": "no_answer"}


@app.post("/webhooks/elevenlabs")
async def elevenlabs_webhook(
    request: Request,
    client: Annotated[Client, Depends(temporal)],
    elevenlabs_signature: Annotated[str | None, Header()] = None,
) -> dict[str, str]:
    raw = (await request.body()).decode()
    s = get_settings()
    try:
        event = ElevenLabs(api_key=s.elevenlabs_api_key.get_secret_value()).webhooks.construct_event(
            rawBody=raw,
            sig_header=elevenlabs_signature or "",
            secret=s.elevenlabs_webhook_secret.get_secret_value(),
        )
    except Exception as e:
        raise HTTPException(401, "invalid signature") from e

    data = event.get("data") or {}
    conversation_id = data.get("conversation_id")
    if event.get("type") == "post_call_transcription":
        outcome = CallOutcome(
            "completed", conversation_id, (data.get("metadata") or {}).get("call_duration_secs")
        )
    elif event.get("type") == "call_initiation_failure":
        outcome = CallOutcome(FAILURE_OUTCOMES.get(data.get("failure_reason", ""), "failed"), conversation_id)
    else:
        return {"status": "ignored"}

    async with session() as db:
        attempt = (
            await db.execute(select(CallAttempt).where(CallAttempt.conversation_id == conversation_id))
        ).scalar_one_or_none()
        if attempt is None:
            return {"status": "unknown_conversation"}
        attempt.outcome, attempt.duration_s = outcome.outcome, outcome.duration_s
        db.add(
            RecoveryEvent(
                workflow_id=attempt.workflow_id,
                shop=attempt.shop,
                type="call_ended",
                data={
                    "outcome": outcome.outcome,
                    "duration_s": outcome.duration_s,
                    "conversation_id": conversation_id,
                },
                agent_version=s.agent_version,
            )
        )
        workflow_id = attempt.workflow_id
    await _signal(client, workflow_id, "call_finished", outcome)
    return {"status": "ok"}


# Server tools called by the ElevenLabs agent during a call -------------------------------------


def require_tool_secret(x_tool_secret: Annotated[str, Header()] = "") -> None:
    expected = get_settings().tool_secret.get_secret_value()
    if not expected or not hmac.compare_digest(x_tool_secret, expected):
        raise HTTPException(401, "invalid tool secret")


class ToolRequest(BaseModel):
    workflow_id: str


def _token(workflow_id: str) -> tuple[str, str]:
    _, shop, token = workflow_id.split(":", 2)
    return shop, token


@app.post("/tools/get_cart", dependencies=[Depends(require_tool_secret)])
async def tool_get_cart(req: ToolRequest) -> dict[str, Any]:
    """Live cart facts. The agent may only state prices and items that this tool returned."""
    shop, token = _token(req.workflow_id)
    async with session() as s:
        c = await s.get(Checkout, token)
    if c is None or c.shop != shop:
        raise HTTPException(404, "unknown checkout")
    if c.order_id or c.completed_at:
        return {"already_purchased": True}
    return {
        "already_purchased": False,
        "items": c.line_items,
        "total": str(c.total_price),
        "currency": c.currency,
    }


@app.post("/tools/opt_out", dependencies=[Depends(require_tool_secret)])
async def tool_opt_out(req: ToolRequest) -> dict[str, Any]:
    """Idempotent: the number goes on the shop's do-not-call list immediately."""
    shop, token = _token(req.workflow_id)
    async with session() as s:
        c = await s.get(Checkout, token)
        if c is None or not c.phone_hash:
            raise HTTPException(404, "unknown checkout")
        await s.execute(
            insert(DoNotCall)
            .values(shop=shop, phone_hash=c.phone_hash, reason="customer_request")
            .on_conflict_do_nothing()
        )
        s.add(
            RecoveryEvent(
                workflow_id=req.workflow_id,
                shop=shop,
                type="opt_out",
                data={},
                agent_version=get_settings().agent_version,
            )
        )
    return {"ok": True, "say": "Confirm they will not be called again, apologize briefly and end the call."}


class SendLinkRequest(ToolRequest):
    channel: str = "sms"


@app.post("/tools/send_checkout_link", dependencies=[Depends(require_tool_secret)])
async def tool_send_checkout_link(req: SendLinkRequest) -> dict[str, Any]:
    shop, token = _token(req.workflow_id)
    async with session() as s:
        c = await s.get(Checkout, token)
        if c is None or not c.recovery_url:
            raise HTTPException(404, "unknown checkout")
        # M2: deliver via Twilio SMS / WhatsApp. M1 records the intent so the flow is testable end to end.
        s.add(
            RecoveryEvent(
                workflow_id=req.workflow_id,
                shop=shop,
                type="link_sent",
                data={"channel": req.channel},
                agent_version=get_settings().agent_version,
            )
        )
    return {"ok": True, "channel": req.channel}


# Browser phone (DIALER=browser): free end-to-end testing over the browser mic -------------------

PHONE_PAGE = Path(__file__).with_name("phone.html")
SIGNED_URL = "https://api.elevenlabs.io/v1/convai/conversation/get-signed-url"
RING_TIMEOUT = timedelta(minutes=30)  # matches the workflow's CALL_RESULT_TIMEOUT


def browser_phone_enabled(request: Request) -> None:
    """Local only: the page shows customer data, so it is refused when reached through the tunnel."""
    if get_settings().dialer != "browser" or "cf-connecting-ip" in request.headers:
        raise HTTPException(404)


phone = APIRouter(prefix="/phone", dependencies=[Depends(browser_phone_enabled)])


async def _ringing(db: AsyncSession, ring_id: str) -> CallAttempt:
    attempt = (
        await db.execute(
            select(CallAttempt).where(CallAttempt.conversation_id == ring_id, CallAttempt.outcome.is_(None))
        )
    ).scalar_one_or_none()
    if attempt is None or not ring_id.startswith(BROWSER_CALL_PREFIX):
        raise HTTPException(404, "no such ringing call")
    return attempt


async def _variables(db: AsyncSession, attempt: CallAttempt) -> dict[str, str]:
    shop, token = _token(attempt.workflow_id)
    shop_row, checkout = await db.get(Shop, shop), await db.get(Checkout, token)
    if shop_row is None or checkout is None:
        raise HTTPException(404, "unknown checkout")
    return call_variables(attempt.workflow_id, shop_row, checkout)


@phone.get("", response_class=HTMLResponse)
async def phone_page() -> str:
    return PHONE_PAGE.read_text()


@phone.get("/calls")
async def phone_calls() -> list[dict[str, str]]:
    async with session() as db:
        attempts = (
            (
                await db.execute(
                    select(CallAttempt)
                    .where(
                        CallAttempt.conversation_id.startswith(BROWSER_CALL_PREFIX),
                        CallAttempt.outcome.is_(None),
                        CallAttempt.started_at > datetime.now(UTC) - RING_TIMEOUT,
                    )
                    .order_by(CallAttempt.started_at.desc())
                )
            )
            .scalars()
            .all()
        )
        calls = []
        for a in attempts:
            v = await _variables(db, a)
            calls.append(
                {
                    "ring_id": str(a.conversation_id),
                    **{k: v[k] for k in ("shop_name", "cart_summary", "cart_total")},
                }
            )
    return calls


@phone.post("/calls/{ring_id}/answer")
async def phone_answer(ring_id: str) -> dict[str, Any]:
    s = get_settings()
    async with session() as db:
        variables = await _variables(db, await _ringing(db, ring_id))
    async with httpx.AsyncClient(timeout=10) as http:
        res = await http.get(
            SIGNED_URL,
            params={"agent_id": s.elevenlabs_agent_id},
            headers={"xi-api-key": s.elevenlabs_api_key.get_secret_value()},
        )
    res.raise_for_status()
    return {"signed_url": res.json()["signed_url"], "dynamic_variables": variables}


class Connected(BaseModel):
    conversation_id: str


@phone.post("/calls/{ring_id}/connected")
async def phone_connected(ring_id: str, body: Connected) -> dict[str, str]:
    """Swap the placeholder for the real conversation id so the post-call webhook finds this attempt."""
    async with session() as db:
        attempt = await _ringing(db, ring_id)
        attempt.conversation_id = body.conversation_id
        db.add(
            RecoveryEvent(
                workflow_id=attempt.workflow_id,
                shop=attempt.shop,
                type="call_answered",
                data={"conversation_id": body.conversation_id, "channel": "browser"},
                agent_version=get_settings().agent_version,
            )
        )
    return {"status": "ok"}


@phone.post("/calls/{ring_id}/decline")
async def phone_decline(ring_id: str, client: Annotated[Client, Depends(temporal)]) -> dict[str, str]:
    """Simulates an unanswered call, exercising the retry path."""
    async with session() as db:
        attempt = await _ringing(db, ring_id)
        attempt.outcome = "no_answer"
        workflow_id = attempt.workflow_id
    await _signal(client, workflow_id, "call_finished", CallOutcome("no_answer", ring_id))
    return {"status": "ok"}


app.include_router(phone)
