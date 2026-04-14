#!/bin/bash
set -euo pipefail
mkdir -p certs
openssl req -new -newkey rsa:2048 -days 365 -nodes -x509 \
  -subj "/CN=MCD Traffic Inspection CA" \
  -keyout certs/ca.key -out certs/ca-cert.pem 2>/dev/null
cat certs/ca-cert.pem certs/ca.key > certs/mitmproxy-ca.pem
rm certs/ca.key
chmod 644 certs/ca-cert.pem
chmod 600 certs/mitmproxy-ca.pem
echo "CA certificate generated in certs/"
echo ""
echo "Next steps:"
echo "  1. Paste the contents of certs/ca-cert.pem into values.yaml under firewallCa.cert"
echo "  2. Create the mitmproxy secret:"
echo "     kubectl create secret generic mitmproxy-ca -n mcd-agent --from-file=mitmproxy-ca.pem=certs/mitmproxy-ca.pem"
