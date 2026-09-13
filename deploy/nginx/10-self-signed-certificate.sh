#!/bin/sh
set -eu

certificate=/etc/nginx/tls/fullchain.pem
private_key=/etc/nginx/tls/privkey.pem

if [ -e "$certificate" ] && [ ! -e "$private_key" ]; then
  echo "TLS certificate exists but its private key is missing" >&2
  exit 1
fi

if [ ! -e "$certificate" ] && [ -e "$private_key" ]; then
  echo "TLS private key exists but its certificate is missing" >&2
  exit 1
fi

if [ ! -e "$certificate" ]; then
  domain=${DOMAIN:-localhost}
  livekit_domain=${LIVEKIT_DOMAIN:-voice.$domain}
  mkdir -p /etc/nginx/tls
  openssl req -x509 -newkey rsa:3072 -sha256 -nodes -days 7 \
    -keyout "$private_key" \
    -out "$certificate" \
    -subj "/CN=$domain" \
    -addext "subjectAltName=DNS:$domain,DNS:$livekit_domain"
  chmod 0600 "$private_key"
  chmod 0644 "$certificate"
  echo "Generated a seven-day self-signed TLS certificate. Replace it before serving real users." >&2
fi
