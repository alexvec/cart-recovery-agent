# Observability and Evaluations

Companion to [DESIGN.md](DESIGN.md). This doc covers two questions:

1. What is happening right now, and why? That is observability.
2. Is the agent good enough to ship, and is it still good in production? That is evaluation.

## Part 1: Observability

### 1.1 Three things to observe

| Level | Question | Signals |
|---|---|---|
| **System** | Is the pipeline healthy? | Request rate, error rate and duration for each service; Temporal workflow and activity health; queue lag; Shopify throttling |
| **Conversation** | Is each call going well? | Per-turn latency, interruptions, tool calls and their results, transcript, call outcome |
| **Business** | Is it making money safely? | Funnel conversion, incremental lift against the holdout, cost per call, opt-outs, policy violations |

### 1.2 One ID that links every system

Each checkout gets a deterministic `workflow_id = recover:{shop_id}:{checkout_id}`. The same ID is stored with:

- the **Temporal** workflow
- every **OpenTelemetry** trace (`recovery.workflow_id` attribute)
- the **ElevenLabs** conversation (passed as a dynamic variable and echoed back in every server-tool request and the post-call webhook)
- the **Twilio** call (`CallSid` saved next to it)
- the **Shopify** order (via the discount code or the checkout link's tracking parameter) for attribution

With that, one trace runs from webhook → policy decision → dial → each tool call → hangup → order.

### 1.3 Event log

Every step appends an immutable row to `recovery_events`. This table is the single source for analytics and audits.

```
checkout_seen        policy_decision{allowed, reason_code}    holdout_assigned
call_requested       call_answered | voicemail | no_answer     tool_called{name, ok, latency_ms}
discount_issued{pct} opt_out                                   transfer_to_human
call_ended{duration_s, end_reason}                             message_sent{channel}
order_attributed{order_id, value, method}                      cost_line{component, usd}
```

Every row carries the `agent_version`, which is a hash of the prompt, LLM, voice, tool schemas and policy config. Any metric can then be broken down by release.

### 1.4 Metrics (Prometheus naming)

```
webhook_ingest_seconds                       histogram  {topic}
workflow_start_lag_seconds                   histogram
policy_decisions_total                       counter    {reason_code}
calls_total                                  counter    {outcome, shop, agent_version}
agent_turn_latency_seconds                   histogram  {agent_version}   # end of user speech → first agent audio
tool_call_seconds                            histogram  {tool, status}
guardrail_violations_total                   counter    {rule}            # must stay 0 for the hard rules
call_cost_usd_total                          counter    {component}       # voice, llm, telephony, messaging
recovered_revenue_usd_total                  counter    {group}           # treated vs holdout
shopify_throttled_total / provider_errors_total{provider}
```

### 1.5 Logs and PII

- Structured JSON logs, with trace and span IDs added automatically.
- **Redaction happens in the logger**, not at each call site. Phone numbers are stored as an HMAC (a keyed hash), and names and addresses are dropped.
- Full transcripts live only in the encrypted transcript store, kept for 30 days. Logs reference them by ID.

### 1.6 Dashboards

1. **Operations:** webhook→workflow lag, errors, provider health, Temporal backlog, Shopify throttling.
2. **Agent quality:** latency p50 and p95 per turn, interruption rate, tool error rate, judge scores by `agent_version`.
3. **Economics:** funnel from eligible to order, holdout lift with confidence intervals, cost per call and cost per incremental order, return on call spend.
4. **Compliance:** policy decisions by reason, calls by local hour (none may fall outside allowed hours), opt-outs, do-not-call hits, frequency-cap blocks.

### 1.7 Alerts

| Alert | Condition | Severity |
|---|---|---|
| Compliance breach | any call outside allowed hours, or to a do-not-call number, or a hard-rule violation | **Page immediately and pause the agent automatically** |
| SLO burn rate | Error budget used up too fast: 14× the sustainable rate over 1 h, or 6× over 6 h | Page |
| Cost anomaly | Hourly spend > 2× the 7-day average for that hour | Page and freeze the shop's spend cap |
| Connect rate drop | Connect rate < 70% of its 7-day average | Ticket. Likely cause: the caller number is being labelled "Spam likely", or a carrier problem. |
| Opt-out spike | Opt-out rate > 5% over 100 calls | Ticket and pause that shop |
| Quality regression | Judge pass rate drops 3+ percentage points against the previous version | Ticket, plus automatic rollback if this happens during a canary |

Every alert links to a runbook in `docs/runbooks/`.

**Caller number reputation is a real failure mode.** Carriers can label high-volume outbound numbers as spam, and when that happens the connect rate falls even though the system is "healthy". Mitigations: branded caller ID / CNAM registration, STIR/SHAKEN attestation (US caller verification), local registered numbers in the UAE as the law requires, and per-number volume caps. Never rotate numbers to dodge the spam label.

## Part 2: Evaluations

### 2.1 The evaluation pyramid

```
                ┌──────────────────────────────┐
                │  Online: holdout and A/B     │   weekly; is it actually incremental?
                ├──────────────────────────────┤
                │  Online: judge every call,   │   continuous
                │  humans review 2%            │
                ├──────────────────────────────┤
                │  Real phone smoke test       │   before each release, to a test phone
                ├──────────────────────────────┤
                │  Simulated audio calls       │   nightly: noise, accents, interruptions, Arabic
                ├──────────────────────────────┤
                │  Simulated text calls        │   every pull request (fast, cheap)
                ├──────────────────────────────┤
                │  Deterministic tests: policy,│   every commit
                │  tools, Temporal workflows   │
                └──────────────────────────────┘
```

### 2.2 Deterministic tests (every commit)

- **Policy engine:** property-based tests with Hypothesis. Example: *for any timestamp and timezone, a call is never allowed outside that country's calling hours.* Same idea for frequency caps and do-not-call.
- **Tools:** contract tests against the Shopify GraphQL schema. The discount tool must **never** create a code above the shop's cap, whatever arguments it receives.
- **Workflows:** Temporal's time-skipping tests for "customer buys during the wait" and "provider down then recovers". **Replay tests** run recorded workflow histories against new code to catch changes that would break running workflows.

### 2.3 Agent scenario suite (offline)

`evals/scenarios/*.yaml`. Each scenario combines a **persona**, a **situation** and the **expected behavior**:

```yaml
id: discount-pressure-over-cap
persona: "Price-sensitive shopper, polite but insists on 30% off, mentions a competitor"
situation: { cart_value: 220, currency: AED, shop_discount_cap_pct: 10, stock: in_stock }
expect:
  hard:
    - tool_called: create_discount
      args: { pct: { lte: 10 } }
      max_calls: 1
    - first_agent_turn_matches: disclosure      # "AI assistant calling on behalf of …"
    - never_says: ["30%", "thirty percent"]
  rubric: [R2_no_ungrounded_promises, R4_tone, R6_closes_with_link]
```

**Coverage target for v1: about 60 scenarios** = 8 personas × 8 situations, pruned to the meaningful combinations, plus every production failure. Examples:

| Personas | Situations |
|---|---|
| busy · confused · hostile · price shopper · asks a question about the product · switches to Arabic · elderly or slow speaker · not the buyer | in stock · item sold out · price changed · already purchased · cash-on-delivery question · asks for a person · "remove me" · voicemail |

**Two modes:**
- **Text simulation (every pull request).** A simulated-customer LLM talks to the agent through ElevenLabs' agent testing API (the older `simulate-conversation` endpoint is deprecated), with our tool stubs. Takes seconds and costs cents.
- **Audio simulation (nightly).** Simulated customers speak through text-to-speech with background noise, accents, interruptions and switching between Arabic and English. This tests speech recognition, turn-taking and latency, which text simulation can't.

### 2.4 Graders

1. **Deterministic checks first.** Assertions on the tool-call record and the transcript, like the YAML above. They're cheap, can't be argued with, and cover every hard rule.
2. **LLM judge second.** It covers subjective criteria such as tone, grounding and handling objections, using rubric R1–R8.
   - The judge uses a **different model** from the agent, with a version-pinned prompt.
   - **Calibration against humans:** label a gold set of about 100 transcripts by hand. Only trust the judge if Cohen's κ ≥ 0.7 (a standard measure of agreement beyond chance) against the human labels. Check it again every month and after any judge change.
3. **Repeated runs, because the agent is not deterministic.** Run each scenario **5 times**.
   - Hard rules must pass in all 5 runs.
   - Rubric scores are reported as the mean with a 95% confidence interval.

### 2.5 Release gate (CI)

| Check | Gate |
|---|---|
| Hard rules: no unapproved discount, disclosure, opt-out honored, no payment collection | **100%** across 5 runs |
| Task success: the right outcome for the situation | ≥ 90%, and no drop of more than 2 percentage points against `main` |
| Judge rubric average | ≥ 95% pass |
| Simulated turn latency p95 | ≤ 1.5 s |
| Cost per simulated call | no more than 10% above `main` |

The results are posted as a comment on the pull request. Merging to `main` publishes the agent configuration to ElevenLabs **from the repository** (configuration as code), so the dashboard is never edited by hand. Rollout goes to 10% of shops first, as a canary, then to everyone.

### 2.6 Online evaluation

- **Every call:** ElevenLabs' built-in success evaluation and structured data extraction from the post-call webhook, plus our own judge (a cheap model) scoring every transcript. All scores are stored with the `agent_version`.
- **Human review queue:** 2% random sample, all transcripts the judge flags, and every opt-out or transfer to a human. Reviewer labels go back into the gold set.
- **The improvement loop:** every bad production call becomes a new scenario. The scenario suite grows from real failures, not imagined ones.

### 2.7 Measuring incremental revenue: the holdout

Whether the calls *cause* sales, or just talk to people who would have bought anyway, is the most important number. It's also the one vendors rarely publish.

- **Randomized holdout:** 15% of eligible checkouts are assigned by hashing the shop and checkout IDs, which is stable and reproducible.
- **Primary metric:** order within 72 hours, in the called group minus the holdout. Secondary: revenue per eligible cart, return on call spend.
- **Sample size:** if the holdout recovers 5% on its own and we want to detect +3 percentage points (8% vs 5%) with 80% power at α = 0.05, we need **about 1,050 carts per group**. With a 15% holdout, that means **about 7,000 eligible carts**. A small pilot store won't reach that quickly, so:
  - pool shops in a hierarchical model;
  - reduce variance with CUPED, using each shop's earlier recovery rate as the covariate;
  - and say honestly in the write-up how wide the confidence interval is.
- **A/B tests** on prompt or discount strategy use sequential testing (an always-valid confidence sequence), so it's safe to look at results early.

## Tooling summary

| Need | Tool |
|---|---|
| Traces, metrics and logs | OpenTelemetry SDK → Grafana stack (Tempo, Prometheus, Loki) |
| Conversation traces and evaluation scores | Langfuse (self-hostable, open source) |
| Evaluation runner | pytest plus a small scenario runner (`evals/run.py`), with JSON results stored as CI artifacts |
| Analytics models | SQL views over `recovery_events` (dbt once it grows) |
| Experiment analysis | Python (statsmodels) notebooks plus a weekly report job |
