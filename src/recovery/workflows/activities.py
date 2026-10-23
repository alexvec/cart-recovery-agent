"""Activities: every side effect of the recovery workflow (database, policy inputs, dialing)."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from temporalio import activity

from recovery import economics, pii, policy
from recovery.config import get_settings
from recovery.db import CallAttempt, Checkout, DoNotCall, RecoveryEvent, Shop, session
from recovery.voice.dialer import CallRequest, get_dialer
from recovery.workflows.types import EventIn, PolicyResult, RecoveryInput, workflow_id_for


async def _log_event(s: AsyncSession, workflow_id: str, shop: str, type_: str, data: dict[str, Any]) -> None:
    s.add(
        RecoveryEvent(
            workflow_id=workflow_id,
            shop=shop,
            type=type_,
            data=data,
            agent_version=get_settings().agent_version,
        )
    )


@activity.defn
async def record_event(e: EventIn) -> None:
    async with session() as s:
        await _log_event(s, e.workflow_id, e.shop, e.type, e.data)


@activity.defn
async def evaluate_policy(inp: RecoveryInput) -> PolicyResult:
    wid = workflow_id_for(inp.shop, inp.checkout_token)
    now = datetime.now(UTC)
    settings = get_settings()
    async with session() as s:
        checkout = await s.get(Checkout, inp.checkout_token)
        shop = await s.get(Shop, inp.shop)
        if checkout is None or shop is None:
            raise ValueError(f"unknown checkout or shop for {wid}")

        country, on_dnc, last_24h, last_7d = "", False, 0, 0
        zones: tuple[str, ...] = ()
        if checkout.phone_enc and checkout.phone_hash:
            e164 = pii.decrypt(checkout.phone_enc)
            country, zones = pii.phone_country(e164), pii.phone_timezones(e164)
            on_dnc = await s.get(DoNotCall, (inp.shop, checkout.phone_hash)) is not None
            attempts = (
                select(func.count())
                .select_from(CallAttempt)
                .where(CallAttempt.phone_hash == checkout.phone_hash, CallAttempt.shop == inp.shop)
            )
            last_24h = (
                await s.execute(attempts.where(CallAttempt.started_at > now - timedelta(hours=24)))
            ).scalar_one()
            last_7d = (
                await s.execute(attempts.where(CallAttempt.started_at > now - timedelta(days=7)))
            ).scalar_one()

        today = now.replace(hour=0, minute=0, second=0, microsecond=0)
        spend: Decimal | None = (
            await s.execute(
                select(func.coalesce(func.sum(CallAttempt.cost_usd), Decimal(0))).where(
                    CallAttempt.shop == inp.shop, CallAttempt.started_at >= today
                )
            )
        ).scalar_one()

        decision = policy.evaluate(
            policy.PolicyInput(
                now=now,
                kill_switch=shop.kill_switch,
                order_exists=checkout.order_id is not None or checkout.completed_at is not None,
                has_phone=checkout.phone_enc is not None,
                consent=checkout.consent,
                on_do_not_call=on_dnc,
                country=country,
                timezones=zones,
                attempts_last_24h=last_24h,
                attempts_last_7d=last_7d,
                cart_value=checkout.total_price_usd,
                min_cart_value=shop.min_cart_value_usd or economics.min_cart_value(country),
                in_stock=True,  # M2: live inventory check through the Admin API
                spend_today=Decimal(spend or 0),
                daily_budget=shop.daily_budget_usd,
                is_test_or_risky=checkout.is_test,
                in_holdout=policy.in_holdout(inp.shop, inp.checkout_token, settings.holdout_pct),
            )
        )
        await _log_event(
            s,
            wid,
            inp.shop,
            "policy_decision",
            {
                "allowed": decision.allowed,
                "reason": decision.reason.value,
                "cart_value_usd": str(checkout.total_price_usd),
                "country": country,
            },
        )
    return PolicyResult(
        decision.allowed, decision.reason.value, decision.retry_at.isoformat() if decision.retry_at else None
    )


@activity.defn
async def place_call(inp: RecoveryInput) -> str:
    wid = workflow_id_for(inp.shop, inp.checkout_token)
    async with session() as s:
        checkout = await s.get(Checkout, inp.checkout_token)
        shop = await s.get(Shop, inp.shop)
        if checkout is None or shop is None or not checkout.phone_enc or not checkout.phone_hash:
            raise ValueError(f"cannot call for {wid}")
        phone_hash = checkout.phone_hash
        req = CallRequest(
            workflow_id=wid,
            to_number=pii.decrypt(checkout.phone_enc),
            dynamic_variables={
                "shop_name": shop.name or inp.shop.split(".")[0],
                "first_name": pii.decrypt(checkout.first_name_enc) if checkout.first_name_enc else "there",
                "cart_summary": ", ".join(f"{li['quantity']} x {li['title']}" for li in checkout.line_items),
                "cart_total": f"{checkout.total_price} {checkout.currency}",
            },
        )

    conversation_id = await get_dialer().dial(req)

    async with session() as s:
        s.add(
            CallAttempt(
                workflow_id=wid, shop=inp.shop, phone_hash=phone_hash, conversation_id=conversation_id
            )
        )
        await _log_event(s, wid, inp.shop, "call_placed", {"conversation_id": conversation_id})
    return conversation_id
