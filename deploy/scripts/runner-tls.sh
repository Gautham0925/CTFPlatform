#!/usr/bin/env bash
# Create a private CA and a server certificate for the runner control API.
#   On the runner host:  deploy/scripts/runner-tls.sh runner.internal.example
# Copies: runner-tls/{runner.crt,runner.key} stay on the runner host;
#         runner-ca/ca.crt goes to the platform host (deploy/compose/runner-ca/)
#         and ARENA_RUNNER_CA=/run/arena/runner-ca/ca.crt in deploy/.env.
set -euo pipefail
name=${1:?usage: runner-tls.sh <runner-hostname>}
base="$(cd "$(dirname "$0")/.." && pwd)/compose"
mkdir -p "$base/runner-tls" "$base/runner-ca" "$base/runner-ca-key"; umask 077
# ca.crt is mounted into the platform containers; the CA key must not be.
ca="$base/runner-ca"; cakey="$base/runner-ca-key/ca.key"; tls="$base/runner-tls"
openssl req -x509 -newkey rsa:3072 -nodes -days 825 -subj "/CN=Arena runner CA" \
  -keyout "$cakey" -out "$ca/ca.crt" 2>/dev/null
openssl req -newkey rsa:2048 -nodes -subj "/CN=$name" \
  -keyout "$tls/runner.key" -out "$tls/runner.csr" 2>/dev/null
printf "subjectAltName=DNS:%s\nextendedKeyUsage=serverAuth\n" "$name" > "$tls/ext.cnf"
openssl x509 -req -in "$tls/runner.csr" -CA "$ca/ca.crt" -CAkey "$cakey" \
  -CAserial "$base/runner-ca-key/ca.srl" -CAcreateserial -days 825 -extfile "$tls/ext.cnf" -out "$tls/runner.crt" 2>/dev/null
rm -f "$tls/runner.csr" "$tls/ext.cnf"
chmod 644 "$ca/ca.crt" "$tls/runner.crt"
# The runner container runs as root and reads the key; keep it 600 on the host.
echo "CA:     $ca/ca.crt   (copy to the platform)"
echo "CA key: $cakey   (move offline; never onto the platform host)"
echo "Server: $tls/runner.crt + runner.key (runner host only)"
