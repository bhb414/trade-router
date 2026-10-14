# Provider adapters

Adapters implement `trade_router.adapters.base.ProviderAdapterV1` and publish their
features with `BrokerCapabilities`. Unsupported features must be absent from the
capability set and raise `UNSUPPORTED_CAPABILITY` before a provider request is sent.

Third-party packages register adapters through this entry point group:

```toml
[project.entry-points."trade_router.providers"]
mybroker = "mybroker.adapter:MyBrokerAdapter"
```

An adapter receives an `AdapterContext` containing public profile metadata, the
decrypted current-user credential object, a token-persistence callback, the shared
rate-limiter pool, and the current connection generation. It must:

- return only normalized models;
- use `Decimal` for prices and amounts;
- return timezone-aware Asia/Seoul timestamps;
- keep provider-only fields in its validated provider option namespace;
- retry read requests only within a small bound;
- never automatically retry an order whose acceptance is uncertain;
- avoid logging credentials, raw account numbers, bearer tokens, and raw request
  bodies.

The built-in adapter contract is checked for `fake`, `kis`, and `namuh`. The fake
adapter supports deterministic prices and the `fill`, `ack`, `partial`, `reject`,
and `uncertain` submission behaviors.
