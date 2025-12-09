#!/bin/bash
# Zero-downtime deployment script
# Usage: ./deploy.sh [image_tag]

set -e

DEPLOY_DIR="/opt/app"
RELEASES_DIR="$DEPLOY_DIR/releases"
SHARED_DIR="$DEPLOY_DIR/shared"
DEPLOY_ID=$(date +%Y%m%d_%H%M%S)
NEW_DIR="$RELEASES_DIR/$DEPLOY_ID"
IMAGE_TAG=${1:-latest}

echo "=== Deploying $DEPLOY_ID (image: $IMAGE_TAG) ==="

# Ensure directories exist
mkdir -p "$RELEASES_DIR"
mkdir -p "$SHARED_DIR"

# Check for .env file
if [ ! -f "$SHARED_DIR/.env" ]; then
  echo "ERROR: .env file not found at $SHARED_DIR/.env"
  echo "Create it from .env.template first"
  exit 1
fi

# Create release directory
mkdir -p "$NEW_DIR"
cd "$NEW_DIR"

# Copy docker-compose and configs
echo "Setting up release directory..."
cat > docker-compose.yml << 'COMPOSE'
version: '3.8'

services:
  web:
    image: ${WEB_IMAGE:-ghcr.io/repo/web:latest}
    ports:
      - "${WEB_PORT:-3000}:80"
    depends_on:
      - api
    deploy:
      resources:
        limits:
          cpus: '0.5'
          memory: 256M
    restart: unless-stopped

  api:
    image: ${API_IMAGE:-ghcr.io/repo/api:latest}
    ports:
      - "${API_PORT:-3001}:3001"
    environment:
      - DATABASE_URL=${DATABASE_URL}
      - NODE_ENV=production
      - CORS_ORIGIN=${CORS_ORIGIN:-http://localhost:3000}
    depends_on:
      db:
        condition: service_healthy
    deploy:
      resources:
        limits:
          cpus: '0.5'
          memory: 512M
    restart: unless-stopped

  db:
    image: postgres:16-alpine
    environment:
      POSTGRES_USER: ${POSTGRES_USER:-app}
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:-app}
      POSTGRES_DB: ${POSTGRES_DB:-app}
    volumes:
      - ${DEPLOY_DIR}/shared/postgres_data:/var/lib/postgresql/data
    deploy:
      resources:
        limits:
          cpus: '0.5'
          memory: 512M
    command: >
      postgres
      -c shared_buffers=128MB
      -c effective_cache_size=256MB
      -c maintenance_work_mem=64MB
      -c work_mem=4MB
      -c max_connections=50
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U ${POSTGRES_USER:-app} -d ${POSTGRES_DB:-app}"]
      interval: 10s
      timeout: 5s
      retries: 5
    restart: unless-stopped
COMPOSE

# Link shared .env
ln -sf "$SHARED_DIR/.env" .env

# Export variables for docker-compose
export DEPLOY_DIR
export WEB_IMAGE="ghcr.io/repo/web:$IMAGE_TAG"
export API_IMAGE="ghcr.io/repo/api:$IMAGE_TAG"

# Pull new images
echo "Pulling new images..."
docker compose pull web api || true

# Get current release for potential rollback
CURRENT=$(readlink -f "$DEPLOY_DIR/current" 2>/dev/null || echo "")

# Run migrations
echo "Running database migrations..."
docker compose run --rm api npx prisma migrate deploy

# Start new containers
echo "Starting new containers..."
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
  echo "Waiting for API to be ready... ($i/30)"
  sleep 2
done

if [ "$HEALTHY" = false ]; then
  echo "ERROR: Health check failed!"
  echo "Rolling back to previous release..."
  docker compose down

  if [ -n "$CURRENT" ] && [ -d "$CURRENT" ]; then
    cd "$CURRENT"
    docker compose up -d
    echo "Rolled back to $CURRENT"
  fi

  # Clean up failed release
  rm -rf "$NEW_DIR"
  exit 1
fi

# Stop old containers (if any)
if [ -n "$CURRENT" ] && [ -d "$CURRENT" ] && [ "$CURRENT" != "$NEW_DIR" ]; then
  echo "Stopping old containers..."
  cd "$CURRENT"
  docker compose down --remove-orphans || true
fi

# Update current symlink
ln -sfn "$NEW_DIR" "$DEPLOY_DIR/current"

echo ""
echo "=== Deployment Complete ==="
echo "Release: $DEPLOY_ID"
echo "Current: $DEPLOY_DIR/current -> $NEW_DIR"
echo ""
