#!/usr/bin/env bash
# Run on the Oracle VM (Ubuntu). Usage: DOMAIN=app.example.com bash deploy/setup-vm.sh
set -euo pipefail
: "${DOMAIN:?set DOMAIN}"
if ! command -v docker >/dev/null; then curl -fsSL https://get.docker.com | sudo sh; sudo usermod -aG docker "$USER"; fi
# swap so the build survives on small VMs
if [ ! -f /swapfile ]; then sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile && sudo mkswap /swapfile && sudo swapon /swapfile; fi
# Oracle's Ubuntu image blocks 80/443 in iptables by default
sudo iptables -I INPUT 5 -p tcp --dport 80 -j ACCEPT
sudo iptables -I INPUT 5 -p tcp --dport 443 -j ACCEPT
sudo netfilter-persistent save 2>/dev/null || true
cd "$(dirname "$0")"
sudo DOMAIN="$DOMAIN" docker compose -f docker-compose.prod.yml up -d --build
