#!/bin/sh
set -e

# Start dockerd in background
/usr/local/bin/dockerd-entrypoint.sh &

# Wait for Docker daemon to be ready
sleep 2

# Run shell in infinite loop - exit just respawns
while true; do
    /bin/sh || true
done
