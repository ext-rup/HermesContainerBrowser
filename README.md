# Local Chromium sidecar for Hermes

Runs Chromium 152 + **uBlock Origin Lite** inside an isolated Apple Container. Hermes on macOS drives it over CDP through a **host-loopback-only** port forward. No cloud, no Browserbase, no API key.

Also ships a **Hermes browser-provider plugin** so the sidecar appears as a first-class selectable browser backend.

## Quick start

```bash
cd hermes-local-browser
container build --pull -t hermes-local-browser:2026.914.1325 .
./install.sh
```

`install.sh` (idempotent, works with or without the `hermes` CLI on PATH):

1. copies the plugin into `~/.hermes/plugins/browser/browser-local-sidecar/`
2. enables it in `config.yaml` (`plugins.enabled` allow-list)
3. sets `browser.cloud_provider: local-sidecar` (the Capabilities selection)
4. checks the sidecar is up; prints the exact `container run` command if not

Then **start a new Hermes session** — provider selection is cached per process. If the sidecar isn't running yet, the script tells you exactly how to start it:

```bash
container run -d --name hermes-browser --init --memory 2G --cpus 2 \
  --shm-size 512M -p 127.0.0.1:9222:9222 hermes-local-browser:2026.914.1325
```

That's the whole install. Everything below is reference detail.

## Layout

```text
hermes-local-browser/
├── install.sh            # one-shot: copy plugin, enable, select provider, check sidecar
├── Dockerfile            # image: debian-trixie-slim + chromium + uBOL (SHA-pinned)
├── start-browser.sh      # tini entrypoint: socat CDP relay + headless chromium
├── plugin/browser-local-sidecar/
│   ├── plugin.yaml       # kind: backend, provides_browser_providers: [local-sidecar]
│   ├── __init__.py       # register(ctx) → ctx.register_browser_provider(...)
│   └── provider.py       # LocalSidecarProvider (BrowserProvider ABC)
├── test_plugin.py        # 36-check offline suite (fake CDP sidecar, stub Hermes modules)
└── README.md
```

## 1. Build on the Mac

```bash
cd hermes-local-browser
container build --pull -t hermes-local-browser:2026.914.1325 .
```

## 2. Run

```bash
container run -d \
  --name hermes-browser \
  --init \
  --memory 2G \
  --cpus 2 \
  --shm-size 512M \
  -p 127.0.0.1:9222:9222 \
  hermes-local-browser:2026.914.1325
```

- **Do not publish as `0.0.0.0:9222`.** CDP grants complete browser control (cookies, downloads, every page). `-p 127.0.0.1:9222:9222` keeps it on the Mac's loopback only (Apple Container publishes host-side; the in-container side is reached by Hermes via that published loopback).
- The entrypoint relays Chromium's internal `127.0.0.1:9223` to the container's `0.0.0.0:9222` (Chromium 152 binds CDP to its own loopback even when asked for 0.0.0.0; `socat` is a TCP relay only — never in the page-traffic path).

### Verify from the Mac

```bash
curl -fsS http://127.0.0.1:9222/json/version
```

The JSON must contain `webSocketDebuggerUrl`.

## 3. Install the plugin

```bash
mkdir -p ~/.hermes/plugins/browser
cp -R plugin/browser-local-sidecar ~/.hermes/plugins/browser/
hermes plugins enable browser/browser-local-sidecar
```

User plugins are opt-in: the plugin loads only after the `plugins.enable` step (or after `migrate_config` grandfathering on first config write).

## 4. Select it as the browser backend

```bash
hermes config set browser.cloud_provider local-sidecar
```

That's the whole selection: it appears in `hermes tools` (picker row "Local Sidecar", badge *free*, no key prompt), and the built-in `browser_*` tools drive the sidecar. No `browser.cdp_url` or `browser.backend` keys needed — the plugin supplies the endpoint (default `http://127.0.0.1:9222`; override with `BROWSER_LOCAL_CDP_URL` in `~/.hermes/.env` if you change the published port).

Start a **new Hermes session** afterwards — provider selection is cached per process.

### Security properties of the plugin

- **Loopback/private-only by construction.** The endpoint must be a loopback IP, RFC1918, CGNAT/100.64/10 (covers Tailscale), or `fc00::/7` (or a hostname resolving only to those). Public-IP endpoints are *refused before any connection* — `ipaddress.is_private` is deliberately not trusted (it also matches IANA documentation ranges).
- **No silent cloud fallback.** If the sidecar is down, `create_session` raises a `RuntimeError` naming the URL and the fix; it never routes to Browserbase.
- **Persistent browser semantics.** `close_session`/`emergency_cleanup` are no-ops (the container owns the browser's lifetime); each Hermes task still gets its own agent-browser session/tab.
- **Ad blocking on every page.** uBlock Origin Lite (MV3, declarativeNetRequest) is baked into the image and loaded via `--load-extension` — verified: Google Analytics / AdRoll / Taboola requests report CDP `blockedReason: blocked-by-client` while a control request returns 200.

## 5. Verify no cloud fallback

In the fresh session:

1. `browser_navigate` to `https://example.com` — it works.
2. Stop the sidecar: `container stop hermes-browser`
3. Navigate again — it must **fail** (with the plugin's clear error), not open a cloud session.
4. `container start hermes-browser` — works again.

Optionally, delete `BROWSERBASE_API_KEY`, `BROWSERBASE_PROJECT_ID`, `BROWSER_USE_API_KEY`, `FIRECRAWL_API_KEY` from `~/.hermes/.env` — belt and braces against future auto-detection.

## Fallback: no plugin, plain config

If you'd rather not install the plugin:

```bash
hermes config set browser.cloud_provider local
hermes config set browser.cdp_url http://127.0.0.1:9222
hermes config set browser.backend off
```

`cloud_provider=local` is intentional (an *unset* key lets Hermes auto-detect Browser Use/Browserbase credentials); `backend=off` keeps the built-in `browser_*` tools instead of the Browser Use driver. New session required.

## Browser profile persistence

A stopped-then-started container keeps its writable layer. To survive image *rebuilds*, mount a dedicated Apple Container volume at `/data/profile`. **Never** mount the Hermes home, the workspace, or your personal Chrome profile into this container.

## Updating uBlock Origin Lite

Pinned in the `Dockerfile` (`UBOL_VERSION`, `UBOL_SHA256`). To update:

1. Download the current official Chromium ZIP from the `uBlockOrigin/uBOL-home` releases.
2. Compute its SHA-256.
3. Update both build args, rebuild, recreate the container.

Classic uBlock Origin is Manifest V2 and is not appropriate for Chromium 152; this uses Raymond Hill's official MV3 build (uBOL).

## Test the plugin offline

```bash
python3 test_plugin.py
```

36 checks: ABC conformance, registry wiring, CDP discovery against a fake sidecar, persistent-session no-ops, down-sidecar errors, public-IP refusal, bad-scheme handling, config/env precedence, and manifest validity. Needs only the Python stdlib.
