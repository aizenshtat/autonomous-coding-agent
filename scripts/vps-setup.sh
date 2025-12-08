#!/bin/bash
# VPS Setup Script for Long-Horizon Coding Agent
# Provider-agnostic - tested on Hetzner, DigitalOcean, Linode, Vultr
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/YOUR_REPO/main/scripts/vps-setup.sh | bash
#   # Or download and run:
#   chmod +x vps-setup.sh && ./vps-setup.sh

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}  VPS Setup for Claude Code Agent${NC}"
echo -e "${GREEN}========================================${NC}"
echo ""

# Check if running as root
if [ "$EUID" -ne 0 ]; then
    echo -e "${RED}Please run as root (sudo)${NC}"
    exit 1
fi

# Detect OS
if [ -f /etc/os-release ]; then
    . /etc/os-release
    OS=$ID
else
    echo -e "${RED}Cannot detect OS${NC}"
    exit 1
fi

echo -e "${YELLOW}Detected OS: $OS${NC}"

# Install Docker if not present
if ! command -v docker &> /dev/null; then
    echo -e "${YELLOW}Installing Docker...${NC}"
    curl -fsSL https://get.docker.com -o get-docker.sh
    sh get-docker.sh
    rm get-docker.sh
    systemctl enable docker
    systemctl start docker
    echo -e "${GREEN}Docker installed${NC}"
else
    echo -e "${GREEN}Docker already installed${NC}"
fi

# Install Docker Compose v2 if not present
if ! docker compose version &> /dev/null; then
    echo -e "${YELLOW}Installing Docker Compose...${NC}"
    apt-get update
    apt-get install -y docker-compose-plugin
    echo -e "${GREEN}Docker Compose installed${NC}"
else
    echo -e "${GREEN}Docker Compose already installed${NC}"
fi

# Install Nginx if not present
if ! command -v nginx &> /dev/null; then
    echo -e "${YELLOW}Installing Nginx...${NC}"
    apt-get update
    apt-get install -y nginx
    systemctl enable nginx
    systemctl start nginx
    echo -e "${GREEN}Nginx installed${NC}"
else
    echo -e "${GREEN}Nginx already installed${NC}"
fi

# Install certbot for SSL
if ! command -v certbot &> /dev/null; then
    echo -e "${YELLOW}Installing Certbot...${NC}"
    apt-get install -y certbot python3-certbot-nginx
    echo -e "${GREEN}Certbot installed${NC}"
else
    echo -e "${GREEN}Certbot already installed${NC}"
fi

# Create agent directories
echo -e "${YELLOW}Creating agent directories...${NC}"
mkdir -p /opt/agent/{data,metrics,previews,secrets,logs}
chmod 755 /opt/agent
chmod 755 /opt/agent/{data,metrics,previews,logs}
chmod 700 /opt/agent/secrets

echo -e "${GREEN}Created directory structure:${NC}"
echo "  /opt/agent/"
echo "  ├── data/      - Agent workspace (git repos)"
echo "  ├── metrics/   - Health metrics (JSON)"
echo "  ├── previews/  - Static preview builds"
echo "  ├── secrets/   - API keys (restricted)"
echo "  └── logs/      - Application logs"

# Create deploy user for GitHub Actions SSH access
if ! id "deploy" &>/dev/null; then
    echo -e "${YELLOW}Creating deploy user...${NC}"
    useradd -m -s /bin/bash deploy
    usermod -aG docker deploy

    # Create .ssh directory for deploy user
    mkdir -p /home/deploy/.ssh
    chmod 700 /home/deploy/.ssh
    touch /home/deploy/.ssh/authorized_keys
    chmod 600 /home/deploy/.ssh/authorized_keys
    chown -R deploy:deploy /home/deploy/.ssh

    echo -e "${GREEN}Deploy user created${NC}"
else
    echo -e "${GREEN}Deploy user already exists${NC}"
fi

# Give deploy user access to agent directories
chown -R deploy:docker /opt/agent

# Configure firewall (ufw)
if command -v ufw &> /dev/null; then
    echo -e "${YELLOW}Configuring firewall...${NC}"
    ufw allow 22/tcp comment 'SSH'
    ufw allow 80/tcp comment 'HTTP'
    ufw allow 443/tcp comment 'HTTPS'

    # Enable ufw if not already enabled
    if ! ufw status | grep -q "Status: active"; then
        echo "y" | ufw enable
    fi

    echo -e "${GREEN}Firewall configured${NC}"
fi

# Create placeholder secrets files
echo -e "${YELLOW}Creating placeholder secrets files...${NC}"
if [ ! -f /opt/agent/secrets/anthropic_api_key ]; then
    echo "REPLACE_WITH_YOUR_ANTHROPIC_API_KEY" > /opt/agent/secrets/anthropic_api_key
    chmod 600 /opt/agent/secrets/anthropic_api_key
fi
if [ ! -f /opt/agent/secrets/github_token ]; then
    echo "REPLACE_WITH_YOUR_GITHUB_TOKEN" > /opt/agent/secrets/github_token
    chmod 600 /opt/agent/secrets/github_token
fi
chown -R deploy:deploy /opt/agent/secrets

# Print summary
echo ""
echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}  Setup Complete!${NC}"
echo -e "${GREEN}========================================${NC}"
echo ""
echo -e "${YELLOW}Next steps:${NC}"
echo ""
echo "1. Add your SSH public key for GitHub Actions:"
echo "   echo 'YOUR_PUBLIC_KEY' >> /home/deploy/.ssh/authorized_keys"
echo ""
echo "2. Add your secrets:"
echo "   echo 'sk-ant-...' > /opt/agent/secrets/anthropic_api_key"
echo "   echo 'ghp_...' > /opt/agent/secrets/github_token"
echo ""
echo "3. Configure SSL certificate (replace YOUR_DOMAIN):"
echo "   certbot --nginx -d YOUR_DOMAIN"
echo ""
echo "4. Add Nginx configuration for previews:"
echo "   cp nginx/agent-previews.conf /etc/nginx/sites-available/"
echo "   ln -s /etc/nginx/sites-available/agent-previews.conf /etc/nginx/sites-enabled/"
echo "   nginx -t && systemctl reload nginx"
echo ""
echo "5. Build and test the Docker image:"
echo "   docker build -f Dockerfile.vps -t claude-code-agent:latest ."
echo ""
echo "6. Configure GitHub repository secrets:"
echo "   - VPS_HOST: $(hostname -I | awk '{print $1}')"
echo "   - VPS_SSH_USER: deploy"
echo "   - VPS_SSH_KEY: (your private key)"
echo ""
echo -e "${GREEN}Done!${NC}"
