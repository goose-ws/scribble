#!/bin/bash
set -e

# Default PUID/PGID to 0 if not set
PUID=${PUID:-0}
PGID=${PGID:-0}

# Create a group and user
if ! getent group appgroup >/dev/null; then
    groupadd -g "${PGID}" appgroup
fi

if ! getent passwd appuser >/dev/null; then
    useradd --shell /bin/bash -u "${PUID}" -g "${PGID}" -m appuser
fi

# Set ownership of the app directory
chown -R appuser:appgroup /app

# Drop privileges and execute the main command (CMD)
# The "$@" passes along any arguments from the Docker CMD
exec gosu appuser "${@}"
