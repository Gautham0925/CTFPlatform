#!/usr/bin/env bash
# TEST ONLY: create a self-signed certificate for nginx (Profile B/C trials).
# Production: use Let's Encrypt (webroot /var/www/acme) or your CA.
# Usage: deploy/scripts/selfsigned-cert.sh ctf.example.org
set -euo pipefail
domain=${1:?usage: selfsigned-cert.sh <domain>}
dir="$(cd "$(dirname "$0")/.." && pwd)/nginx/certs"
mkdir -p "$dir"; umask 077
openssl req -x509 -newkey rsa:2048 -nodes -days 30 -subj "/CN=$domain" \
  -addext "subjectAltName=DNS:$domain" \
  -keyout "$dir/privkey.pem" -out "$dir/fullchain.pem" 2>/dev/null
chmod 644 "$dir/fullchain.pem"
echo "wrote $dir/fullchain.pem and privkey.pem (self-signed, 30 days)"
