# cart-recovery-agent

An AI voice agent that calls shoppers who abandoned a Shopify checkout. It is built the way a production system would be: deterministic guardrails outside the LLM, durable workflows, unit economics, and a randomized holdout to prove the revenue is incremental.

- **Design:** [docs/DESIGN.md](docs/DESIGN.md) covers the architecture, guardrails, cost model and metrics.
- **Observability and evaluations:** [docs/OBSERVABILITY_AND_EVALS.md](docs/OBSERVABILITY_AND_EVALS.md)

## How it works

```
Shopify webhook ─► API (verify HMAC, dedupe, persist) ─► Temporal RecoveryWorkflow (one per checkout)
                                                           1. wait until the checkout is quiet
                                                           2. policy: consent, do-not-call, calling hours,
                                                              frequency cap, break-even value, budget, holdout
                                                           3. call via ElevenLabs Agents + Twilio
                                                           4. retry after no answer (within caps)
                                                           5. attribute any order within 72h, called or not
                         order webhook ─► signal ─► cancels the call if the customer already bought
```

## Run locally

```bash
cp .env.example .env            # fill in values; see comments
docker compose up -d --build    # postgres, temporal (UI on :8233), migrations, api (:8090), worker
uv run python scripts/send_test_webhook.py checkout --token demo1
uv run python scripts/send_test_webhook.py order --token demo1
```

With `DIALER=fake` no phone rings. Set `DIALER=elevenlabs` once the ElevenLabs agent and Twilio number are configured.

Ports: Postgres on 5433 and the API on 8090, so they don't clash with local services.

## Shopify app

`shopify-app/` is the official Shopify app template (React Router, TypeScript). It owns OAuth, the embedded admin and
the app configuration in `shopify-app/shopify.app.toml`. Recovery webhooks go straight to the Python API.

```bash
cloudflared tunnel --url http://localhost:8090     # dev: public URL for Shopify and ElevenLabs
cd shopify-app && shopify app deploy               # after updating the webhook URIs to the tunnel URL
uv run python scripts/shopify/seed_products.py --store <store>.myshopify.com
```

## Voice agent (configuration as code)

The agent lives on ElevenLabs but is defined here: `agent/prompt.md`, `agent/agent.json`, `agent/tools.json`.

```bash
uv run python scripts/sync_agent.py --base-url https://<public-host>
```

The sync stores the tool secret in ElevenLabs' secret store, registers the post-call webhook, upserts the tools and the
agent, and writes the agent id and webhook signing secret to `.env`. Never edit the agent in the dashboard.

## Evaluations

`evals/scenarios.json` holds simulated customers (busy, hostile, wrong number, discount pressure, card over the phone,
Arabic, already purchased...). Each has expected behavior plus hard rules that apply to every scenario. Tools are
mocked, so the suite is hermetic.

```bash
uv run python scripts/run_evals.py --repeat 3      # exits non-zero if any run fails
```

## Develop

```bash
uv sync
uv run pytest        # policy property tests, webhook parsing, Temporal workflows on skipped time
uv run ruff check . && uv run mypy
```

## Status

- [x] M1 core loop: webhooks, policy engine, durable workflow, fake dialer, attribution
- [x] M1 agent: ElevenLabs agent as code, 9-scenario simulation suite (27/27 runs passing)
- [x] M1 Shopify: protected customer data approved, app installed on the dev store
- [ ] M1 real calls: Twilio number imported into ElevenLabs
- [ ] M2 discounts with server-enforced limits, SMS/WhatsApp link delivery, live stock checks, dashboard
- [ ] M3 Terraform on AWS me-central-1, OpenTelemetry, SLO alerts, evaluation gate in CI
- [ ] M4 merchant pilot, Arabic, write-up with real metrics
