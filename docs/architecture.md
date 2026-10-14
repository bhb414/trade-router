# Architecture

Trade Router is one local daemon with versioned HTTP and WebSocket APIs. Broker
credentials, provider OAuth tokens, request pacing, shared subscriptions, account
snapshots, risk checks, and the order ledger stay in the daemon. Strategy processes
use only registered aliases.

```text
strategy process ─┐
strategy process ─┼─ HTTP /v1 + WebSocket ─ daemon ─ ProviderAdapterV1 ─ KIS
CLI / diagnostics ┘                         │          ├─────────────── NAMUH
                                            │          └─────────────── fake
                                            ├─ SQLite WAL (non-secrets)
                                            └─ Windows DPAPI (secrets/tokens)
```

The order path is fixed:

```text
provider preview → account limits → strategy limits → idempotency reservation
→ provider submit → acknowledgement → durable ledger
```

`client_order_id` is unique within a strategy. A repeated request returns the
existing result. Network loss or a provider 5xx after submission changes the order
to `UNKNOWN_RECONCILING`; the daemon closes its new-order gate and never resends it.
The gate opens after all unresolved orders have been reconciled.

Provider market-data subscriptions are reference counted per connection profile.
Multiple SDK clients that request the same subscription share one provider
registration. Reconnects increment the connection generation so consumers can
discard events from an earlier connection.

The first release intentionally does not run strategy code. A later process runner
can use the same `StrategyConfig` and daemon without changing provider adapters or
the public SDK.
