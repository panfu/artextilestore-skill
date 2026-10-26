#!/usr/bin/env bash
# Install the artextilestore skill for Claude Code / ZCode / Codex.
#
# Works on ANY machine — no ~/ar workspace required:
#   • full mode  : ~/ar/chatbot checkout with .venv exists → use it
#                  (enables --internal-view with your .env credentials)
#   • standalone : otherwise create ~/.config/artex/venv and run against
#                  the vendored ar_chatbot subset shipped in this repo
#                  (customer mode: artex login + --as only)
#
#   ~/.agents/skills/artextilestore   → this repo   (ZCode)
#   ~/.claude/skills/artextilestore   → ~/.agents/… (Claude Code)
#   ~/.codex/skills/artextilestore    → ~/.agents/… (Codex)
#   ~/.local/bin/artex                → wrapper using the resolved python
#
# Usage: bash install.sh [--uninstall]
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL_NAME="artextilestore"
AGENTS_DIR="$HOME/.agents/skills"
ARTEX_HOME="$HOME/.config/artex"
CHATBOT_VENV="$HOME/ar/chatbot/.venv/bin/python"

if [ "${1:-}" = "--uninstall" ]; then
  rm -rf "$AGENTS_DIR/$SKILL_NAME" "$HOME/.claude/skills/$SKILL_NAME" "$HOME/.codex/skills/$SKILL_NAME" "$HOME/.local/bin/artex"
  echo "uninstalled (kept: $ARTEX_HOME/venv and identities — remove manually if desired)."
  exit 0
fi

# ── resolve python ─────────────────────────────────────────────────
PY=""
MODE=""
if [ -x "$CHATBOT_VENV" ] && [ -d "$HOME/ar/chatbot/ar_chatbot" ]; then
  PY="$CHATBOT_VENV"
  MODE="full (~/ar/chatbot)"
elif [ -d "$SRC/vendor/ar_chatbot" ]; then
  if [ ! -x "$ARTEX_HOME/venv/bin/python" ] || ! "$ARTEX_HOME/venv/bin/python" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'; then
    echo "creating standalone venv at $ARTEX_HOME/venv (python >= 3.10 required) …"
    rm -rf "$ARTEX_HOME/venv"
    mkdir -p "$ARTEX_HOME"
    if command -v uv >/dev/null 2>&1; then
      # uv manages its own CPython — works even when the system python is old
      uv venv --python 3.12 "$ARTEX_HOME/venv" >/dev/null
      uv pip install --python "$ARTEX_HOME/venv/bin/python" --quiet -r "$SRC/requirements-artex.txt"
    else
      PYBIN=""
      for c in python3.13 python3.12 python3.11 python3.10 python3; do
        if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'; then
          PYBIN="$c"; break
        fi
      done
      if [ -z "$PYBIN" ]; then
        echo "ERROR: no python >= 3.10 found. Install uv (https://docs.astral.sh/uv/) or python3.10+ and retry." >&2
        exit 1
      fi
      "$PYBIN" -m venv "$ARTEX_HOME/venv"
      "$ARTEX_HOME/venv/bin/pip" install --quiet --upgrade pip
      "$ARTEX_HOME/venv/bin/pip" install --quiet -r "$SRC/requirements-artex.txt"
    fi
  fi
  PY="$ARTEX_HOME/venv/bin/python"
  MODE="standalone (vendored)"
else
  echo "ERROR: neither ~/ar/chatbot nor ./vendor/ar_chatbot is available." >&2
  exit 1
fi
echo "python : $PY [$MODE]"

# ── links ──────────────────────────────────────────────────────────
link() {
  local target="$1" dest="$2"
  if [ -L "$dest" ] || [ -e "$dest" ]; then
    rm -rf "$dest"
  fi
  ln -s "$target" "$dest"
  echo "linked: $dest -> $target"
}

mkdir -p "$AGENTS_DIR" "$HOME/.claude/skills" "$HOME/.codex/skills"
link "$SRC" "$AGENTS_DIR/$SKILL_NAME"
link "$AGENTS_DIR/$SKILL_NAME" "$HOME/.claude/skills/$SKILL_NAME"
link "$AGENTS_DIR/$SKILL_NAME" "$HOME/.codex/skills/$SKILL_NAME"

if [ -d "$HOME/.local/bin" ]; then
  cat > "$HOME/.local/bin/artex" <<EOF
#!/usr/bin/env bash
exec "$PY" "$AGENTS_DIR/$SKILL_NAME/artex.py" "\$@"
EOF
  chmod +x "$HOME/.local/bin/artex"
  echo "installed: ~/.local/bin/artex"
fi

echo "done [$MODE]. verify with: $PY $AGENTS_DIR/$SKILL_NAME/artex.py doctor"
echo "customer mode start here: artex login --bridge <bridge-url>"
echo "note: start a NEW agent session for the skill to be picked up."
