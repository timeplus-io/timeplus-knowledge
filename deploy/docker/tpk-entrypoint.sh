#!/bin/bash
# DB container entrypoint: render the users.d override from TIMEPLUS_PASSWORD,
# then hand off to the base image's entrypoint (timeplusd as PID 1). Used by
# the `db` image target and, historically, the all-in-one image.
set -euo pipefail

/usr/local/bin/render-users.sh
exec /entrypoint.sh "$@"
