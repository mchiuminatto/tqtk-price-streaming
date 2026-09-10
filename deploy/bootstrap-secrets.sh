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

# Set before anything is created, so nothing is ever briefly wider than it ends up. A redirection
# creates a file at the process umask and `chmod` only tightens it afterwards, which would leave
# each secret readable for the length of its write.
umask 077

secrets_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/secrets"
mkdir -p "$secrets_dir"
# Also correct a directory left behind by an earlier run, which predates the umask above - the
# same keep-and-repair the loop does for files. This is what makes Grafana's 0644 harmless; see
# below.
chmod 700 "$secrets_dir"

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
# Grafana's 0644 would otherwise mean any account on this host can read that password. The 0700 on
# the directory above is what stops it: the container reads the *bind-mounted* file, which the
# Docker daemon mounts as root, so the host directory's mode is never consulted on that path and
# closing it costs Grafana nothing (verified - it starts healthy and authenticates). The file mode
# still has to be 0644 for uid 472; it just no longer has a path to it from another account.
#
# That makes this a single-owner boundary, not a multi-tenant one. On a host where even the owning
# account is shared, a file is the wrong mechanism - use the orchestrator's secret store, where
# the `uid`/`gid`/`mode` fields on a secret actually apply.

for entry in postgres_password:600 grafana_admin_password:644; do
    name="${entry%%:*}"
    mode="${entry##*:}"
    path="$secrets_dir/$name"
    if [ -s "$path" ]; then
        # Only ever widens, given the umask above: 0600 is a no-op, 0644 is Grafana's.
        chmod "$mode" "$path"
        echo "kept    $path (mode $mode)"
        continue
    fi
    # -hex, not -base64: same 24 bytes and the same 192 bits, but an alphabet that survives being
    # put anywhere. base64 emits `+` and `/` in about two thirds of draws, and a `/` in the
    # userinfo of a `postgresql://user:pass@host/db` URI terminates the authority - the connection
    # then fails to parse or quietly targets something else. Nothing here builds such a URI today;
    # the services in tasks 7.1 and 8.1 are where it would bite, in a change far from this line.
    # 48 hex characters is longer, which costs nothing that is never read by eye.
    #
    # `tr -d` drops the newline openssl appends: the consumers strip a trailing newline themselves,
    # but a secret whose file has one is a secret that reads differently depending on who reads it.
    openssl rand -hex 24 | tr -d '\n' > "$path"
    chmod "$mode" "$path"
    echo "created $path (mode $mode)"
done
