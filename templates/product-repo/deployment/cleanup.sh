#!/bin/bash
# Cleanup script - removes old releases and Docker artifacts
# Run via cron daily or manually
# Usage: ./cleanup.sh [keep_count]

set -e

DEPLOY_DIR="/opt/app"
RELEASES_DIR="$DEPLOY_DIR/releases"
KEEP_RELEASES=${1:-3}  # Keep last N releases for rollback

echo "=== Cleanup Started at $(date) ==="

# 1. Remove old releases (keep last N)
echo "Cleaning old releases (keeping last $KEEP_RELEASES)..."
if [ -d "$RELEASES_DIR" ]; then
  cd "$RELEASES_DIR"
  RELEASE_COUNT=$(ls -d */ 2>/dev/null | wc -l || echo 0)

  if [ "$RELEASE_COUNT" -gt "$KEEP_RELEASES" ]; then
    # Get current release to avoid deleting it
    CURRENT=$(readlink -f "$DEPLOY_DIR/current" 2>/dev/null || echo "")
    CURRENT_NAME=$(basename "$CURRENT" 2>/dev/null || echo "")

    # List releases sorted by name (oldest first), skip current and keep last N
    ls -d */ 2>/dev/null | head -n -$KEEP_RELEASES | while read DIR; do
      DIR_NAME=$(basename "$DIR")
      if [ "$DIR_NAME" != "$CURRENT_NAME" ]; then
        echo "Removing old release: $DIR_NAME"
        rm -rf "$DIR"
      fi
    done
  else
    echo "Only $RELEASE_COUNT releases, nothing to clean"
  fi
else
  echo "No releases directory found"
fi

# 2. Remove dangling Docker images
echo "Removing dangling Docker images..."
docker image prune -f 2>/dev/null || true

# 3. Remove unused Docker volumes (except named ones used by releases)
echo "Removing unused Docker volumes..."
docker volume prune -f 2>/dev/null || true

# 4. Remove old Docker build cache (keep 5GB)
echo "Pruning Docker build cache..."
docker builder prune -f --keep-storage 5GB 2>/dev/null || true

# 5. Remove stopped containers
echo "Removing stopped containers..."
docker container prune -f 2>/dev/null || true

# 6. Remove unused networks
echo "Removing unused networks..."
docker network prune -f 2>/dev/null || true

# 7. Clean up old log files (older than 30 days)
echo "Cleaning old log files..."
find /var/log/app -type f -name "*.log" -mtime +30 -delete 2>/dev/null || true

# 8. Report disk usage
echo ""
echo "=== Disk Usage Report ==="
echo "Root filesystem:"
df -h / | tail -1

echo ""
echo "Docker disk usage:"
docker system df 2>/dev/null || true

echo ""
echo "Releases directory:"
du -sh "$RELEASES_DIR" 2>/dev/null || echo "N/A"

echo ""
echo "=== Cleanup Complete at $(date) ==="
