# LabTrack web app

This React, TypeScript, and Vite app provides role-specific Sender, Courier,
Lab, Lab Manager, and Operations workspaces. Courier offline Pickup/Delivery
commands remain local proposals until accepted by the API; the server is the
custody authority.

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

The local Sender→Courier→Lab browser/API acceptance is separate from the
default mocked suite. From `web/`, run `npm run test:acceptance:local` after
starting the disposable local setup described in the [development guide](../docs/development.md).

## Local data and demo environment

The access token stays in memory and is not restored after a reload. Sign in
again to access the same account's IndexedDB queue. Snapshots, immutable
commands, local command states, and sync outcomes are scoped to the authenticated
user and this browser installation. Logging out or switching accounts warns
before deleting that user's saved device data. A separate browser installation
has a separate queue.

The generated service worker precaches static shell assets only. API traffic
uses `cache: "no-store"` and has no service-worker runtime cache. The application
stores Sample records with material type, collection time, and opaque external
references, but no patient identity fields. External references may still
identify people, so use synthetic data only. The build in `dist/` is static
output.
The [demo environment](https://labtrack.wvzl.site) uses shared synthetic
accounts and mutable synthetic data. The link identifies an environment; it
does not promise availability or that the environment matches this revision.
