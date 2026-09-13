#!/bin/sh
# Throwaway local pipa server for end-to-end tests of the pipa backend.
#
#   PIPA_DIR=~/src/pipa scripts/pipa-local.sh      # needs `cargo build --release` done in PIPA_DIR
#   . .interact-data/pipa/env.sh                    # CLI creds + INTERACT_* for the pipa backend
#   python -m interact demo                         # deploys the demo page to the local pipa
#
# Everything lives in .interact-data/pipa (server data, isolated HOME for the CLI, a file-based
# credential vault), so your real pipa login and OS keychain are never touched. Dev mode only.
set -eu

ROOT=$(cd "$(dirname "$0")/.." && pwd)
PIPA_DIR=${PIPA_DIR:?set PIPA_DIR to a pipa checkout with release binaries}
PIPA=$PIPA_DIR/target/release/pipa
PIPA_SERVER=$PIPA_DIR/target/release/pipa-server
PORT=${PIPA_PORT:-8090}
SERVER=http://127.0.0.1:$PORT
E=$ROOT/.interact-data/pipa
mkdir -p "$E/home" "$E/data" "$E/pages"

cat > "$E/pages.toml" <<EOF
[server]
addr = "127.0.0.1:$PORT"
public_url = "$SERVER"
data_dir = "$E/data"
pages_dir = "$E/pages"
dev = true
EOF

"$PIPA_SERVER" --config "$E/pages.toml" --dev > "$E/server.log" 2>&1 &
echo "$!" > "$E/server.pid"
for _ in $(seq 1 50); do curl -fsS "$SERVER/health" >/dev/null 2>&1 && break; sleep 0.2; done

export HOME=$E/home
export PIPA_SECRET_GET_CMD="cat $E/refresh.tok"
export PIPA_SECRET_SET_CMD="cat > $E/refresh.tok"
JAR=$E/cookies.txt

if [ ! -s "$E/refresh.tok" ]; then
  # First run: create the admin, then pair the CLI with an automation-scoped device.
  curl -sS -o /dev/null -c "$JAR" -b "$JAR" --data-urlencode username=admin \
    --data-urlencode password=local-dev-only --data-urlencode password_confirm=local-dev-only "$SERVER/setup"
  INIT=$("$PIPA" --headless --json login --no-wait --automation --server "$SERVER" --label interact-e2e)
  CODE=$(printf '%s' "$INIT" | python3 -c 'import json,sys,urllib.parse as u; print(u.parse_qs(u.urlsplit(json.load(sys.stdin)["verify_url"]).query)["code"][0])')
  curl -sS -o /dev/null -b "$JAR" --data-urlencode device_code="$CODE" \
    --data-urlencode label=interact-e2e --data-urlencode scope=automation "$SERVER/cli"
  "$PIPA" --headless --json login --resume >/dev/null
fi

cat > "$E/env.sh" <<EOF
export HOME='$E/home'
export PIPA_SECRET_GET_CMD='cat $E/refresh.tok'
export PIPA_SECRET_SET_CMD='cat > $E/refresh.tok'
export INTERACT_BACKEND=pipa
export INTERACT_PIPA_BIN='$PIPA'
export INTERACT_PIPA_HEADLESS=1
export INTERACT_DATA_DIR='$E/interact'
EOF

echo "pipa-server on $SERVER (pid $(cat "$E/server.pid"), log $E/server.log)"
echo "next:  . $E/env.sh && python -m interact demo"
echo "stop:  kill \$(cat $E/server.pid)"
