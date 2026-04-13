#!/bin/bash
set -euo pipefail
mkdir -p certs
openssl req -new -newkey rsa:2048 -days 365 -nodes -x509 \
  -subj "/CN=MCD Traffic Inspection CA" \
  -keyout certs/ca.key -out certs/mitmproxy-ca-cert.pem 2>/dev/null
cat certs/mitmproxy-ca-cert.pem certs/ca.key > certs/mitmproxy-ca.pem
rm certs/ca.key
chmod 644 certs/mitmproxy-ca-cert.pem
chmod 600 certs/mitmproxy-ca.pem
echo "CA certificate generated in certs/"
