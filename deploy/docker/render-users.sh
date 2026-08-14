#!/bin/bash
# Render a timeplusd users.d override from TIMEPLUS_PASSWORD. Shared by the
# `db` and `allinone` entrypoints. When TIMEPLUS_PASSWORD is unset (a bare
# dev container), nothing is rendered and the base image's open `default`
# user is kept.
set -euo pipefail

if [ -n "${TIMEPLUS_PASSWORD:-}" ]; then
  hash=$(printf %s "$TIMEPLUS_PASSWORD" | sha256sum | awk '{print $1}')
  cat > /etc/timeplusd-server/users.d/tpk-users.yaml <<EOF
users:
    tpk:
        password_sha256_hex: ${hash}
        profile: default
        quota: default
        networks:
            ip: "::/0"
    default:
        # The base users.yaml sets a plaintext \`password: ''\` for \`default\`;
        # YAML config merging unions sibling keys rather than replacing them,
        # so without an explicit removal timeplusd fails to start ("More than
        # one field of 'password', 'password_sha256_hex', ... are used").
        # '@replace'/'@remove' are the YAML equivalents of ClickHouse/timeplusd
        # XML config merge attributes.
        password:
            '@remove': '1'
        password_sha256_hex: ${hash}
EOF
fi
