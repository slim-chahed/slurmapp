#!/bin/bash
# Build the custom DinD terminal image on the VM
# Run this on the VM as a user with docker permissions

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

echo "Building dind-terminal image..."
docker build -t dind-terminal -f Dockerfile.dind .

echo "Done. Image built successfully."
