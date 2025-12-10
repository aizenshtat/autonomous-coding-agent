#!/bin/bash
# Bootstrap Script for Long-Horizon Coding Agent
#
# One-click VPS setup that is IDEMPOTENT - safe to run on existing servers
# with other services. Only installs what's missing.
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/OWNER/REPO/main/agent/deployment/bootstrap.sh | \
#     bash -s -- --domain agent.example.com --email admin@example.com
#
# Or download and run:
#   chmod +x bootstrap.sh
#   ./bootstrap.sh --domain agent.example.com --email admin@example.com

set -e

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

log_info() { echo -e "${BLUE}[INFO]${NC} $1"; }
log_success() { echo -e "${GREEN}[OK]${NC} $1"; }
log_warning() { echo -e "${YELLOW}[SKIP]${NC} $1"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1"; exit 1; }

# Parse arguments
DOMAIN=""
EMAIL=""

while [[ $# -gt 0 ]]; do
    case $1 in
        --domain)
            DOMAIN="$2"
            shift 2
            ;;
        --email)
            EMAIL="$2"
            shift 2
            ;;
        -h|--help)
            echo "Usage: $0 --domain <domain> --email <email>"
            echo ""
            echo "Options:"
            echo "  --domain    Domain name for the agent (required, for SSL)"
            echo "  --email     Email for Let's Encrypt notifications (required)"
            echo ""
            echo "Example:"
            echo "  $0 --domain agent.example.com --email admin@example.com"
            exit 0
            ;;
        *)
            log_error "Unknown option: $1"
            ;;
    esac
done

# Validate required arguments
if [ -z "$DOMAIN" ]; then
    log_error "Missing required argument: --domain"
fi

if [ -z "$EMAIL" ]; then
    log_error "Missing required argument: --email"
fi

# Check if running as root
if [ "$EUID" -ne 0 ]; then
    log_error "Please run as root (sudo)"
fi

echo ""
echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}  Claude Code Agent Bootstrap${NC}"
echo -e "${GREEN}========================================${NC}"
echo ""
echo "Domain: $DOMAIN"
echo "Email:  $EMAIL"
echo ""

# Get the directory where this script is located (for accessing other files)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ============================================================================
# INSTALL DEPENDENCIES (IDEMPOTENT)
# ============================================================================

log_info "Checking dependencies..."

# Docker
if command -v docker &> /dev/null; then
    log_warning "Docker already installed ($(docker --version | head -1))"
else
    log_info "Installing Docker..."
    curl -fsSL https://get.docker.com | sh
    systemctl enable docker
    systemctl start docker
    log_success "Docker installed"
fi

# Docker Compose (v2 plugin)
if docker compose version &> /dev/null; then
    log_warning "Docker Compose already installed ($(docker compose version --short))"
else
    log_info "Installing Docker Compose..."
    apt-get update -qq
    apt-get install -y -qq docker-compose-plugin
    log_success "Docker Compose installed"
fi

# Nginx
if command -v nginx &> /dev/null; then
    log_warning "Nginx already installed ($(nginx -v 2>&1))"
else
    log_info "Installing Nginx..."
    apt-get update -qq
    apt-get install -y -qq nginx
    systemctl enable nginx
    systemctl start nginx
    log_success "Nginx installed"
fi

# Certbot
if command -v certbot &> /dev/null; then
    log_warning "Certbot already installed ($(certbot --version 2>&1))"
else
    log_info "Installing Certbot..."
    apt-get update -qq
    apt-get install -y -qq certbot python3-certbot-nginx
    log_success "Certbot installed"
fi

# ============================================================================
# CREATE DIRECTORIES
# ============================================================================

log_info "Creating agent directories..."

mkdir -p /opt/agent/{workspace,secrets,metrics,previews,logs}
chmod 755 /opt/agent
chmod 755 /opt/agent/{workspace,metrics,previews,logs}
chmod 700 /opt/agent/secrets

# Initialize metrics file
echo '{"status": "idle", "timestamp": "'$(date -u +%Y-%m-%dT%H:%M:%SZ)'"}' > /opt/agent/metrics/health.json

log_success "Directories created at /opt/agent/"

# ============================================================================
# CREATE DEPLOY USER
# ============================================================================

if id "deploy" &>/dev/null; then
    log_warning "Deploy user already exists"
else
    log_info "Creating deploy user..."
    useradd -m -s /bin/bash deploy
    log_success "Deploy user created"
fi

# Ensure deploy user is in docker group
if groups deploy | grep -q docker; then
    log_warning "Deploy user already in docker group"
else
    log_info "Adding deploy user to docker group..."
    usermod -aG docker deploy
    log_success "Deploy user added to docker group"
fi

# Set ownership
chown -R deploy:docker /opt/agent

# ============================================================================
# GENERATE SSH KEYPAIR
# ============================================================================

SSH_KEY_PATH="/home/deploy/.ssh/id_ed25519"

if [ -f "$SSH_KEY_PATH" ]; then
    log_warning "SSH keypair already exists"
else
    log_info "Generating SSH keypair for deploy user..."
    mkdir -p /home/deploy/.ssh
    chown deploy:deploy /home/deploy/.ssh
    chmod 700 /home/deploy/.ssh
    sudo -u deploy ssh-keygen -t ed25519 -f "$SSH_KEY_PATH" -N "" -C "deploy@$(hostname)"
    chmod 600 "$SSH_KEY_PATH"
    chmod 644 "${SSH_KEY_PATH}.pub"
    chown -R deploy:deploy /home/deploy/.ssh
    log_success "SSH keypair generated"
fi

# Setup authorized_keys for GitHub Actions to SSH in
if [ ! -f /home/deploy/.ssh/authorized_keys ]; then
    touch /home/deploy/.ssh/authorized_keys
    chmod 600 /home/deploy/.ssh/authorized_keys
    chown deploy:deploy /home/deploy/.ssh/authorized_keys
fi

# ============================================================================
# CONFIGURE NGINX
# ============================================================================

NGINX_CONF="/etc/nginx/sites-available/agent-${DOMAIN}.conf"

if [ -f "$NGINX_CONF" ]; then
    log_warning "Nginx config already exists at $NGINX_CONF"
else
    log_info "Creating Nginx configuration..."

    # Check if template exists in script directory
    TEMPLATE_PATH="$SCRIPT_DIR/nginx/agent.conf.template"

    if [ -f "$TEMPLATE_PATH" ]; then
        # Use template file
        export DOMAIN
        envsubst '${DOMAIN}' < "$TEMPLATE_PATH" > "$NGINX_CONF"
    else
        # Generate inline (for curl | bash usage)
        cat > "$NGINX_CONF" << EOF
server {
    listen 80;
    server_name ${DOMAIN};

    # Allow certbot challenge
    location /.well-known/acme-challenge/ {
        root /var/www/html;
    }

    # Redirect all other HTTP to HTTPS
    location / {
        return 301 https://\$server_name\$request_uri;
    }
}

server {
    listen 443 ssl http2;
    server_name ${DOMAIN};

    # SSL certificates (will be configured by certbot)
    ssl_certificate /etc/letsencrypt/live/${DOMAIN}/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/${DOMAIN}/privkey.pem;

    # SSL settings
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_prefer_server_ciphers on;
    ssl_ciphers ECDHE-ECDSA-AES128-GCM-SHA256:ECDHE-RSA-AES128-GCM-SHA256:ECDHE-ECDSA-AES256-GCM-SHA384:ECDHE-RSA-AES256-GCM-SHA384;

    # Health check endpoint
    location /health {
        access_log off;
        return 200 "OK\n";
        add_header Content-Type text/plain;
    }

    # Metrics endpoint (JSON)
    location /metrics {
        alias /opt/agent/metrics/;
        autoindex off;
        add_header Content-Type application/json;
    }

    # Preview deployments
    location /previews/ {
        alias /opt/agent/previews/;
        autoindex on;
        try_files \$uri \$uri/ \$uri/index.html =404;
    }

    # Default - return 404
    location / {
        return 404;
    }
}
EOF
    fi

    # Enable site
    ln -sf "$NGINX_CONF" /etc/nginx/sites-enabled/

    log_success "Nginx configuration created"
fi

# ============================================================================
# OBTAIN SSL CERTIFICATE
# ============================================================================

if [ -d "/etc/letsencrypt/live/${DOMAIN}" ]; then
    log_warning "SSL certificate already exists for ${DOMAIN}"
else
    log_info "Obtaining SSL certificate for ${DOMAIN}..."

    # Temporarily disable the HTTPS server block (cert doesn't exist yet)
    # Create a minimal HTTP-only config for certbot
    cat > /tmp/certbot-temp.conf << EOF
server {
    listen 80;
    server_name ${DOMAIN};

    location /.well-known/acme-challenge/ {
        root /var/www/html;
    }

    location / {
        return 301 https://\$server_name\$request_uri;
    }
}
EOF

    # Backup and replace config temporarily
    cp "$NGINX_CONF" "${NGINX_CONF}.bak"
    cp /tmp/certbot-temp.conf "$NGINX_CONF"
    nginx -t && systemctl reload nginx

    # Obtain certificate
    certbot certonly \
        --webroot \
        --webroot-path /var/www/html \
        -d "$DOMAIN" \
        --non-interactive \
        --agree-tos \
        --email "$EMAIL" \
        --no-eff-email

    # Restore full config
    cp "${NGINX_CONF}.bak" "$NGINX_CONF"
    rm -f "${NGINX_CONF}.bak" /tmp/certbot-temp.conf

    log_success "SSL certificate obtained"
fi

# Test and reload nginx
log_info "Testing Nginx configuration..."
nginx -t
systemctl reload nginx
log_success "Nginx configured and running"

# ============================================================================
# CONFIGURE FIREWALL (IF UFW IS INSTALLED)
# ============================================================================

if command -v ufw &> /dev/null; then
    log_info "Configuring firewall..."
    ufw allow 22/tcp comment 'SSH' 2>/dev/null || true
    ufw allow 80/tcp comment 'HTTP' 2>/dev/null || true
    ufw allow 443/tcp comment 'HTTPS' 2>/dev/null || true
    log_success "Firewall rules added"
else
    log_warning "UFW not installed, skipping firewall configuration"
fi

# ============================================================================
# INSTALL SYSTEMD SERVICE
# ============================================================================

log_info "Installing systemd service..."

cat > /etc/systemd/system/claude-agent.service << 'EOF'
[Unit]
Description=Claude Code Agent
After=docker.service
Requires=docker.service

[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory=/opt/agent
ExecStart=/usr/bin/docker compose up -d
ExecStop=/usr/bin/docker compose down
User=deploy
Group=docker

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable claude-agent.service

log_success "Systemd service installed"

# ============================================================================
# CREATE DOCKER COMPOSE FILE
# ============================================================================

log_info "Creating Docker Compose configuration..."

cat > /opt/agent/docker-compose.yml << 'EOF'
services:
  agent:
    image: claude-code-agent:latest
    container_name: claude-code-agent
    restart: unless-stopped
    environment:
      - CLAUDE_CODE_OAUTH_TOKEN_FILE=/app/secrets/claude_oauth_token
      - ANTHROPIC_API_KEY_FILE=/app/secrets/anthropic_api_key
      - GITHUB_TOKEN_FILE=/app/secrets/github_token
      - METRICS_FILE=/app/metrics/health.json
    volumes:
      - /opt/agent/workspace:/app/workspace
      - /opt/agent/secrets:/app/secrets:ro
      - /opt/agent/metrics:/app/metrics
      - /opt/agent/previews:/app/previews
    healthcheck:
      test: ["CMD", "cat", "/app/metrics/health.json"]
      interval: 30s
      timeout: 10s
      retries: 3
EOF

chown deploy:docker /opt/agent/docker-compose.yml

log_success "Docker Compose configuration created"

# ============================================================================
# SUMMARY
# ============================================================================

echo ""
echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}  Bootstrap Complete!${NC}"
echo -e "${GREEN}========================================${NC}"
echo ""
echo "Agent is configured at: https://${DOMAIN}"
echo ""
echo -e "${YELLOW}GitHub Secrets to Configure:${NC}"
echo ""
echo "  VPS_HOST:           ${DOMAIN}"
echo "  VPS_USER:           deploy"
echo ""
echo -e "${YELLOW}VPS_SSH_KEY (copy the entire private key below):${NC}"
echo ""
cat "$SSH_KEY_PATH"
echo ""
echo -e "${YELLOW}Also add these secrets:${NC}"
echo "  ANTHROPIC_API_KEY:    Your Anthropic API key (sk-ant-...)"
echo "    OR"
echo "  CLAUDE_OAUTH_TOKEN:   Your Claude OAuth token"
echo ""
echo "  AGENT_GITHUB_TOKEN:   GitHub PAT for agent operations"
echo ""
echo -e "${BLUE}Next Steps:${NC}"
echo "1. Add the above secrets to your GitHub repository"
echo "2. Push to main branch to trigger deployment"
echo "3. Create an issue and add a rocket emoji reaction to start a build"
echo ""
echo -e "${GREEN}Endpoints:${NC}"
echo "  Health:   https://${DOMAIN}/health"
echo "  Metrics:  https://${DOMAIN}/metrics/health.json"
echo "  Previews: https://${DOMAIN}/previews/"
echo ""
