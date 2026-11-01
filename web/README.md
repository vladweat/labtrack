# Courier PWA

This React, TypeScript, and Vite app supports only offline Pickup confirmation
and one-container Delivery initiation. The API remains the custody authority;
local pending commands do not change server state.

## Local development

Use Node 22.13 or newer within Node 22 and npm 10. From `web/`:

```sh
npm ci
npm run dev
```

The Vite server proxies `/api/v1` to `http://127.0.0.1:8000`. Start the FastAPI
service from `backend/` using the instructions in [backend/README.md](../backend/README.md).
Set `API_PROXY_TARGET` if the API is listening elsewhere. `VITE_API_BASE` can
override the browser API prefix; it defaults to `/api/v1`.

## Checks

```sh
npx playwright install chromium
npm run check
```

`check` runs ESLint, the IndexedDB acknowledgement-order unit tests, a
TypeScript-checked production build, and Chromium browser tests against the
built app. Browser tests mock the API and verify offline behavior, replay,
account scoping, and service-worker cache boundaries.

## Local data and deployment boundary

The access token stays in memory and is not restored after a reload. Sign in
again to access the same account's IndexedDB queue. Snapshots, immutable
commands, local command states, and sync outcomes are scoped to the authenticated
user and this browser installation. Logging out or switching accounts warns
before deleting that user's saved device data. A separate browser installation
has a separate queue.

The generated service worker precaches static shell assets only. API traffic
uses `cache: "no-store"` and has no service-worker runtime cache. This app does
not store patient or Sample fields. The build in `dist/` is static output; Stage
G does not configure production hosting or deployment. When hosted later, serve
the static files on the API origin or behind an equivalent same-origin reverse
proxy so `/api/v1` remains reachable.
