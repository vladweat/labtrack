# Demo deployment and local sandbox

## What this repository demonstrates

LabTrack models sample custody and laboratory shipments. Its existing synthetic
demo is Sender → Courier → Lab: it provisions four scoped demo accounts, creates
a sample and shipment, seals a container, and leaves the shipment
`READY_FOR_PICKUP`.

The seed passwords are read from `DEMO_*_PASSWORD` environment variables; they
are not embedded in the seed code. Use unique generated values in a private
environment file. Demo email addresses end in `.example.test`. The seed only
calls the local LabTrack API. The tracked Compose overlay restricts the one-shot
seed service to the internal data network.

## Seed guard

`python -m app.cli seed-demo` refuses to run unless all of these conditions are
true:

- `APP_ENV=demo`
- `DEMO_SEED_ENABLED=true`
- `POSTGRES_DB` contains the `demo` or `test` component and contains no
  `prod`, `production`, or `live` component
- `DEMO_API_URL` targets `http://api:8000`, localhost, or loopback on port 8000

The opt-in defaults to false. The production API and migration services keep
`APP_ENV=production`; only the one-shot seed service receives the demo mode and
opt-in from [compose.demo-seed.yaml](../compose.demo-seed.yaml). The deployment
Compose project uses `labtrack_demo_postgres_data`; development uses the
independent `labtrack_dev_postgres_data` volume.

## Reproduce locally with isolated synthetic data

These commands use a separate Compose project and PostgreSQL volume. They do not
use the public demo environment or its database. Run them from the repository
root. Docker must already have the `postgres:15` image because development
Compose is configured not to pull images.

Create a private environment file containing fresh synthetic-only credentials;
the script writes it with mode `0600` and does not print the values:

```bash
python3 - <<'PY'
import os
from pathlib import Path
from secrets import token_urlsafe

target = Path(".env.demo.sandbox")
if target.exists():
    raise SystemExit(".env.demo.sandbox already exists; refusing to overwrite it")

values = {
    "POSTGRES_DB": "labtrack_demo_sandbox",
    "POSTGRES_USER": "labtrack_owner",
    "POSTGRES_PASSWORD": token_urlsafe(32),
    "APP_POSTGRES_USER": "labtrack_app",
    "APP_POSTGRES_PASSWORD": token_urlsafe(32),
    "POSTGRES_HOST": "127.0.0.1",
    "POSTGRES_PORT": "5433",
    "POSTGRES_VOLUME_NAME": "labtrack_demo_sandbox_postgres_data",
    "APP_ENV": "demo",
    "DEMO_SEED_ENABLED": "true",
    "DEMO_API_URL": "http://127.0.0.1:8000",
    "JWT_SECRET_KEY": token_urlsafe(48),
    "DEMO_SENDER_EMAIL": "sender@demo.example.test",
    "DEMO_SENDER_PASSWORD": token_urlsafe(24),
    "DEMO_COURIER_EMAIL": "courier@demo.example.test",
    "DEMO_COURIER_PASSWORD": token_urlsafe(24),
    "DEMO_LAB_OPERATOR_EMAIL": "lab-operator@demo.example.test",
    "DEMO_LAB_OPERATOR_PASSWORD": token_urlsafe(24),
    "DEMO_LAB_MANAGER_EMAIL": "lab-manager@demo.example.test",
    "DEMO_LAB_MANAGER_PASSWORD": token_urlsafe(24),
}
descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(descriptor, "w", encoding="utf-8") as env_file:
    env_file.writelines(f"{name}={value}\n" for name, value in values.items())
print("Created .env.demo.sandbox with private synthetic credentials (values omitted).")
PY
```

Start the isolated database, migrate, and run the API in one terminal:

```bash
docker compose --project-name labtrack-demo-sandbox \
  --env-file .env.demo.sandbox up -d postgres
cd backend
set -a
source ../.env.demo.sandbox
set +a
.venv/bin/alembic upgrade head
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000 --no-proxy-headers
```

In another terminal, from `backend/`, seed the synthetic workflow:

```bash
set -a
source ../.env.demo.sandbox
set +a
.venv/bin/python -m app.cli seed-demo
```

The command is idempotent: it reuses the four correctly scoped accounts and the
same idempotency keys for the synthetic sample and shipment. It prints the
synthetic Courier email and Shipment ID, but never prints a password. The web
client can be started separately with `cd web && npm ci && npm run dev`; use
only the generated `.example.test` accounts for local walkthroughs.

To reset only this sandbox, stop the API, then run from the repository root:

```bash
docker compose --project-name labtrack-demo-sandbox \
  --env-file .env.demo.sandbox down --volumes --remove-orphans
rm .env.demo.sandbox
```

This removes the sandbox volume named
`labtrack_demo_sandbox_postgres_data`, not the development or public demo
volumes. Recreate the environment file and repeat the setup to start over. Do
not use this reset command with the public deployment project.

Для проверки backup/restore только этой локальной synthetic БД см.
[воспроизводимый drill](backup-restore.md). Он не является инструкцией
восстановления public demo.

## Public synthetic demo

The base `deploy/demo/compose.yaml` and its `.env` are environment-private
deployment files and are not in this source checkout. Run the following from
the deployment checkout that contains them; the tracked overlay is in this
repository. Start the one-shot seed with:

```bash
docker compose --project-name labtrack-demo \
  --env-file deploy/demo/.env \
  --file deploy/demo/compose.yaml \
  --file compose.demo-seed.yaml \
  --profile demo-seed run --rm seed
```

The overlay sets `APP_ENV=demo` and `DEMO_SEED_ENABLED=true` only on that
service, restricts it to the internal network, and preserves the existing
volume names by default. It accepts `DEMO_POSTGRES_VOLUME`,
`DEMO_CADDY_DATA_VOLUME`, and `DEMO_CADDY_CONFIG_VOLUME` overrides when a
separate deployment sandbox needs its own volumes. Keep `DEMO_*_PASSWORD`,
database passwords, and `JWT_SECRET_KEY` in the private deployment environment
file. Do not copy them into this repository, issue output, screenshots, or
shared logs. The seed is idempotent and is not a reset of the shared public
demo.

### Login source address behind the public edge

The public repository does not contain `deploy/demo/compose.yaml` or an active
Caddy/Nginx configuration. The older private checkout also has no tracked edge
configuration. The API Dockerfiles in this repository start Uvicorn with
`--no-proxy-headers`, but a private Compose `command` can override that default.
The deployed Uvicorn command, edge header rules, API reachability, and actual
proxy peer therefore remain unverified from this repository.

Keep `LOGIN_TRUSTED_PROXY_NETWORKS` empty until the deployment owner verifies
all of the following against the running configuration:

1. Uvicorn still starts with `--no-proxy-headers`; proxy middleware must not
   rewrite `request.client.host` before the login limiter reads the immediate
   transport peer.
2. Untrusted clients cannot connect directly to the API and bypass the edge.
3. The edge removes or overwrites any incoming `X-Real-IP`, then sets it to the
   validated client address. If another load balancer precedes Caddy/Nginx,
   that proxy must be explicitly trusted there and the client address must be
   derived from the validated proxy chain. Remove incoming `X-Forwarded-For`;
   the application does not use it for rate limiting.
4. `LOGIN_TRUSTED_PROXY_NETWORKS` contains only the immediate proxy peer's
   pinned `/32` or `/128`, never a shared Docker subnet or broad proxy range.

For a direct Caddy edge, the reverse-proxy handler can set `X-Real-IP` from
`{client_ip}` and remove `X-Forwarded-For`; these are required settings to
verify or add in the private Caddyfile, not claims about the current deployment.
If the proxy peer cannot be pinned and isolated, leave the setting empty: the
limiter will use the transport peer, so clients behind a shared proxy will
share a bucket. The application does not trust client-supplied identity by
default.
