#!/bin/bash
# All-in-one entrypoint: run the DB (OSS proton) AND `tpk serve` in one
# container.
#
# Starts the DB in the background (via the base image's entrypoint), waits for
# its SQL HTTP port to answer, then runs the chat server in the foreground. The
# container exits (and the other process is stopped) as soon as either process
# exits, and SIGTERM/SIGINT are forwarded to both so `docker stop` shuts down
# cleanly. Intended for tests / quick local runs; the DB + App compose file is
# the production shape.
set -euo pipefail

# Same user provisioning as the standalone db image.
/usr/local/bin/render-users.sh

# DB (proton) in the background.
/entrypoint.sh &
TP_PID=$!

APP_PID=""
shutdown() {
  [ -n "$APP_PID" ] && kill -TERM "$APP_PID" 2>/dev/null || true
  kill -TERM "$TP_PID" 2>/dev/null || true
}
trap shutdown TERM INT

# Wait for the DB to accept HTTP before starting the app. proton answers
# `/` with 200 once it's up (there is no /ping). Bail early if it dies.
echo "allinone: waiting for proton on :8123 ..."
for _ in $(seq 1 60); do
  if wget -q -O /dev/null http://localhost:8123/ 2>/dev/null \
     || curl -sf http://localhost:8123/ -o /dev/null 2>/dev/null; then
    echo "allinone: proton is up"
    break
  fi
  if ! kill -0 "$TP_PID" 2>/dev/null; then
    echo "allinone: proton exited during startup" >&2
    wait "$TP_PID"
    exit $?
  fi
  sleep 2
done

# App in the foreground (serve also retries the DB connection on its own).
tpk serve --host 0.0.0.0 --port 8000 &
APP_PID=$!

# Exit as soon as either process exits, then stop the other.
wait -n "$TP_PID" "$APP_PID"
status=$?
shutdown
wait 2>/dev/null || true
exit "$status"
