"""Local sidecar browser provider: a long-lived headless Chromium with uBlock Origin Lite,
run in an isolated Apple Container on the same machine.

Configuration:
  * Endpoint:  ``BROWSER_LOCAL_CDP_URL`` (env) or ``browser.local_cdp_url`` (config),
               default ``http://127.0.0.1:9222``.
  * Selection: ``browser.cloud_provider: local-sidecar`` — an explicit, never-
               auto-detected provider (no credentials, so it must be chosen).

The sidecar is a *persistent* browser: ``close_session`` / ``emergency_cleanup`` are
no-ops by design (the container owns the browser's lifetime). ``create_session``
discovers the current ``webSocketDebuggerUrl`` from the sidecar's CDP endpoint so a
restarted sidecar is always picked up; the per-task ``session_name`` lets Hermes
launch its own agent-browser client for each task.

No cloud: no API calls, no third-party service. The only outbound connection made by
this provider is the local ``/json/version`` discovery request to the sidecar.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import socket
import uuid
from typing import Dict, Optional
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from agent.browser_provider import BrowserProvider

logger = logging.getLogger(__name__)

DEFAULT_CDP_URL = "http://127.0.0.1:9222"

# create_session runs only when actually starting a session; a little more patience is fine.
_CREATE_TIMEOUT_S = 5.0


def _parse_endpoint(url: str) -> tuple:
    """Return ``(host, port)`` from an http(s)/ws(s) URL."""
    parts = urlsplit((url or "").strip())
    scheme = parts.scheme.lower()
    if scheme in ("http", "ws"):
        default_port = 80 if scheme == "http" else 3128
    elif scheme in ("https", "wss"):
        default_port = 443 if scheme == "https" else 3129
    else:
        raise ValueError(f"unsupported scheme in CDP URL: {url!r} (use http/https/ws/wss)")
    host = parts.hostname
    if not host:
        raise ValueError(f"no host in CDP URL: {url!r}")
    port = parts.port or default_port
    return host, port


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def _is_allowed_local_ip(ip: str) -> bool:
    """True for the explicitly allowed local ranges only.

    We use an explicit network list instead of ``ipaddress.is_private`` — Python's
    ``is_private`` also covers the IANA *documentation* ranges (192.0.2.0/24,
    198.51.100.0/24, 203.0.113.0/24) and benchmarking space, which we do NOT want
    to treat as "local": CDP grants full browser control, so anything not on this
    list is refused. Covered:
      * loopback (127.0.0.0/8, ::1)
      * RFC1918 private (10/8, 172.16/12, 192.168/16) — docker bridges, LAN
      * CGNAT 100.64.0.0/10 — also the Tailscale range, in case the sidecar is
        reached over Tailscale rather than loopback
      * IPv6 unique-local fc00::/7
    """
    allowed_v4 = (
        ipaddress.ip_network("127.0.0.0/8"),
        ipaddress.ip_network("10.0.0.0/8"),
        ipaddress.ip_network("172.16.0.0/12"),
        ipaddress.ip_network("192.168.0.0/16"),
        ipaddress.ip_network("100.64.0.0/10"),
    )
    allowed_v6 = (
        ipaddress.ip_network("::1/128"),
        ipaddress.ip_network("fc00::/7"),
    )
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if addr.version == 4:
        return any(addr in net for net in allowed_v4)
    return any(addr in net for net in allowed_v6)


def _resolve_all_hosts(host: str, timeout: float = _CREATE_TIMEOUT_S) -> list:
    """Resolve a hostname (or return the literal) → all IP strings; [] on failure.

    Used only by ``create_session`` (never by ``is_available`` — see its
    network-free contract) to enforce that the "local sidecar" endpoint is
    really local: a ``.local`` / docker-bridge / hostname that resolves to a
    *public* IP is exactly the kind of thing that must NOT be given a browser
    session.
    """
    if _is_ip_literal(host):
        return [host]
    try:
        prev = None
        try:
            import signal
            if hasattr(signal, "setitimer"):
                prev = signal.getitimer(signal.ITIMER_REAL)
                signal.setitimer(signal.ITIMER_REAL, timeout)
        except Exception:
            pass
        try:
            infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
        finally:
            try:
                import signal
                if prev is not None:
                    signal.setitimer(signal.ITIMER_REAL, *prev)
            except Exception:
                pass
        return sorted({info[4][0] for info in infos})
    except Exception:
        return []


class LocalSidecarProvider(BrowserProvider):
    """Connects the built-in ``browser_*`` tools to a locally hosted Chromium sidecar.

    The sidecar is a persistent, isolated headless browser (own Apple Container,
    uBlock Origin Lite pre-loaded) that Hermes reaches over CDP on the host
    loopback. Sessions are not created or destroyed by this provider — the
    container's lifetime is the session's lifetime.
    """

    @property
    def name(self) -> str:
        """Registry key — the value users write as ``browser.cloud_provider``."""
        return "local-sidecar"

    @property
    def display_name(self) -> str:
        return "Local Sidecar"

    def _cdp_url(self) -> str:
        env_val = (os.environ.get("BROWSER_LOCAL_CDP_URL") or "").strip()
        if env_val:
            return env_val
        try:
            from hermes_cli.config import read_raw_config
            browser_cfg = read_raw_config().get("browser", {})
            if isinstance(browser_cfg, dict):
                val = str(browser_cfg.get("local_cdp_url") or "").strip()
                if val:
                    return val
        except Exception:
            pass
        return DEFAULT_CDP_URL

    def _validate_local_endpoint(self, resolve: bool = True) -> Optional[tuple]:
        """Return ``(host, port)`` when the configured endpoint is genuinely local.

        Rules: scheme http(s)/ws(s); IP-literal hosts must be in the allowed
        local ranges; *hostname* hosts are accepted when ``resolve=False``
        (pure syntax — used by the network-free ``is_available``) or, when
        ``resolve=True``, must resolve only to allowed local ranges (used by
        ``create_session``, where the enforcement actually bites). Public IPs are
        refused by design — CDP grants full browser control. Returns None
        otherwise.
        """
        url = (self._cdp_url() or "").strip()
        try:
            host, port = _parse_endpoint(url)
        except ValueError:
            return None
        if _is_ip_literal(host):
            if not _is_allowed_local_ip(host):
                logger.warning(
                    "Refusing non-local CDP endpoint %s (public IP) — a local sidecar "
                    "must live on this machine.", url,
                )
                return None
            return host, port
        if not resolve:
            # Syntax pass only: the hostname is re-validated (with DNS) in
            # create_session, where refusing actually prevents a connection.
            return host, port
        ips = _resolve_all_hosts(host)
        if not ips:
            return None
        if not all(_is_allowed_local_ip(ip) for ip in ips):
            logger.warning(
                "Refusing CDP endpoint %s — %s resolves to a public address (%s). "
                "A 'local' sidecar must resolve only to loopback/private.", url, host,
                ", ".join(ips),
            )
            return None
        return host, port

    def _reachable(self, host: str, port: int, timeout: float) -> bool:
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except OSError:
            return False

    def _discover_websocket_url(self) -> Optional[str]:
        """Resolve the current CDP websocket URL from the sidecar's ``/json/version``.

        Mirrors ``tools.browser_tool_cdp._resolve_cdp_override`` (a user-supplied HTTP
        root is resolved to its ``webSocketDebuggerUrl``), kept self-contained so the
        plugin has no dependency on Hermes internals.
        """
        raw = (self._cdp_url() or "").strip()
        if not raw:
            return None
        lowered = raw.lower()
        if "/devtools/browser/" in lowered:
            return raw

        discovery_url = raw
        if lowered.startswith(("ws://", "wss://")):
            if not (raw.count(":") == 2
                    and raw.rstrip("/").rsplit(":", 1)[-1].isdigit()
                    and "/" not in raw.split(":", 2)[-1]):
                return raw
            discovery_url = ("http://" if lowered.startswith("ws://") else "https://") + raw.split("://", 1)[1]
        version_url = discovery_url if discovery_url.lower().endswith("/json/version") \
            else discovery_url.rstrip("/") + "/json/version"

        try:
            import json
            req = Request(version_url, headers={"User-Agent": "hermes-local-sidecar/1.0"})
            with urlopen(req, timeout=_CREATE_TIMEOUT_S) as resp:
                payload = json.loads(resp.read().decode("utf-8", "replace"))
        except Exception as exc:
            logger.warning("Local sidecar CDP discovery at %s failed: %s", raw, exc)
            return None
        ws_url = str(payload.get("webSocketDebuggerUrl") or "").strip()
        if ws_url:
            return ws_url
        logger.warning("CDP discovery at %s did not return webSocketDebuggerUrl; using raw endpoint", version_url)
        return raw

    def is_available(self) -> bool:
        """Cheap, network-free check (contract: runs at tool-registration time and on
        every ``hermes tools`` paint, so no DNS/TCP/HTTP here).

        return True when the endpoint is configured and syntactically a local
        target (loopback/private IP, or a hostname — hostnames get their full
        DNS-level verification in ``create_session``). Whether the sidecar is
        actually *up* is deliberately NOT checked here: a stopped sidecar keeps
        the row selectable (mirrors how the bundled Browserbase row stays
        selectable when the cloud is down); the real liveness check happens in
        ``create_session`` and fails with a clear, actionable error instead of
        silently falling back to any cloud provider.
        """
        return self._validate_local_endpoint(resolve=False) is not None

    def create_session(self, task_id: str) -> Dict[str, object]:
        """Hand the sidecar's current CDP websocket to Hermes.

        No cloud request: the sidecar is a long-lived local browser that already
        exists. Each ``task_id`` gets a fresh agent-browser session name so the
        per-task CDP supervisor can drive its own tab in the persistent sidecar.
        """
        target = self._validate_local_endpoint()
        if target is None:
            raise ValueError(
                "Local sidecar endpoint is not a reachable loopback/private address. "
                f"Refusing to treat {self._cdp_url()!r} as a local CDP endpoint "
                "(CDP grants full browser control — point BROWSER_LOCAL_CDP_URL at "
                "the Mac's 127.0.0.1 forwarded from the sidecar container)."
            )
        if not self._reachable(target[0], target[1], _CREATE_TIMEOUT_S):
            raise RuntimeError(
                f"Local sidecar is not reachable at {self._cdp_url()!r}. "
                "Start it first: `container start hermes-browser` "
                "(see the hermes-local-browser README in the Hermes workspace)."
            )
        ws = self._discover_websocket_url()
        if not ws:
            raise RuntimeError(
                f"Local sidecar accepted TCP at {self._cdp_url()!r} but CDP discovery "
                "(/json/version) failed — the browser may not be fully up yet."
            )
        session_name = f"hermes_{task_id}_{uuid.uuid4().hex[:8]}"
        return {
            "session_name": session_name,
            # Stable "provider session ID" — the sidecar has no per-call session to close,
            # so we use a fixed opaque token; close/emergency_cleanup are no-ops.
            "bb_session_id": "local-sidecar",
            "cdp_url": ws,
            "features": {
                "sidecar-managed": True,
                "adblock": "uBlock Origin Lite",
                "cloud": False,
            },
        }

    def close_session(self, session_id: str) -> bool:
        """No-op: the sidecar container owns the browser's lifetime.

        Closing would mean tearing down the whole persistent browser for one task,
        which would defeat the point of a sidecar. We report success so the
        dispatcher's cleanup loop keeps moving.
        """
        logger.debug("close_session(%s) — no-op (sidecar is persistent)", session_id)
        return True

    def emergency_cleanup(self, session_id: str) -> None:
        """No-op: never raise from a signal/atexit handler, and there is nothing
        to release — the sidecar is a persistent daemon."""
        logger.debug("emergency_cleanup(%s) — no-op (sidecar is persistent)", session_id)

    def get_setup_schema(self) -> Dict[str, object]:
        """Row in the `hermes tools` browser picker. No API key — just enable it
        and (optionally) point it at a different CDP URL."""
        return {
            "name": "Local Sidecar",
            "badge": "free",
            "tag": "Local Chromium in an isolated Apple Container, uBlock Origin Lite pre-loaded",
            "env_vars": [],
        }
