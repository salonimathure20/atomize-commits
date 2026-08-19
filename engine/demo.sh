#!/usr/bin/env bash
#
# Rebuild the tangled demo repo and show what the tool does with it.
#
#   ./demo.sh                      rebuild and compare both message modes
#   ./demo.sh --commit             ...then actually make the commits
#   DEMO_REPO=~/somewhere ./demo.sh
#
# The demo is two unrelated features — payments and authentication — edited
# into the same file in the same sitting. That is the case this project
# exists for.

set -euo pipefail

DEMO_REPO="${DEMO_REPO:-$HOME/Desktop/hardpractice}"
ENGINE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMMIT_FOR_REAL="${1:-}"

banner() { printf '\n\033[1m%s\033[0m\n' "$*"; }

# --- rebuild ---------------------------------------------------------------

banner "Rebuilding $DEMO_REPO"
rm -rf "$DEMO_REPO"
mkdir -p "$DEMO_REPO/app"
cd "$DEMO_REPO"

git init -q .
git config user.email "demo@example.com"
git config user.name "Demo"

cat > app/routes.py <<'EOF'
from flask import Flask

app = Flask(__name__)


def health():
    return {"ok": True}


def index():
    return {"service": "demo"}
EOF

git add -A
git commit -qm "initial"
echo "  baseline committed"

# --- make the mess ---------------------------------------------------------

cat > app/routes.py <<'EOF'
from flask import Flask

from .auth import verify_token
from .payments import charge_card

app = Flask(__name__)


def health():
    return {"ok": True}


def index():
    return {"service": "demo"}


def login_route(request):
    token = verify_token(request.headers.get("Authorization"))
    return {"token": token}


def checkout_route(request):
    receipt = charge_card(request.json["card"], request.json["amount"])
    return {"receipt": receipt}
EOF

cat > app/auth.py <<'EOF'
import jwt

AUTH_SECRET = "dev"


def verify_token(header):
    return jwt.decode(header, AUTH_SECRET)
EOF

cat > app/payments.py <<'EOF'
import stripe


def charge_card(card, amount):
    return stripe.Charge.create(card=card, amount=amount)
EOF

cat > app/test_payments.py <<'EOF'
from .payments import charge_card


def test_charge_card():
    assert charge_card("4242", 100)
EOF

echo "  tangled two features into $(git status --short | wc -l | tr -d ' ') files"

# --- run it ----------------------------------------------------------------

cd "$ENGINE"
run() { python3 -m splitter --repo "$DEMO_REPO" "$@"; }

banner "1. How the changes were grouped, and why"
run group

banner "2. Messages WITHOUT a model (structural heuristics)"
SPLITTER_LLM=none run apply --dry-run

banner "3. Model configuration"
run doctor || true

banner "4. Messages WITH your configured model"
run apply --dry-run

if [ "$COMMIT_FOR_REAL" = "--commit" ]; then
  banner "5. Committing for real"
  BEFORE=$(cd "$DEMO_REPO" && shasum app/routes.py | cut -d' ' -f1)
  run apply

  banner "6. History"
  git -C "$DEMO_REPO" log --oneline

  banner "7. Safety check"
  AFTER=$(cd "$DEMO_REPO" && shasum app/routes.py | cut -d' ' -f1)
  if [ "$BEFORE" = "$AFTER" ]; then
    echo "  working tree unchanged — no code was lost or reordered"
  else
    echo "  WARNING: working tree changed. That is a bug; please report it."
  fi
  if git -C "$DEMO_REPO" diff --quiet HEAD; then
    echo "  every change is committed, nothing left behind"
  else
    echo "  some changes were not committed:"
    git -C "$DEMO_REPO" status --short
  fi
else
  banner "Nothing was committed"
  echo "  re-run with --commit to make the commits for real"
fi
