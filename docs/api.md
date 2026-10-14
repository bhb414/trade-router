# Local API

The daemon listens on `127.0.0.1:8877` by default. Every `/v1` HTTP request uses:

```text
Authorization: Bearer <installation token>
```

The token is generated once and encrypted in the Windows current-user DPAPI vault.
The bundled CLI and SDK read it directly for the same Windows user. A client under
a different user must receive the token explicitly through `TRADE_ROUTER_TOKEN`.

Main endpoints:

| Service | Endpoints |
|---|---|
| system | `/v1/system/health`, `/capabilities`, `/connections`, `/rate-limits`, `/reconcile`, `/shutdown` |
| profiles | list, add, remove, connection check |
| accounts | list, add, arm/disarm, balance, positions, orders |
| strategies | list, register, show, arm/disarm |
| instruments | search |
| market | quote, order book, bars |
| orders | preview, submit, amend, cancel |
| discovery | rankings, condition list/run when supported |
| stream | `/v1/stream` WebSocket |

Profiles store credential `usage` (`live`, `paper`, or `shared`). Account requests
use the environment saved on the account. Market, discovery, profile-check, and
stream requests accept an `environment=live|paper` parameter. It is optional for
single-use profiles and required for `shared` profiles.

For a WebSocket, put the installation token in the `token` query parameter or the
Authorization header. The first client message is:

```json
{
  "profile_id": "kis-main",
  "environment": "live",
  "subscriptions": [
    {"kind": "QUOTE", "code": "005930", "venue": "INTEGRATED"}
  ]
}
```

Use `/docs` on the running daemon for the generated OpenAPI schema.
