#!/usr/bin/env bash
# Install the artextilestore skill for Claude Code / ZCode / Codex.
#
# Source of truth: this repo (~/ar/artextilestore-skill).
#   ~/.agents/skills/artextilestore   → this repo   (ZCode)
#   ~/.claude/skills/artextilestore   → ~/.agents/… (Claude Code)
#   ~/.codex/skills/artextilestore    → ~/.agents/… (Codex)
#   ~/.local/bin/artex                → wrapper using the chatbot venv python
#
# Prerequisite: ~/ar/chatbot checkout with .venv (see llms.txt).
# Usage: bash install.sh [--uninstall]
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL_NAME="artextilestore"
AGENTS_DIR="$HOME/.agents/skills"
VENV_PY="$HOME/ar/chatbot/.venv/bin/python"

if [ "${1:-}" = "--uninstall" ]; then
  rm -rf "$AGENTS_DIR/$SKILL_NAME" "$HOME/.claude/skills/$SKILL_NAME" "$HOME/.codex/skills/$SKILL_NAME" "$HOME/.local/bin/artex"
  echo "uninstalled."
  exit 0
fi

if [ ! -d "$HOME/ar/chatbot/ar_chatbot" ]; then
  echo "ERROR: ~/ar/chatbot (ar_chatbot package) not found — see llms.txt prerequisites." >&2
  exit 1
fi
if [ ! -x "$VENV_PY" ]; then
  echo "ERROR: $VENV_PY missing. Run: cd ~/ar/chatbot && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt" >&2
  exit 1
fi

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
exec "$VENV_PY" "$AGENTS_DIR/$SKILL_NAME/artex.py" "\$@"
EOF
  chmod +x "$HOME/.local/bin/artex"
  echo "installed: ~/.local/bin/artex"
fi

echo "done. verify with: $VENV_PY $AGENTS_DIR/$SKILL_NAME/artex.py doctor"
echo "note: start a NEW agent session for the skill to be picked up."
