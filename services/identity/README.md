# plinth-identity

JWT capability-token issuance, verification, and revocation.

The identity service is the trust root for Plynf in v0.3. It mints HS256 JWTs
that carry agent + tenant + workspace + scope claims, persists metadata for
introspection and revocation, and exposes a verify endpoint other services
can call (or replicate locally with the shared secret).

## Endpoints

- `POST /v1/tokens` — issue a JWT
- `POST /v1/tokens/verify` — verify a JWT, return claims
- `POST /v1/tokens/{jti}/revoke` — revoke
- `GET  /v1/tokens/{jti}` — token metadata (no JWT body)
- `GET  /v1/.well-known/jwks.json` — empty for HS256 (RS256 will populate)
- `GET  /healthz`

## Run

```bash
PLINTH_DATA_DIR=/tmp/plinth-data \
PLINTH_IDENTITY_PORT=7425 \
python -m plinth_identity
```

When the env var `PLINTH_IDENTITY_JWT_SECRET` is unset and `auto_generate_secret`
is enabled (default), a 32-byte secret is generated and persisted at
`$PLINTH_DATA_DIR/identity-jwt-secret`.

## Test

```bash
pytest -q
```
