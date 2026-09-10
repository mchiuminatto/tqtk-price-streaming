#!/usr/bin/env bash
# Generate the stack's secret files, once per machine.
#
# The Compose file reads every credential from deploy/secrets/ rather than from a variable with a
# default, so there is no password in the repository to leak and none in a container's environment
# to read back with `docker inspect`. The cost is this step: the stack cannot start until the files
# exist, which is the point - a missing secret is a startup failure rather than a silent fallback
# to a well-known default.
#
# Idempotent: an existing secret is left alone, so re-running never invalidates a database that was
# initialised with the current one. Delete a file and re-run to rotate - see deploy/README.md, as
# rotating the Postgres password needs an ALTER USER on an already-initialised volume.
set -euo pipefail

secrets_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/secrets"
mkdir -p "$secrets_dir"

# `name:mode`. The modes differ because the two images read the file as different users, and
# Compose cannot fix that for us: `uid`/`gid`/`mode` on a secret are honoured by Swarm and
# Kubernetes, but a plain `file:` secret is bind-mounted with the host's ownership untouched
# (verified - the fields are silently ignored).
#
#   postgres  0600  its entrypoint reads the file as root, before dropping to the postgres user,
#                   and root is not subject to the mode. Owner-only is therefore free.
#   grafana   0644  its entrypoint runs as uid 472 from the start, so an owner-only file owned by
#                   the host user is unreadable and Grafana crash-loops on "Permission denied".
#
# 0644 means any account on this host can read the Grafana admin password. That is a real
# weakening and it is deliberate: the boundary being defended here is the repository, not a
# multi-tenant host. On a host where local accounts are untrusted, this file is the wrong
# mechanism - use the orchestrator's secret store, where the mode fields actually apply.
for entry in postgres_password:600 grafana_admin_password:644; do
    name="${entry%%:*}"
    mode="${entry##*:}"
    path="$secrets_dir/$name"
    if [ -s "$path" ]; then
        chmod "$mode" "$path"
        echo "kept    $path (mode $mode)"
        continue
    fi
    # -base64 24 is 32 printable characters. `tr -d` drops the newline openssl appends: the
    # consumers strip a trailing newline themselves, but a secret whose file has one is a secret
    # that reads differently depending on who reads it.
    openssl rand -base64 24 | tr -d '\n' > "$path"
    chmod "$mode" "$path"
    echo "created $path (mode $mode)"
done
