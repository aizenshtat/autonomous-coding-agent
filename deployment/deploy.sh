#!/usr/bin/env bash

set -e

# Deploy script for Autonomous Coding Agent
# Builds Docker image and prepares environment for agent execution

# Colors
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

# Paths
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

log_step() { echo ""; echo -e "${YELLOW}$1${NC}"; }
log_success() { echo -e "${GREEN}✓ $1${NC}"; }
log_error() { echo -e "${RED}✗ $1${NC}"; }

# ============================================================================
# MAIN DEPLOYMENT
# ============================================================================

log_step "🚀 Deploying Autonomous Coding Agent"
echo "Project directory: $PROJECT_DIR"

# Create required directories
log_step "Creating directories..."
sudo mkdir -p /opt/agent/{workspace,metrics,secrets,data}
sudo chown -R $USER:$USER /opt/agent
log_success "Directories created"

# Build Docker image
log_step "Building Docker image..."
cd "$PROJECT_DIR"

docker build -f Dockerfile.vps -t claude-code-agent:latest .
log_success "Docker image built"

# Stop any existing container (but don't fail if none exists)
log_step "Stopping existing container (if any)..."
docker stop claude-code-agent 2>/dev/null || true
docker rm claude-code-agent 2>/dev/null || true
log_success "Cleanup complete"

# Initialize metrics file
log_step "Initializing metrics..."
echo '{}' > /opt/agent/metrics/health.json
log_success "Metrics initialized"

# Verify secrets exist
log_step "Verifying secrets..."
if [ -f "/opt/agent/secrets/anthropic_api_key" ]; then
    echo "  ✓ Anthropic API key found"
elif [ -f "/opt/agent/secrets/claude_oauth_token" ]; then
    echo "  ✓ Claude OAuth token found"
else
    log_error "No authentication credentials found!"
    echo "  Please add one of:"
    echo "    /opt/agent/secrets/anthropic_api_key"
    echo "    /opt/agent/secrets/claude_oauth_token"
    exit 1
fi

if [ -f "/opt/agent/secrets/github_token" ]; then
    echo "  ✓ GitHub token found"
else
    log_error "GitHub token not found!"
    echo "  Please add: /opt/agent/secrets/github_token"
    exit 1
fi

log_success "Secrets verified"

# Print summary
echo ""
echo "============================================"
echo -e "${GREEN}Deployment Complete!${NC}"
echo "============================================"
echo ""
echo "Docker image: claude-code-agent:latest"
echo "Secrets dir:  /opt/agent/secrets/"
echo "Workspace:    /opt/agent/workspace/"
echo "Metrics:      /opt/agent/metrics/health.json"
echo ""
echo "The agent will be started automatically when a GitHub"
echo "issue is approved with 🚀 reaction."
echo ""
echo "To manually test the container:"
echo "  docker run --rm -it \\"
echo "    -v /opt/agent/secrets:/app/secrets:ro \\"
echo "    -v /opt/agent/workspace:/app/workspace \\"
echo "    -v /opt/agent/metrics:/app/metrics \\"
echo "    -e AGENT_PAYLOAD='{\"mode\":\"test\"}' \\"
echo "    claude-code-agent:latest"
echo ""
