#!/bin/bash

# 1. Create directories if they don't exist (Crucial for volume mounts)
mkdir -p /data/input /data/scripts /data/archive

# 2. Handle PUID/PGID
if [ -n "$PUID" ] && [ -n "$PGID" ]; then
    echo "Configuring permissions for PUID:$PUID PGID:$PGID"
    
    # Create group if not exists
    if ! getent group scribble_group > /dev/null; then
        groupadd -g "$PGID" scribble_group
    fi

    # Create user if not exists
    if ! id -u scribble_user > /dev/null 2>&1; then
        useradd -u "$PUID" -g "$PGID" -d /app -s /bin/bash scribble_user
    fi

    # Recursive chown to ensure user can write
    chown -R "$PUID":"$PGID" /data
    chown -R "$PUID":"$PGID" /app

    # [NEW] Check & Run Database Migration
    echo "Checking for database migrations..."
    su scribble_user -c "python3 migrate.py"

    echo "Starting Scribble as scribble_user..."
    # Switch user and run gunicorn
    exec su scribble_user -c "gunicorn --workers 1 --threads 4 --bind 0.0.0.0:13131 app:app"
else
    # [NEW] Check & Run Database Migration
    echo "Checking for database migrations..."
    python3 migrate.py

    echo "Starting Scribble as root (Not Recommended)..."
    exec gunicorn --workers 1 --threads 4 --bind 0.0.0.0:13131 app:app
fi