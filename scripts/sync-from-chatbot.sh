#!/usr/bin/env bash
# Sync the vendored ar_chatbot subset from a chatbot checkout into ./vendor.
#
# The vendored subset makes the skill self-contained on machines WITHOUT the
# ~/ar workspace: no chatbot checkout, no chatbot .env, no system Odoo
# credentials. Only the modules required by OdooSalesTools at runtime are
# copied; package __init__ files are replaced with lightweight shells so the
# heavy agent/lark/db imports of the full chatbot package never trigger.
#
# Usage: bash scripts/sync-from-chatbot.sh [path-to-chatbot-checkout]
#        (default: ~/ar/chatbot; run from the artextilestore-skill repo root)
set -euo pipefail

SRC="${1:-$HOME/ar/chatbot}"
DST="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/vendor"

if [ ! -d "$SRC/ar_chatbot" ]; then
  echo "ERROR: $SRC is not a chatbot checkout (ar_chatbot/ missing)." >&2
  exit 1
fi

MODULES=(
  ar_chatbot/tools/odoo_tools.py
  ar_chatbot/tools/cache.py
  ar_chatbot/tools/factory_code_match.py
  ar_chatbot/tools/portal_client.py
  ar_chatbot/tools/fabric_length.py
  ar_chatbot/auth/models.py
  ar_chatbot/auth/permission_checker.py
  ar_chatbot/messaging/balance.py
  ar_chatbot/scripts/__init__.py
  ar_chatbot/scripts/order.py
  ar_chatbot/scripts/lib/__init__.py
  ar_chatbot/scripts/lib/odoo_client.py
)

rm -rf "$DST"
mkdir -p "$DST/ar_chatbot/tools" "$DST/ar_chatbot/auth" "$DST/ar_chatbot/messaging" "$DST/ar_chatbot/scripts/lib"
for m in "${MODULES[@]}"; do
  cp "$SRC/$m" "$DST/$m"
done

# Lightweight package shells: keep imports from pulling the full chatbot
# agent/lark/db stack.
cat > "$DST/ar_chatbot/__init__.py" <<'EOF'
"""Vendored ar_chatbot subset (runtime modules for the artex CLI only).

Synced from the chatbot repo by scripts/sync-from-chatbot.sh — do not edit
here; edit upstream and re-sync.
"""

__version__ = "vendored"
EOF

cat > "$DST/ar_chatbot/tools/__init__.py" <<'EOF'
"""Vendored tools subset: only OdooSalesTools."""

from ar_chatbot.tools.odoo_tools import OdooSalesTools

__all__ = ["OdooSalesTools"]
EOF

cat > "$DST/ar_chatbot/auth/__init__.py" <<'EOF'
"""Vendored auth subset: binding models + permission checkers only."""

from ar_chatbot.auth.models import OdooUserBinding

__all__ = ["OdooUserBinding"]
EOF

cat > "$DST/ar_chatbot/messaging/__init__.py" <<'EOF'
"""Vendored messaging subset."""

from ar_chatbot.messaging.balance import check_balance

__all__ = ["check_balance"]
EOF

# order.py does a bare `from lib import OdooClient` (script-style import);
# as a package module it must go through the package path.
sed -i.bak 's/^from lib import OdooClient$/from ar_chatbot.scripts.lib import OdooClient/' "$DST/ar_chatbot/scripts/order.py"
rm -f "$DST/ar_chatbot/scripts/order.py.bak"

echo "vendored $(find "$DST" -name '*.py' | wc -l | tr -d ' ') files from $SRC"
echo "review the diff before committing (business logic mirrors the chatbot repo)."
