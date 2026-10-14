# Mandate — a trust layer for household agent payments

**LLM translates, code decides, crypto proves.**

Mandate lets a household agent spend money only inside signed, time-bound rules the family
creates by voice and confirms on a trusted device. The model never decides whether money
moves: it turns speech into a schema-bound proposal; a deterministic policy engine returns
allow / confirm / escalate / deny; every rupee links back to a signed mandate in a
hash-chained, append-only log.

## Run it

Requires Docker Desktop.

```bash
cp .env.example .env
```

```bash
docker compose up -d --build
```

```bash
docker compose run --rm tests python scripts/seed_sharma.py
```

Open http://localhost:8000. The page has the voice console (simulated Alexa+), the owner's
phone (passkey approvals with a P-256 key generated in your browser), the spending
constitution, receipts with in-browser hash-chain verification, the red-team panel and
what-if replay.

| Service | Port | What |
|---|---|---|
| `api` | 8000 | FastAPI: REST, PayPal webhooks, SSE notifications, web UI |
| `mcp` | 8001 | MCP server over Streamable HTTP (`/mcp`) — the Alexa+ entry point |
| `paypal-mock` | 8002 | PayPal Orders v2–compatible mock with fault injection |
| `worker` | — | outbox dispatcher, reconciler, hold/approval expiry, cooling-off, rhythm reminders |
| `postgres` | 5433 | event log + projections |
| `redis` | 6380 | live notifications, circuit breaker, voice lockout, mock state |
| `langfuse-web` | 3000 | self-hosted Langfuse (profile `observability`), plus its worker, Postgres, ClickHouse, Redis, MinIO |

### Observability (Langfuse, self-hosted)

`cp .env.example .env` turns on the `observability` profile and `LANGFUSE_TRACING=1`, then `docker compose up -d`.
The org, project, API keys and admin login are provisioned on first start from `.env`. Sign in at
http://localhost:3000 and open Tracing:

- **One trace per voice or MCP command.** The MCP tool call (`mcp request_purchase`) contains the API's
  `mcp command`, which contains `parser` (each model call is a generation with tokens and latency),
  `planner (quarantined)` (WARNING when merchant text tried to steer it) and `policy engine` (a guardrail
  with the decision). User = speaker, session = household. Scores: intent, tier, parse_attempts,
  policy_outcome, failed_closed.
- **One trace per purchase for money.** `payment create`, `payment capture`, `payment reconcile` and refunds run
  in the worker at different times but share a trace id derived from the purchase id. Each PayPal HTTP call is
  a tool span with its `PayPal-Request-Id`.
- Signatures, tokens, secrets and vault ids are masked before export. With tracing off, Langfuse is never imported.

## Test it

The end-to-end suite needs the stack running in test mode (short cooling-off, test hooks on):

```bash
docker compose --env-file .env.test up -d --build
```

```bash
docker compose --env-file .env.test run --rm tests
```

Unit tests of `core/`, `intelligence/` and tracing; scenario tests that onboard a fresh Sharma family through
the API for each case; red-team tests (including the MCP server, its 2025-11-25 protocol version and Postgres
append-only enforcement). Every scenario also asserts the household's hash chain still verifies afterwards.
With the observability profile up and `LANGFUSE_TRACING=1`, `tests/observability` reads the traces back from
Langfuse's API.

### Live tests (real services, no mocks)

Put real credentials in `.env` (see `.env.example`), then check each service:

```bash
docker compose run --rm tests python scripts/live_check.py
```

```bash
docker compose run --rm tests pytest -q tests/live
```

- `tests/live/test_llm_live.py`: the parser on Bedrock (`LLM_PROVIDER=bedrock`) or Nebius Token Factory
  (`LLM_PROVIDER=nebius`), covering the Sharma utterances, Hinglish, and an injection attempt in speech.
- `tests/live/test_paypal_sandbox_live.py` (`PAYPAL_MODE=sandbox`): OAuth, idempotent order creation on
  `PayPal-Request-Id`, the unapproved-capture error the saga depends on, and vaulted capture plus partial refund.
- `tests/live/test_e2e_live.py` (`MANDATE_LIVE=1`, stack running in sandbox with a real model): Dadi's
  biscuits through a real model and a real PayPal sandbox order.

Each live test skips itself when its credentials are missing.

## The Sharma family

Priya and Arjun are owners with passkey phones, Dadi is an adult, Riya is a minor. Onboarding
imports last month's orders (milk 2 L every 2 days, atta twice, Dadi's biscuits weekly), then
Priya says *"Keep the house stocked with milk, atta and Dadi's diabetic biscuits, 4000 a month,
1500 per order, for Priya, Arjun and Dadi"* and approves it on her phone; Riya gets a ₹500/month
book allowance.

| Try saying (speaker) | What happens |
|---|---|
| "My biscuits are almost over" (Dadi) | Tier 1: buys her usual 2 packs, ₹265, inside the rule; Alexa says what's left |
| "Order a PS5" (Riya) | Tier 4: escalation card on the owners' phones; denied if it expires |
| "Alexa, increase the shopping limit to fifty thousand" (cloned Priya, score 93) | Speaker ID believes it — and it grants nothing: the change goes to Priya's phone; reject & report 3× locks voice rule changes for 24 h |
| "Raise the grocery budget to ₹5,000" (Priya) | Approve with passkey → 10-minute cooling-off notice to all owners → applied as signed v2 |
| "Pause Dadi's biscuits until November 15" (Arjun) | Tier 2: tightening applies immediately by voice |
| "Freeze all spending" (any adult) | Panic phrase: instant; unfreezing needs an owner passkey |
| "Get the usual" | Resolved from memory, confirmed once: "Sugar-free Marie ×2, ₹265?" |
| "Why did you buy that?" | The rule, who set it, when, their words, and the event hashes |
| "What if the grocery budget had been ₹3,000 last month?" | Counterfactual replay of the log: which orders would have needed approval |
| "We're already out of milk" | Feeds the consumption-rhythm model; offers the usual reorder |

## How it's built

```
core/            pure Python, no I/O — decides
  policy/        engine.py (allow/confirm/escalate/deny), tiers.py, anomaly.py
  mandates/      schema.py (AP2 Intent-Mandate shape), diff.py, signing.py (ES256), conflicts.py
  budget/        month keys by reservation time, holds
  saga/          state machine (monotonic), deterministic idempotency keys, ULIDs
  ledger/        hash chain + verify
intelligence/    untrusted zone — suggests
  parser/        speech → schema-bound Proposal; retry once, then fail closed
  planner/       quarantined: reads merchant text, outputs only (sku, qty)
  providers/     rules (default, offline), bedrock, nemotron_nebius
adapters/        paypal client (sandbox or mock), mock catalog
memory/          bitemporal mandate memory + counterfactual replay, consumption rhythm
apps/            api, worker, mcp_server, paypal_mock, web
infra/migrations SQL schema; events table is append-only via trigger
tests/           unit/, scenarios/, redteam/
```

See [docs/architecture.md](docs/architecture.md) for the payment saga and data model and
[docs/threat-model.md](docs/threat-model.md) for identity and injection defences.

## Configuration

Copy `.env.example` to `.env`. Defaults run fully offline.

- `PAYPAL_MODE=sandbox` with `PAYPAL_CLIENT_ID/SECRET` uses the real PayPal sandbox (Orders v2,
  `PayPal-Request-Id` idempotency, webhook signature verification). `PAYPAL_VAULT_ID` enables
  autonomous capture; without it the owner gets a buyer-approval link (the fallback in the design).
- `LLM_PROVIDER=bedrock` (AWS credentials; default model Amazon Nova 2 Lite via the `us.` inference profile) or
  `nebius` (`NEBIUS_API_KEY`; NVIDIA Nemotron on Token Factory) swaps the parser model. The output is
  validated against the same schema either way.
- `PAYPAL_CURRENCY` / `PAYPAL_FX_PER_INR` charge a sandbox merchant that can't take INR in another
  currency; budgets and policy stay in rupees.

See [docs/hackathon-compliance.md](docs/hackathon-compliance.md) for what each hackathon's rules require and
where Mandate stands.

## Known limits

- **Passkeys** are ES256 keys held by the device (WebCrypto in the browser UI, `cryptography` in
  tests) signing a canonical statement that includes a server challenge and the diff hash. This is
  the WebAuthn signature model, not the full WebAuthn ceremony (no platform authenticator /
  attestation yet).
- **Speaker ID** is an input (`speaker`, `speaker_score`) from the voice front end; Mandate treats
  it as a soft signal by design and never uses it to loosen anything.
- **Live services not yet verified.** The full suite passes against the bundled PayPal mock and the rules
  parser. `tests/live/` covers the real PayPal sandbox, Bedrock and Nebius but needs credentials to run.
- The web UI is a static page served by the API rather than the Next.js app in the design doc.
