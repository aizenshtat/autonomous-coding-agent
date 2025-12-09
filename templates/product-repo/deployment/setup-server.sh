#!/bin/bash
# Initial server setup script
# Run once on new server to set up prerequisites
# Usage: ./setup-server.sh <domain>

set -e

DOMAIN=${1:-"app.example.com"}

echo "=== Server Setup for $DOMAIN ==="

# Check if running as root
if [ "$EUID" -ne 0 ]; then
  echo "Please run as root (sudo ./setup-server.sh)"
  exit 1
fi

echo "Installing Docker..."
if ! command -v docker &> /dev/null; then
  curl -fsSL https://get.docker.com | sh
  systemctl enable docker
  systemctl start docker
else
  echo "Docker already installed"
fi

echo "Installing Docker Compose plugin..."
if ! docker compose version &> /dev/null; then
  apt-get update
  apt-get install -y docker-compose-plugin
else
  echo "Docker Compose already installed"
fi

echo "Creating directories..."
mkdir -p /opt/app/releases
mkdir -p /opt/app/shared
mkdir -p /var/log/app

echo "Installing Nginx..."
if ! command -v nginx &> /dev/null; then
  apt-get update
  apt-get install -y nginx
  systemctl enable nginx
else
  echo "Nginx already installed"
fi

echo "Configuring Nginx for $DOMAIN..."
cat > /etc/nginx/sites-available/$DOMAIN << NGINX
server {
    listen 80;
    server_name $DOMAIN;

    location / {
        proxy_pass http://localhost:3000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection 'upgrade';
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_cache_bypass \$http_upgrade;
        proxy_read_timeout 300s;
        proxy_connect_timeout 75s;
    }

    location /api {
        proxy_pass http://localhost:3001;
        proxy_http_version 1.1;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection 'upgrade';
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_cache_bypass \$http_upgrade;
        proxy_read_timeout 300s;
    }

    # Security headers
    add_header X-Frame-Options "SAMEORIGIN" always;
    add_header X-Content-Type-Options "nosniff" always;
    add_header X-XSS-Protection "1; mode=block" always;
}
NGINX

# Enable site
ln -sf /etc/nginx/sites-available/$DOMAIN /etc/nginx/sites-enabled/
rm -f /etc/nginx/sites-enabled/default 2>/dev/null || true

# Test and reload nginx
nginx -t
systemctl reload nginx

echo "Installing Certbot for SSL..."
if ! command -v certbot &> /dev/null; then
  apt-get install -y certbot python3-certbot-nginx
else
  echo "Certbot already installed"
fi

echo "Obtaining SSL certificate for $DOMAIN..."
echo "Make sure DNS is pointing to this server before continuing."
read -p "Press Enter to continue or Ctrl+C to abort..."

certbot --nginx -d $DOMAIN --non-interactive --agree-tos --email admin@$DOMAIN --redirect

echo "Setting up auto-renewal..."
systemctl enable certbot.timer
systemctl start certbot.timer

echo "Creating .env template..."
cat > /opt/app/shared/.env.template << ENV
# Database
DATABASE_URL=postgresql://app:app@localhost:5432/app
POSTGRES_USER=app
POSTGRES_PASSWORD=app
POSTGRES_DB=app

# Application
NODE_ENV=production
CORS_ORIGIN=https://$DOMAIN

# Ports (internal)
WEB_PORT=3000
API_PORT=3001
ENV

echo "Setting up cleanup cron job..."
cat > /etc/cron.d/app-cleanup << CRON
# Clean up old releases daily at 3am
0 3 * * * root /opt/app/cleanup.sh >> /var/log/app/cleanup.log 2>&1
CRON

echo ""
echo "=== Setup Complete ==="
echo ""
echo "Next steps:"
echo "1. Copy .env.template and configure: cp /opt/app/shared/.env.template /opt/app/shared/.env"
echo "2. Deploy your first release: ./deploy.sh"
echo "3. Domain: https://$DOMAIN"
echo ""
