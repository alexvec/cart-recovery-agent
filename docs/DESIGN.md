# Cart Recovery Voice Agent: Design

Status: draft v0.1 (2026-10-22)

## 1. Problem

About 70% of online carts are abandoned, according to Baymard's 50-study average (70.22% in 2026). Shopify merchants already recover some of these with email and SMS flows. Klaviyo's 2026 benchmark puts abandoned-cart **revenue per recipient** at **$6.77 for email** and **$4.48 for SMS**.

Voice can do something those channels can't: answer the question that caused the abandonment. Typical questions are about sizing, delivery dates, cash on delivery or a fee the shopper didn't expect. Vendor case studies report **58–65% connect rates** and **10–24% conversion of contacted carts**. These figures are self-reported (GoKwik, BiteSpeed, LimeChat), and proving or disproving them with a holdout is one of this project's goals.

Voice is also 10–100x more expensive per touch than email, and outbound calling is heavily regulated. The product is therefore really a **policy and economics engine**: it decides *which* carts deserve a call, makes that call safely, and proves the incremental revenue.

## 2. Goals and non-goals

**Goals**
- Shopify app: install with OAuth, configure, and see results inside the Shopify admin.
- Call eligible abandoned checkouts within minutes. Fall back to WhatsApp or SMS.
- Measure **incremental** recovered revenue against a randomized holdout.
- Enforce compliance and spending guardrails deterministically, outside the LLM.
- Run in production: tracing, metrics, SLOs, alerts, a kill switch, and CI gated on evaluations.

**Non-goals (v1)**
- Taking payments over the phone. The agent never collects card data, which keeps the system out of PCI scope.
- Inbound support calls, WooCommerce or Salla. The platform adapter interface leaves room for these later.

## 3. Architecture

```
Shopify ──webhooks (HMAC)──► Ingest API ──► Postgres (outbox) ──► Temporal
  checkouts/create|update                                         │
  orders/create                                                   ▼
  app/uninstalled, GDPR                              RecoveryWorkflow (per checkout)
                                                      1. wait N min (timer)
                                                      2. Policy check (deterministic)
                                                      3. Holdout assignment
                                                      4. place call ───────────────► ElevenLabs Agent ◄─► Twilio ◄─► customer
                                                      5. await post-call webhook       │ server tools (HTTPS, signed):
                                                      6. retry / fallback channel      │  get_cart, check_stock,
                                                      7. attribution window (72h)      │  create_discount, send_checkout_link,
                                                    signal: order_created ─► cancel    │  opt_out, transfer_to_human
                                                                                       ▼
                                                                         Tools API ──► Shopify Admin GraphQL
```

The key design choice is **one durable workflow per abandoned checkout**. Waiting, retries, fallback, cancellation when the shopper buys before the call, and attribution are all explicit, testable steps that survive crashes and restarts.

## 4. Tech stack

| Concern | Choice | Why this over the alternative |
|---|---|---|
| Shopify app shell | Official **Shopify app template** (React Router, TypeScript) + Polaris UI + App Bridge | The industry default: it handles OAuth, session tokens, billing and the embedded admin. Kept thin, with only settings and dashboards. |
| Core services | **Python 3.12, FastAPI, Pydantic v2** | Typed API contracts and strong evaluation and data tooling. |
| Orchestration | **Temporal** (Temporal Cloud in production, the dev server locally) | Durable timers, retries and signals. Used for payments and order workflows at Stripe, Netflix and others. A cron job plus a queue would hide the race conditions this project wants to show off. |
| Database | **Postgres 16** (RDS) + **SQLAlchemy 2 / Alembic** | Transactional outbox, idempotency keys, audit log. |
| Cache and rate limiting | **Redis** | Per-shop token bucket for Shopify's cost-based GraphQL throttling, plus per-number call caps. |
| Voice | **ElevenLabs Agents** + **Twilio** (native integration) | Managed turn-taking and Arabic/English voices. Server tools keep every action deterministic and auditable. |
| Messaging fallback | **Twilio** SMS and WhatsApp (Meta templates) | WhatsApp is the main channel in the GCC. |
| Webhook delivery | Shopify **Amazon EventBridge** delivery in production, HTTPS locally | Shopify's recommended pattern at scale: no lost events during deploys. |
| Infrastructure | **AWS me-central-1 (UAE)**, **Terraform**, ECS Fargate, RDS, Secrets Manager, KMS | Keeps data in the UAE region and makes infrastructure reviewable code. |
| CI/CD | **GitHub Actions** using short-lived cloud credentials (no stored keys): lint (ruff, mypy), tests, Trivy image scan, **evaluation gate**, deploy to staging, manual promotion to production | No long-lived keys, and a release can't ship if it regresses agent quality. |
| Observability | **OpenTelemetry** → Grafana (Prometheus, Tempo, Loki), **Langfuse** for conversation traces | One trace from webhook to call to order. |
| Alerting | Grafana alerts → PagerDuty or Slack, with runbooks in `docs/runbooks/` | Fires when an SLO's error budget is being used up too fast. |
| Load and chaos testing | **k6** to simulate a Black Friday/Cyber Monday webhook burst, plus killing workers mid-workflow | Proves the system recovers from failure. |

## 5. Guardrails

The guardrails are layered so that no single LLM mistake can break a rule.

### 5.1 Before the call: deterministic policy, no LLM

The checks run in order, and the first failure stops the call. Each one logs a reason code.

| # | Rule | Source |
|---|---|---|
| P1 | The shop's global and per-shop **kill switch** is off | Ops |
| P2 | The checkout has **not** become an order (checked again just before dialling) | Correctness |
| P3 | The customer has **consent** to be called (a consent checkbox or SMS marketing consent, with a timestamp) | US TCPA rules. The FCC ruled in 2024 that AI-generated voices count as "artificial" voices, so calls need prior express consent. |
| P4 | The number isn't on the **internal do-not-call list** or a national do-not-call register (the UAE's register, the US national list) | UAE Cabinet Resolutions 56/57 of 2024 (fines up to AED 150k), US TCPA |
| P5 | The call falls inside **calling hours in the customer's local time**. Defaults: UAE **09:00–18:00**, US 08:00–21:00. | UAE telemarketing rules, US TCPA |
| P6 | **Frequency caps**: at most 1 attempt per day and 2 per week per number | UAE rules for unanswered calls |
| P7 | Cart value ≥ the **economic threshold** (§6) and every item is **in stock** | Unit economics |
| P8 | The shop's **daily and monthly spend cap** hasn't been reached | Cost control |
| P9 | Not a test order, a staff account or a high-risk order | Fraud and noise |
| P10 | **Holdout**: 15% of eligible checkouts are deliberately never called (randomized, recorded) | Measurement |

### 5.2 During the call

- **Disclosure first**: the agent opens with "This is an AI assistant calling on behalf of {shop} about your order," which covers both identity and purpose.
- **The agent acts only through tools.** It can't state a price, stock level or discount unless a tool returned it.
- **The discount tool enforces limits on the server**: a maximum percentage per shop, one code per customer, single use, a 24-hour expiry, and only above the threshold. The LLM can ask for a discount, but the server decides.
- Any objection triggers the **opt-out tool**, which writes to the do-not-call list instantly. It is idempotent, so a repeated call can't cause harm.
- **Never collects payment details.** It sends a checkout link instead.
- Hard limits: **3 minutes per call**, and the conversation stays on topic. **Transfer to a human** when the customer asks or is upset (if the shop has a human line).
- **Voicemail**: leave a short message that complies with the rules, or hang up, depending on shop settings. Never leave a discount code on voicemail.

### 5.3 After the call

- An **LLM grader** scores every transcript against rubric R1–R8 (disclosure, no promises the tools didn't back, honored opt-out, tone, accuracy).
- If the policy-violation rate goes above **0.5% over a rolling 200 calls**, the shop's agent pauses automatically and the on-call person is paged.
- 2% of calls are sampled for human review in the admin.
- **PII**: phone numbers are encrypted with KMS keys, transcripts are redacted, recordings are kept for 30 days, and Shopify's GDPR webhooks (`customers/redact`, `shop/redact`, `customers/data_request`) are handled.

## 6. Unit economics

Cost per *connected* minute, from list prices (October 2026). Verify these before publishing.

| Component | US | UAE |
|---|---|---|
| ElevenLabs Agents | $0.080 | $0.080 |
| LLM (small or medium model) | ~$0.005–0.045 | ~$0.005–0.045 |
| Twilio outbound to mobile | ~$0.014 | **$0.30** |
| **Total per minute** | **~$0.10–0.14** | **~$0.39–0.43** |

**Cost per called cart.** Assumptions: up to 2 attempts, 60% connect, 1.5 minutes on a connected call, 0.4 minutes on a voicemail or short call.
- US: ~$0.12–0.18 per cart. UAE: ~$0.45–0.60 per cart.

**Break-even rule** used in policy P7. Call only when expected margin per call is at least 3× expected cost:

```
E[margin] = P(connect) × P(recover | connect) × incremental_factor × cart_value × gross_margin
call if E[margin] ≥ 3 × E[cost]
```

Example: UAE, 60% connect, 10% recovery, 50% of those recoveries truly incremental, 40% gross margin:
`0.6 × 0.10 × 0.5 × cart × 0.4 ≥ 3 × $0.55` gives **cart ≥ ~$140**. In the US the threshold is about **$40**.

Every number in this formula is learned per shop from its own data and updated weekly. The dashboard shows the current threshold and the reason.

## 7. Metrics

### Business (per shop, against the holdout)
| Metric | Definition | Benchmark to beat |
|---|---|---|
| **Incremental recovery rate** | recovery rate of called group − recovery rate of holdout | Proven only by the holdout |
| Revenue per eligible cart | recovered revenue ÷ eligible carts | Klaviyo email $6.77, SMS $4.48 |
| **Return on call spend** | incremental gross margin ÷ (voice + telephony + LLM cost) | ≥ 3× |
| Cost per incremental order | total cost ÷ incremental orders | |
| Connect rate | answered by a person ÷ dialled | 58–65% (vendor reported) |
| Contacted-to-order conversion | orders within 72h ÷ connected | 10–18% (vendor reported) |
| Opt-out rate / complaint rate | opt-outs ÷ connected | < 3% / < 0.1% |
| Time to first touch | p50 time from abandonment to first call | ≤ 15 minutes |

### Agent quality
| Metric | Target |
|---|---|
| Response latency, end of customer's speech to start of agent's reply (p50 / p95) | ≤ 800 ms / ≤ 1.5 s |
| Rubric pass rate (LLM grader) | ≥ 95% |
| **Hard-rule violations** (unapproved discount, no disclosure, ignored opt-out) | **0**, and any violation blocks the release |
| Successful tool-call rate | ≥ 99% |

### Reliability SLOs
| SLO | Target |
|---|---|
| Webhook acknowledged in < 1 s (Shopify's timeout is 5 s) | 99.9% |
| Workflow started within 60 s of the webhook | 99.5% |
| Calls placed outside calling hours or to a do-not-call number | **0**, with an immediate page if it happens |
| Tool API availability | 99.9% |

## 8. Unexpected events, by design

Each event below has an automated test (unit tests, Temporal replay tests or end-to-end tests) and a runbook.

| Event | Handling |
|---|---|
| Customer buys while the call is queued or in progress | The `order_created` signal cancels the workflow. During a call, the `get_cart` tool returns "already purchased" and the agent ends the call politely. |
| Shopify sends the same webhook twice or out of order | Idempotency key = shop + checkout ID + update time. The workflow ID is deterministic. |
| Stock or price changed | `check_stock` and `get_cart` read live data. The agent never relies on data saved at checkout time. |
| Voicemail or no answer | Detect voicemail; retry after back-off within the frequency caps; then WhatsApp or SMS. |
| Wrong number or not the buyer | Do-not-call this number for this shop, and close the attempt. |
| Opt-out or abusive caller | Opt-out tool; the call ends; the do-not-call list syncs to all of the shop's workflows. |
| ElevenLabs or Twilio outage | Circuit breaker, then the messaging fallback. The workflow pauses rather than dropping the checkout. |
| Shopify rate limit (429) | Throttling aware of Shopify's GraphQL query costs, with exponential back-off and jitter. |
| Black Friday/Cyber Monday spike | Concurrency limit per shop and per ElevenLabs plan. Optionally avoid ElevenLabs' 2× overflow pricing. Queue the excess. |
| App uninstalled | `app/uninstalled` cancels all workflows and deletes access tokens. |
| Model regression after a prompt change | The CI evaluation gate. Canary to 10% of shops, with automatic rollback if the rubric pass rate drops. |

## 9. Evaluation harness

Full detail is in [OBSERVABILITY_AND_EVALS.md](OBSERVABILITY_AND_EVALS.md). Summary:

- `evals/scenarios/*.yaml` defines simulated customers: busy, confused, hostile, price shopper, switches to Arabic, already bought, asks for a discount, "remove me".
- Runs through ElevenLabs' agent simulation API plus our tool stubs. The LLM grader scores each transcript against the rubric.
- **CI gate**: hard rules must pass 100%, and the rubric must pass at least 95%. Results are published as a pull-request comment.

## 10. Milestones

1. **M1, core loop (week 1):** a Shopify dev store, webhook ingestion, the Temporal workflow, policy checks P1–P10, one outbound call with `get_cart` and `send_checkout_link`, Docker Compose locally.
2. **M2, safety and economics (week 2):** the discount tool with limits, opt-out and do-not-call, calling hours by timezone, holdout, cost tracking, the admin dashboard.
3. **M3, production (week 3):** Terraform on AWS me-central-1, CI/CD with the evaluation gate, OpenTelemetry and Grafana, SLO alerts, runbooks, a k6 load test.
4. **M4, real environment (week 4+):** a pilot with a real merchant, Arabic, a WhatsApp fallback, and a public write-up with real metrics and incidents.

## 11. Sources

- Baymard cart abandonment (via [ReadyCloud](https://readycloud.com/info/cart-abandonment-rates-2026-numbers-reasons-what-ecommerce-brands-should-do-next))
- [Klaviyo abandoned cart benchmarks 2026](https://www.klaviyo.com/blog/abandoned-cart-benchmarks)
- [ElevenLabs Agents pricing](https://elevenlabs.io/pricing/agents)
- [ElevenLabs burst pricing](https://elevenlabs.io/docs/agents-platform/guides/burst-pricing)
- [ElevenLabs batch calling](https://elevenlabs.io/docs/agents-platform/phone-numbers/batch-calls)
- [Twilio voice pricing, UAE](https://www.twilio.com/en-us/voice/pricing/ae)
- [Twilio voice pricing, Saudi Arabia](https://www.twilio.com/en-us/voice/pricing/sa)
- [GoKwik AI calling data](https://stellagent.ai/insights/gokwik-ai-calling-whatsapp-cart-recovery)
- [BiteSpeed Off Duty case study](https://files.bitespeed.co/Off_Duty.pdf)
- [LimeChat BeastLife case study](https://limechat.ai/case-study/beastlife-unlocks-92x-roi-using-voice-ai-for-cart-recovery)
- [UAE telemarketing rules (Khaleej Times)](https://www.khaleejtimes.com/uae/new-telemarketing-rules-in-uae-timings-fines-exemptions-explained)
- [UAE telemarketing rules (Legal500)](https://www.legal500.com/developments/thought-leadership/new-telemarketing-rules-and-regulations-in-the-uae/)
- [Dubai enforcement (AGBI)](https://www.agbi.com/telecoms/2025/02/dubai-fines-companies-millions-for-telemarketing-violations)
