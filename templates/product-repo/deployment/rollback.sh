#!/bin/bash
# Quick rollback to previous release
# Usage: ./rollback.sh [release_name]

set -e

DEPLOY_DIR="/opt/app"
RELEASES_DIR="$DEPLOY_DIR/releases"

echo "=== Rollback Script ==="

# Get current release
CURRENT=$(readlink -f "$DEPLOY_DIR/current" 2>/dev/null || echo "")
CURRENT_NAME=$(basename "$CURRENT" 2>/dev/null || echo "none")
echo "Current release: $CURRENT_NAME"

# List available releases
echo ""
echo "Available releases:"
ls -lt "$RELEASES_DIR" 2>/dev/null | grep "^d" | awk '{print "  " $NF}' || echo "  (none)"
echo ""

# Determine target release
if [ -n "$1" ]; then
  # User specified a release
  TARGET_NAME="$1"
  TARGET="$RELEASES_DIR/$TARGET_NAME"
else
  # Get previous release (second most recent)
  RELEASES=($(ls -t "$RELEASES_DIR" 2>/dev/null))

  if [ ${#RELEASES[@]} -lt 2 ]; then
    echo "ERROR: No previous release to rollback to"
    exit 1
  fi

  # Find the first release that isn't current
  for REL in "${RELEASES[@]}"; do
    if [ "$REL" != "$CURRENT_NAME" ]; then
      TARGET_NAME="$REL"
      TARGET="$RELEASES_DIR/$TARGET_NAME"
      break
    fi
  done
fi

# Validate target
if [ ! -d "$TARGET" ]; then
  echo "ERROR: Release not found: $TARGET"
  exit 1
fi

if [ "$TARGET" = "$CURRENT" ]; then
  echo "ERROR: Cannot rollback to current release"
  exit 1
fi

echo "Rolling back to: $TARGET_NAME"
read -p "Continue? [y/N] " -n 1 -r
echo

if [[ ! $REPLY =~ ^[Yy]$ ]]; then
  echo "Aborted"
  exit 0
fi

# Stop current containers
echo "Stopping current containers..."
if [ -n "$CURRENT" ] && [ -d "$CURRENT" ]; then
  cd "$CURRENT"
  docker compose down --remove-orphans || true
fi

# Start target containers
echo "Starting rollback containers..."
cd "$TARGET"

# Link shared .env if not present
if [ ! -f ".env" ]; then
  ln -sf "$DEPLOY_DIR/shared/.env" .env
fi

docker compose up -d

# Health check
echo "Waiting for health check..."
HEALTHY=false
for i in {1..30}; do
  if curl -sf http://localhost:3001/api/health > /dev/null 2>&1; then
    echo "Health check passed!"
    HEALTHY=true
    break
  fi
  echo "Waiting... ($i/30)"
  sleep 2
done

if [ "$HEALTHY" = false ]; then
  echo "WARNING: Health check failed after rollback!"
  echo "Containers are running but may not be healthy."
  echo "Check logs: docker compose logs"
fi

# Update symlink
ln -sfn "$TARGET" "$DEPLOY_DIR/current"

echo ""
echo "=== Rollback Complete ==="
echo "Previous: $CURRENT_NAME"
echo "Current:  $TARGET_NAME"
echo ""
