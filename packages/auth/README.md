# Eye Auth

Leaf workspace package for the OAuth issuer and A2A trust library. It imports
no application packages. **Application and Compose integration is not yet
implemented:** installing this package alone does not protect existing agents
or replace the existing SellerOS service credential.

## Issuer

Run `uv run eye-auth` from the repository root. It binds to `127.0.0.1:8500` by
default; `EYE_BIND_HOST` changes the bind address. Routes:

- `POST /oauth/token`: Authlib client-credentials grant, HTTP Basic client
  authentication, explicit scope required. RS256 RFC 9068 access tokens last
  300 seconds and include `iss`, `aud`, `scope`, `sub`, `client_id`, `iat`,
  `exp`, and a unique `jti`. No refresh tokens are issued.
- `GET /.well-known/jwks.json`: public signing keys only.
- `POST /cards/sign`: bearer-authenticated with audience `auth` and scope
  `auth:card`; signs an A2A 1.0 card only when its name exactly matches the
  calling client and that client is in `card_clients`.
- `GET /healthz`: public health check.

`EYE_AUTH_URL` is the pinned issuer URL (default `http://127.0.0.1:8500`). It
must agree across issuer and consumers. `EYE_AUTH_CONFIG` defaults to
`config/auth.yaml`; clients' allowed scopes and secret **environment variable
names**, never secret values, live there. Unset secrets disable authentication
for that client. `EYE_AUTH_KEY_PATH` defaults to `data/auth/key.pem`; a 0600
RSA private key is generated on first start, and reused on subsequent starts.
Mount its directory on persistent storage in a deployment.

Scopes use `<agent>:<skill_id>`. `selleros:revert` targets audience `eye-core`;
other scopes target the agent before `:`. A token request can contain several
scopes for one audience, but cannot combine audiences. Client scope assignments
in `config/auth.yaml` are prepared for the current direct call sites.

Authlib requires HTTPS except for loopback URLs. A local Compose deployment
using HTTP between services must explicitly set `AUTHLIB_INSECURE_TRANSPORT=1`
on the issuer. Production deployments should terminate TLS appropriately.

## Consumer library

`eye_auth.client.oauth_client(scopes)` yields Authlib's `AsyncOAuth2Client`;
`sync_client(scopes)` yields its synchronous equivalent. Both fetch an initial
token and automatically reissue client-credentials tokens when expired. Keep
the context alive while making requests to reuse the token. The caller sets
`EYE_AUTH_CLIENT_ID` and `EYE_AUTH_CLIENT_SECRET`, or uses the identity-specific
variable `EYE_AUTH_<UPPERCASE_CLIENT_ID_WITH_UNDERSCORES>_SECRET`.

`require_scopes(request, audience, scopes)` is an async FastAPI-compatible
verification dependency; `A2AAuthMiddleware` applies it to a mapping of RPC
paths to audiences and required scope sets. Missing, invalid, expired,
wrong-issuer, and wrong-audience tokens are 401; missing required scopes are
403. Health and card routes remain public when not in that mapping.

`sign_card(card, client_id)` enrolls a card through the issuer and verifies the
returned signature before mutating the caller's card. Call it during service
startup. `verify_card(card, expected_name)` verifies SDK signatures and the
expected agent identity before routing. Unsigned and tampered cards are
rejected. Verification caches JWKS and refetches on an unknown `kid`. The
card's `jku` is never followed; only `EYE_AUTH_URL` is a trust anchor.

## Tests

`uv run pytest tests/auth -q` exercises a real local HTTP issuer, client
credentials, token reissue, JWT enforcement, card enrollment, tamper rejection,
identity isolation, and JWKS rotation. It makes no model calls.
