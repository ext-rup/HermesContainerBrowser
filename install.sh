#!/usr/bin/env bash
# Install the local-sidecar browser plugin into Hermes and select it as the
# browser backend. Run from the hermes-local-browser directory (or set
# HERMES_LOCAL_BROWSER_DIR). Idempotent.
#
#   1. Copies plugin/browser-local-sidecar/ into $HERMES_HOME/plugins/browser/
#   2. Enables the plugin in config.yaml (opt-in allow-list)
#   3. Sets browser.cloud_provider: local-sidecar (the Capabilities selection)
#   4. Checks the sidecar container is up; prints the exact build/run commands
#      if it isn't.
#
# Works with or without the `hermes` CLI on PATH (falls back to a surgical,
# YAML-stdlib-only config editor that backs up and validates the file).
set -euo pipefail

cd "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC_DIR="${HERMES_LOCAL_BROWSER_DIR:-.}"
PLUGIN_SRC="$SRC_DIR/plugin/browser-local-sidecar"
IMAGE_NAME="${HERMES_BROWSER_IMAGE:-hermes-local-browser:2026.914.1325}"
CONTAINER_NAME="${HERMES_BROWSER_CONTAINER:-hermes-browser}"
PUBLISH_PORT="${HERMES_BROWSER_PORT:-9222}"

for f in plugin.yaml __init__.py provider.py; do
  [[ -f "$PLUGIN_SRC/$f" ]] || { echo "error: $PLUGIN_SRC/$f not found (run from the hermes-local-browser dir, or set HERMES_LOCAL_BROWSER_DIR)" >&2; exit 1; }
done

HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
DEST="$HERMES_HOME/plugins/browser/browser-local-sidecar"
CFG="$HERMES_HOME/config.yaml"
PYBIN="$(command -v python3 2>/dev/null || command -v python 2>/dev/null || true)"

# --- 1. copy the plugin ------------------------------------------------------
mkdir -p "$DEST"
cp "$PLUGIN_SRC/plugin.yaml" "$PLUGIN_SRC/__init__.py" "$PLUGIN_SRC/provider.py" "$DEST/"
rm -rf "$DEST/__pycache__"
echo "✓ plugin copied to $DEST"

# --- 2+3. enable plugin + select provider (CLI first, config editor second) --
if command -v hermes >/dev/null 2>&1; then
  if hermes plugins enable browser/browser-local-sidecar >/dev/null 2>&1; then
    echo "✓ plugin enabled (hermes plugins enable)"
  else
    echo "warning: hermes plugins enable failed — will edit config.yaml directly" >&2
  fi
  if hermes config set browser.cloud_provider local-sidecar >/dev/null 2>&1; then
    echo "✓ browser.cloud_provider: local-sidecar (hermes config set)"
  else
    echo "warning: hermes config set failed — will edit config.yaml directly" >&2
  fi
else
  [[ -n "$PYBIN" ]] || { echo "error: no hermes CLI and no python3 — enable manually:" >&2
    echo "  hermes plugins enable browser/browser-local-sidecar" >&2
    echo "  hermes config set browser.cloud_provider local-sidecar" >&2; exit 1; }
  HERMES_CONFIG_PATH="$CFG" "$PYBIN" - <<'PYEOF'
import os, re, sys, shutil

path = os.environ["HERMES_CONFIG_PATH"]
key = "browser/browser-local-sidecar"
backup = path + ".bak-install"

text = open(path).read() if os.path.exists(path) else ""
had_text = bool(text)

def is_top(l):
    return bool(l) and l[0] not in " \t"

def section_lines(lines, header_re):
    """(start, end) indices of a top-level section, or (None, None)."""
    for i, l in enumerate(lines):
        if is_top(l) and re.match(header_re + r":[ \t]*(#.*)?$", l.strip()):
            end = len(lines)
            for j in range(i + 1, len(lines)):
                if is_top(lines[j]):
                    end = j
                    break
            return i, end
    return None, None

def edit(text):
    if not text.strip():
        return (f"plugins:\n  enabled:\n    - {key}\n"
                f"browser:\n  cloud_provider: local-sidecar\n")
    lines = text.splitlines(keepends=True)

    # unroll empty flow-mapping headers (`plugins: {}` → `plugins:`) so the
    # block-style section edits below can nest children under the bare key.
    for i, l in enumerate(lines):
        if is_top(l) and re.match(r"(plugins|browser):[ \t]*\{\s*\}[ \t]*(#.*)?$",
                                  l.strip()):
            lines[i] = l.strip().split(":")[0].strip() + ":\n"

    # ---- plugins.enabled ------------------------------------------------
    pstart, pend = section_lines(lines, r"plugins")
    if pstart is None:
        if not lines or not lines[-1].endswith("\n"):
            lines.append("\n")
        lines += [f"plugins:\n", f"  enabled:\n", f"    - {key}\n"]
    else:
        eidx = None
        for j in range(pstart + 1, pend):
            if re.match(r"[ \t]+enabled:[ \t]*(#.*)?$", lines[j]):
                eidx = j
                break
        if eidx is None:
            lines[pstart + 1:pstart + 1] = [f"  enabled:\n", f"    - {key}\n"]
        else:
            l = lines[eidx]
            m = re.match(r"([ \t]+)enabled:[ \t]*\[([^\]]*)\]", l)
            if m:
                items = [x.strip() for x in m.group(2).split(",") if x.strip()]
                if key not in items:
                    items.append(key)
                indent = m.group(1)
                lines[eidx] = f"{indent}enabled: [{', '.join(items)}]\n"
            else:
                indent = re.match(r"([ \t]+)", l).group(1)
                item_indent = indent + "  "
                # find the block of items below this line
                j = eidx + 1
                while j < pend and re.match(f"{re.escape(item_indent)}-[ \t]", lines[j]):
                    if lines[j].strip() == f"- {key}":
                        break
                    j += 1
                else:
                    # insert after the last item (or right under 'enabled:')
                    k = eidx + 1
                    while k < pend and (
                        re.match(f"{re.escape(item_indent)}-[ \t]", lines[k])
                        or (lines[k].strip() == "" and k + 1 < pend
                            and re.match(r"[ \t]+", lines[k + 1]))
                    ):
                        k += 1
                    lines.insert(k, f"{item_indent}- {key}\n")
        # best effort: remove from plugins.disabled if present
        didx = None
        pstart, pend = section_lines(lines, r"plugins")
        for j in range(pstart + 1, pend):
            if re.match(r"[ \t]+disabled:[ \t]*(#.*)?$", lines[j]):
                didx = j
                break
        if didx is not None:
            for j in range(didx + 1, pend):
                if lines[j].strip() == f"- {key}":
                    del lines[j]
                    break
    # ---- browser.cloud_provider -----------------------------------------
    bstart, bend = section_lines(lines, r"browser")
    if bstart is None:
        if not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        lines += ["browser:\n", "  cloud_provider: local-sidecar\n"]
    else:
        if re.match(r"browser:[ \t]*\{[^\}]*\}", lines[bstart]):
            lines[bstart] = "browser:\n  cloud_provider: local-sidecar\n"
        else:
            ci = None
            for j in range(bstart + 1, bend):
                if re.match(r"[ \t]+cloud_provider:[ \t]*(#.*)?$", lines[j]):
                    ci = j
                    break
            if ci is not None:
                lines[ci] = re.sub(r"cloud_provider:.*", "cloud_provider: local-sidecar", lines[ci], count=1)
            else:
                lines.insert(bstart + 1, "  cloud_provider: local-sidecar\n")
    return "".join(lines)

new = edit(text)

# ---- verify: parse with PyYAML when available; else confirm both lines ----
ok = True
if had_text and new != text:
    shutil.copyfile(path, backup)
try:
    import yaml  # type: ignore
    data = yaml.safe_load(new) or {}
    ok = (
        isinstance(data, dict)
        and isinstance(data.get("plugins"), dict)
        and key in (data["plugins"].get("enabled") or [])
        and isinstance(data.get("browser"), dict)
        and data["browser"].get("cloud_provider") == "local-sidecar"
    )
except ImportError:
    # No PyYAML: structural checks that still catch the bad cases, including
    # the duplicate top-level key failure mode (the original bug).
    dup = (
        len(re.findall(r"^plugins:[ \t]*", new, re.M)) > 1
        or len(re.findall(r"^browser:[ \t]*", new, re.M)) > 1
    )
    enabled_ok = re.search(
        r"^[ \t]+enabled:.*" + re.escape(key)
        + r"|^[ \t]+-[ \t]*" + re.escape(key) + r"[ \t]*$", new, re.M) is not None
    cp_ok = re.search(r"^[ \t]+cloud_provider:[ \t]*local-sidecar[ \t]*(#.*)?$",
                      new, re.M) is not None
    ok = enabled_ok and cp_ok and not dup
except Exception as e:
    ok = False
    print(f"warning: config parse check failed ({e!r}) — keeping backup", file=sys.stderr)
if not ok:
    if os.path.exists(backup):
        shutil.move(backup, path)
    print("error: config edit did not verify — manual fallback:\n"
          "  hermes plugins enable browser/browser-local-sidecar\n"
          "  hermes config set browser.cloud_provider local-sidecar", file=sys.stderr)
    sys.exit(1)

open(path, "w").write(new)
print(f"✓ config.yaml updated ({path})" + (f" [backup: {backup}]" if os.path.exists(backup) else ""))
PYEOF
  echo "✓ plugin enabled + provider selected (config.yaml edit)"
fi

# --- 4. check the sidecar container ------------------------------------------
cdp_url="http://127.0.0.1:${PUBLISH_PORT}"
if curl -fsS --max-time 3 "$cdp_url/json/version" >/dev/null 2>&1; then
  echo "✓ sidecar is up at $cdp_url"
else
  echo
  echo "⚠ sidecar not reachable at $cdp_url"
  if command -v container >/dev/null 2>&1; then
    if container ps -a 2>/dev/null | grep -q "$CONTAINER_NAME"; then
      echo "   container '$CONTAINER_NAME' exists — start it:  container start $CONTAINER_NAME"
    else
      echo "   build & start it (from $SRC_DIR):"
      echo "     container build --pull -t $IMAGE_NAME ."
      echo "     container run -d --name $CONTAINER_NAME --init --memory 2G --cpus 2 --shm-size 512M -p 127.0.0.1:${PUBLISH_PORT}:${PUBLISH_PORT} $IMAGE_NAME"
    fi
  else
    echo "   no 'container' CLI on PATH — start the sidecar manually (see README.md)"
  fi
fi

echo
echo "done. Start a NEW Hermes session (provider selection is cached per process)."
echo "Your browser tools now drive a local, ad-free Chromium in an isolated Apple Container."
