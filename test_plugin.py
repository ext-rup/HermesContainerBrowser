"""Test harness: validates the local-sidecar plugin against a faithful stub of the
Hermes `agent.browser_provider` contract + a fake CDP sidecar.

Run:  python3 test_plugin.py
Exits non-zero on any failure.
"""
import importlib.util
import json
import os
import socket
import sys
import threading
import types
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN_DIR = os.path.join(HERE, "plugin", "browser-local-sidecar")

# ---------------------------------------------------------------------------
# 0. Network-call checkpoint: records any DNS/TCP/HTTP activity so we can assert
#    is_available() makes zero network calls (its contract: runs on every
#    `hermes tools` paint, so it must be network-free).
# ---------------------------------------------------------------------------
_checkpoints = []

def _is_loopback_target(addr):
    host = addr[0]
    return host in ("127.0.0.1", "localhost") or str(host).startswith("127.") or host == "::1"

def _checkpoint_create_connection(addr, *a, **kw):
    _checkpoints.append(("create_connection", (addr, *a), kw))
    if not _is_loopback_target(addr):
        # Block any non-loopback egress: the plugin must never talk to anything
        # but the local sidecar, and the test must stay hermetic.
        raise OSError(f"simulated egress block: {addr!r}")
    return _orig_create_connection(addr, *a, **kw)
_orig_create_connection = socket.create_connection
socket.create_connection = _checkpoint_create_connection

def _checkpoint_getaddrinfo(host, *a, **kw):
    _checkpoints.append(("getaddrinfo", (host, *a), kw))
    return _orig_getaddrinfo(host, *a, **kw)
_orig_getaddrinfo = socket.getaddrinfo
socket.getaddrinfo = _checkpoint_getaddrinfo

_orig_urlopen = urllib.request.urlopen
def _checkpoint_urlopen(*a, **kw):
    _checkpoints.append(("urlopen", a, kw))
    return _orig_urlopen(*a, **kw)
urllib.request.urlopen = _checkpoint_urlopen

# ---------------------------------------------------------------------------
# 1. Stub the Hermes modules the plugin imports, BEFORE importing the plugin.
# ---------------------------------------------------------------------------

def _make_agent_module():
    agent = types.ModuleType("agent")

    import abc

    class BrowserProvider(abc.ABC):
        """Faithful stub of ``agent.browser_provider.BrowserProvider``:
        abstract name/is_available/create_session/close_session/emergency_cleanup."""
        @property
        @abc.abstractmethod
        def name(self) -> str: ...
        @property
        def display_name(self) -> str:
            return self.name
        @abc.abstractmethod
        def is_available(self) -> bool: ...
        @abc.abstractmethod
        def create_session(self, task_id: str): ...
        @abc.abstractmethod
        def close_session(self, session_id: str) -> bool: ...
        @abc.abstractmethod
        def emergency_cleanup(self, session_id: str) -> None: ...
        def get_setup_schema(self) -> dict:
            return {"name": self.display_name, "badge": "", "tag": "", "env_vars": []}

    bp_mod = types.ModuleType("agent.browser_provider")
    bp_mod.BrowserProvider = BrowserProvider
    agent.browser_provider = bp_mod
    sys.modules["agent"] = agent
    sys.modules["agent.browser_provider"] = bp_mod
    return BrowserProvider


def _make_hermes_cli_module():
    hermes_cli = types.ModuleType("hermes_cli")
    cfg_mod = types.ModuleType("hermes_cli.config")

    _state = {"browser": {"local_cdp_url": ""}}

    def read_raw_config():
        return _state

    def set_cfg(browser_cfg):
        _state["browser"] = browser_cfg

    cfg_mod.read_raw_config = read_raw_config
    hermes_cli.config = cfg_mod
    sys.modules["hermes_cli"] = hermes_cli
    sys.modules["hermes_cli.config"] = cfg_mod
    return hermes_cli


_BROWSER_PROVIDER = _make_agent_module()
_hermes_cli = _make_hermes_cli_module()

# Import the provider module (it does `from agent.browser_provider import BrowserProvider`)
spec = importlib.util.spec_from_file_location(
    "local_sidecar_provider", os.path.join(PLUGIN_DIR, "provider.py")
)
provider_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(provider_mod)
LocalSidecarProvider = provider_mod.LocalSidecarProvider

# Import __init__.py register()
spec2 = importlib.util.spec_from_file_location(
    "local_sidecar_pkg", os.path.join(PLUGIN_DIR, "__init__.py")
)
pkg = importlib.util.module_from_spec(spec2)
# patch: the pkg uses relative import `from .provider import ...` — give it a package name
pkg.__package__ = "local_sidecar_pkg"
sys.modules["local_sidecar_pkg"] = pkg
sys.modules["local_sidecar_pkg.provider"] = provider_mod
spec2.loader.exec_module(pkg)


class FakeCtx:
    """Minimal PluginContext stub: records registered browser providers."""
    def __init__(self):
        self.providers = []
    def register_browser_provider(self, p):
        self.providers.append(p)


# ---------------------------------------------------------------------------
# 2. Fake CDP sidecar: HTTP server serving /json/version + a real open TCP port.
# ---------------------------------------------------------------------------

class CDPHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/json/version":
            body = json.dumps({
                "Browser": "Chromium/152.0.0.0",
                "webSocketDebuggerUrl": "ws://127.0.0.1:9223/devtools/browser/fake-uuid-1234",
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.end_headers()

    def log_message(self, *a):
        pass  # quiet


def start_sidecar():
    server = ThreadingHTTPServer(("127.0.0.1", 0), CDPHandler)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return server, port


# ---------------------------------------------------------------------------
# 3. Tests
# ---------------------------------------------------------------------------

PASS, FAIL = 0, 0
def check(label, cond, extra=""):
    global PASS, FAIL
    status = "ok  " if cond else "FAIL"
    if cond:
        PASS += 1
    else:
        FAIL += 1
    print(f"[{status}] {label}" + (f"  ({extra})" if extra and not cond else ""))


print("=" * 70)
print("LOCAL-SIDECAR PLUGIN TEST SUITE")
print("=" * 70)

# --- T1: register() wires the provider into the registry -------------------
ctx = FakeCtx()
pkg.register(ctx)
check("register() registers exactly 1 browser provider", len(ctx.providers) == 1)
check("registered provider is a LocalSidecarProvider", isinstance(ctx.providers[0], LocalSidecarProvider))
p = ctx.providers[0]

# --- T2: identity contract --------------------------------------------------
check("name == 'local-sidecar'", p.name == "local-sidecar", f"got {p.name!r}")
check("isinstance of Hermes BrowserProvider ABC", isinstance(p, _BROWSER_PROVIDER))
try:
    _BROWSER_PROVIDER()
    check("abstract base cannot be instantiated directly", False)
except TypeError:
    check("abstract base cannot be instantiated directly", True)

# --- T3: get_setup_schema shape (picker row) -------------------------------
schema = p.get_setup_schema()
check("setup schema has name", schema.get("name") == "Local Sidecar", str(schema.get("name")))
check("setup schema has free badge", schema.get("badge") == "free", str(schema.get("badge")))
check("setup schema tag mentions uBlock Origin Lite", "uBlock Origin Lite" in str(schema.get("tag")))
check("setup schema has no env vars (no key to prompt)", schema.get("env_vars") == [])

# --- T4: is_available is CONFIG-only (no liveness probe, no network) --------
# The docs require is_available to make zero network calls: it runs at
# tool-registration time and on every `hermes tools` paint. A *closed* loopback
# port is still a valid configured endpoint, so is_available() is True here —
# liveness is deliberately deferred to create_session.
os.environ["BROWSER_LOCAL_CDP_URL"] = "http://127.0.0.1:1"  # closed port
_checkpoints.clear()
check("is_available() True for closed-but-local port (config-only check)",
      p.is_available() is True)
check("is_available() makes NO network calls (no DNS/TCP/HTTP)",
      not _checkpoints, f"calls: {_checkpoints}")

# --- T5: is_available True when endpoint is a valid local IP ---------------
server, port = start_sidecar()
os.environ["BROWSER_LOCAL_CDP_URL"] = f"http://127.0.0.1:{port}"
_checkpoints.clear()
check("is_available() True when endpoint is local", p.is_available() is True)
check("is_available() makes NO network calls (no DNS/TCP/HTTP)",
      not _checkpoints, f"calls: {_checkpoints}")

# --- T6: create_session returns the full metadata contract -----------------
try:
    meta = p.create_session("task-123")
    check("create_session returns dict", isinstance(meta, dict))
    check("has session_name", isinstance(meta.get("session_name"), str) and meta["session_name"].startswith("hermes_task-123_"), str(meta.get("session_name")))
    check("has bb_session_id (legacy key, intact)", meta.get("bb_session_id") == "local-sidecar", str(meta.get("bb_session_id")))
    check("has cdp_url (websocket)", isinstance(meta.get("cdp_url"), str) and meta["cdp_url"].startswith("ws://"), str(meta.get("cdp_url")))
    # The fake server hardcodes its webSocketDebuggerUrl to 9223 in the JSON body;
    # discovery must return exactly what the server declares (not the bound port).
    check("cdp_url is the discovered webSocketDebuggerUrl (as declared by the server)",
          meta.get("cdp_url") == "ws://127.0.0.1:9223/devtools/browser/fake-uuid-1234", str(meta.get("cdp_url")))
    check("features.flags: sidecar-managed + uBOL + cloud=False",
          meta.get("features", {}).get("sidecar-managed") is True
          and meta.get("features", {}).get("adblock") == "uBlock Origin Lite"
          and meta.get("features", {}).get("cloud") is False)
except Exception as e:
    check("create_session did not raise", False, repr(e))

# --- T7: close_session / emergency_cleanup are no-ops and never raise ------
check("close_session() returns True (no-op, keeps cleanup loop moving)", p.close_session("local-sidecar") is True)
raised = False
try:
    p.emergency_cleanup("local-sidecar")
except Exception:
    raised = True
check("emergency_cleanup() does not raise", not raised)

# --- T8: create_session when sidecar is down -> RuntimeError ---------------
os.environ["BROWSER_LOCAL_CDP_URL"] = f"http://127.0.0.1:{port + 1000}"  # closed
raised = False
try:
    p.create_session("task-456")
except RuntimeError as e:
    raised = True
    check("down sidecar: error message names the URL and fix",
          str(port + 1000) in str(e) and "container start" in str(e), str(e))
except Exception as e:
    raised = True
check("create_session on down sidecar raises (no silent cloud fallback)", raised)

# --- T9: SECURITY — public (but routable) IP endpoint must be refused ------
# 8.8.8.8 is publicly routable so the reachability check PASSES and we exercise
# the policy refusal, not a timeout. (The doc-range 203.0.113.x IPs are
# unroutable and would hit the unreachable branch first.)
os.environ["BROWSER_LOCAL_CDP_URL"] = "http://8.8.8.8:9222"
avail = p.is_available()
check("is_available() False for public-IP endpoint (refused)", avail is False)
try:
    meta = p.create_session("task-789")
    check("create_session() refuses public-IP endpoint", False, f"returned {meta}")
except ValueError as e:
    check("create_session() refuses public-IP endpoint (ValueError, no connect attempt)",
          "full browser control" in str(e), str(e))
except Exception as e:
    check("create_session() refuses public-IP endpoint", False, repr(e))

# --- T10: bad scheme rejected cleanly --------------------------------------
os.environ["BROWSER_LOCAL_CDP_URL"] = "ftp://127.0.0.1:1"
check("bad scheme (ftp) → is_available False, no crash", p.is_available() is False)
try:
    p.create_session("x")
    check("bad scheme → create_session raises", False)
except Exception:
    check("bad scheme → create_session raises", True)

# --- T11: config-file fallback for endpoint --------------------------------
os.environ.pop("BROWSER_LOCAL_CDP_URL")
_hermes_cli.config.read_raw_config = lambda: {"browser": {"local_cdp_url": f"http://127.0.0.1:{port}"}}
check("config-file endpoint honored when env unset", p.is_available() is True)
_hermes_cli.config.read_raw_config = lambda: {"browser": {}}
check("default endpoint (127.0.0.1:9222) used when nothing set",
      p._cdp_url() == "http://127.0.0.1:9222")

# --- T12: plugin.yaml manifest is valid and declares the provider ----------
# (kept last: T13 deliberately blocks egress, which would also block the
#  ThreadingHTTPServer's socket handling in this process)

# --- T13: hostname endpoint → is_available() is still network-free ----------
# DNS is blocked (our getaddrinfo checkpoint records it and fails open for the
# resolve=False path: the provider must not call it at all).
os.environ["BROWSER_LOCAL_CDP_URL"] = "http://my-sidecar.local:9222"
_checkpoints.clear()
def _blocked_getaddrinfo(host, *a, **kw):
    _checkpoints.append(("getaddrinfo-BLOCKED", (host,), kw))
    raise OSError(f"simulated DNS block: {host!r}")
_orig_getaddrinfo2 = socket.getaddrinfo
socket.getaddrinfo = _blocked_getaddrinfo
try:
    check("is_available() True for hostname endpoint WITHOUT any DNS call",
          p.is_available() is True)
    check("is_available() hostname path made zero getaddrinfo calls",
          not _checkpoints, f"calls: {_checkpoints}")
    check("hostname accepted on the syntax pass (resolve=False)",
          p._validate_local_endpoint(resolve=False) == ("my-sidecar.local", 9222))
    # The enforcement path (create_session, resolve=True) must attempt DNS and,
    # failing, refuse rather than connect.
    _checkpoints.clear()
    try:
        p.create_session("task-dns")
        check("create_session() with unresolvable hostname refuses", False)
    except ValueError as e:
        check("create_session() with unresolvable hostname refuses (no connect)",
              any("getaddrinfo-BLOCKED" in str(c) for c in _checkpoints), str(e))
    except Exception as e:
        check("create_session() with unresolvable hostname refuses", False, repr(e))
finally:
    socket.getaddrinfo = _orig_getaddrinfo2

import re
yaml_text = open(os.path.join(PLUGIN_DIR, "plugin.yaml")).read()
check("plugin.yaml: kind: backend", re.search(r"^kind:\s*backend\s*$", yaml_text, re.M) is not None)
check("plugin.yaml: provides_browser_providers lists local-sidecar",
      re.search(r"provides_browser_providers:.*?-\s*local-sidecar", yaml_text, re.S) is not None)
check("plugin.yaml: has name + version + description",
      re.search(r"^name:", yaml_text, re.M) and re.search(r"^version:", yaml_text, re.M) and re.search(r"^description:", yaml_text, re.M))

server.shutdown()
print("=" * 70)
print(f"RESULT: {PASS} passed, {FAIL} failed")
print("=" * 70)
sys.exit(1 if FAIL else 0)
