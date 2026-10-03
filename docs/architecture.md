# Architecture

## Zones

- **Untrusted AI zone** (`intelligence/`): the parser turns speech into a `Proposal` (pydantic
  schema; one retry, then fail closed). The quarantined planner is the only code that reads
  merchant text and can only output `(sku, qty)` from one merchant. Neither can touch payments,
  credentials or prices.
- **Trusted zone** (`core/`, `apps/api/services/`): the catalog re-prices every line, the policy
  engine decides, the saga moves money. `core/` is pure and unit-tested.

## Data model

The source of truth is `events`: one hash-chained, append-only log per household
(`hash = sha256(prev_hash ‖ canonical_json(body))`, UPDATE/DELETE blocked by a trigger).
Projections (`purchases`, `reservations`, `mandate_versions`, `approvals`, `outbox`, …) are
written in the **same transaction** as their events.

Every write path starts with `SELECT … FROM households WHERE id = ? FOR UPDATE`. That row lock
is the per-household serialization point: two members ordering in the same second are
serialized, and the second one sees the first one's hold. The household id is the natural shard
key.

`mandate_versions` is bitemporal: `[valid_from, valid_to)` is when a rule applied,
`[recorded_from, recorded_to)` is when Mandate believed it. A new version closes the old row's
recorded time and re-records it with its valid time closed.

## Payment saga

```
REQUESTED → CHECKED → RESERVED → ORDER_CREATED → CAPTURED → CONFIRMED → (PARTIALLY_)REFUNDED
     ↘ AWAITING_CONFIRMATION (anomaly, unknown speaker, "the usual", price changed)
     ↘ ESCALATED (out of scope / over limit) → owner passkey → CHECKED
     ↘ DENIED (frozen, paused, blocked)          RESERVED/ORDER_CREATED → EXPIRED (hold timeout)
```

1. The device mints `purchase_id` (ULID) before sending; a replay is a no-op.
2. Policy allows → a 15-minute budget hold **and** an outbox row are committed together.
3. The worker leases the outbox row (`FOR UPDATE SKIP LOCKED`), quotes the merchant's final
   total (fees included; a higher price goes back to the user), then calls PayPal with
   `PayPal-Request-Id = sha256(purchase_id:step)`.
4. Timeout → `UNKNOWN`, retried with the **same** key (PayPal returns the original result).
   5xx → retried with backoff; three failures open a circuit breaker.
5. Capture converts the hold into spend. The webhook (deduped by event id, monotonic) or the
   reconciler (reads the order) confirms. Alexa announces it.

| Cut point | Recovery | Test |
|---|---|---|
| Device → cloud | replay with same `purchase_id` | `test_device_offline_replay_lands_once` |
| Cloud → PayPal, response lost | same request id; reconciler | `test_wifi_cut_after_capture_no_double_charge`, `…_create_order` |
| Webhook delayed / lost / duplicated | reconciler; event-id dedupe | `test_webhook_lost_reconciler_confirms`, `test_duplicate_webhook_applied_once` |
| Crash between DB write and PayPal call | outbox row picked up later | `test_crash_between_db_write_and_paypal_call` |
| PayPal down | queue, backoff, breaker | `test_paypal_down_queues_retries_and_recovers` |
| Long outage | hold expires → EXPIRED, budget released | `test_long_outage_hold_expires_and_budget_released` |

A capture that lands after its hold expired is refunded automatically.

## Memory

- **Explain:** each purchase links to the exact mandate version and its events.
- **What-if:** replays a month of confirmed purchases under a proposed limit.
- **Rhythm:** per-item burn rate (days between orders ÷ quantity, EWMA α=0.5, household-size prior
  until 3 orders, spikes ignored), paused items and absent members slow it, "we're out" / "plenty"
  corrects it. A forecast only reminds; with `auto_reorder` it files a normal purchase request.
