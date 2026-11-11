#!/usr/bin/env bash
# Fill every CHANGE_ME value in an env file with a fresh random secret.
# Usage: deploy/scripts/gen-secrets.sh deploy/.env
set -euo pipefail
f=${1:?usage: gen-secrets.sh <env-file>}
[[ -f "$f" ]] || { echo "no such file: $f" >&2; exit 1; }
umask 077
tmp=$(mktemp)
while IFS= read -r line || [[ -n "$line" ]]; do
  if [[ "$line" =~ ^([A-Z0-9_]+)=CHANGE_ME$ ]]; then
    # 48 url-safe chars, no characters that need quoting in URLs/env files.
    val=$(head -c 64 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 48)
    echo "${BASH_REMATCH[1]}=$val" >> "$tmp"
  else
    echo "$line" >> "$tmp"
  fi
done < "$f"
mv "$tmp" "$f"; chmod 600 "$f"
echo "secrets written to $f (mode 600)"
