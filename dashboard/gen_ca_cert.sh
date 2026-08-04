#!/usr/bin/env bash
# gen_ca_cert.sh — Task 350: give the dashboard a CA-signed TLS leaf so browsers
# stop showing "Not Secure".
#
# WHY: the old cert was self-signed (Issued To == Issued By), so browsers refuse
# to trust it even though the connection is already encrypted. Here we mint a tiny
# LOCAL Root CA once, then issue the dashboard's leaf cert signed by it. The ONLY
# device that must trust anything is the browsing machine (install instance/rootCA.pem
# once — see the trust steps at the bottom of this file). The server keeps reading
# the same instance/cert.pem + instance/key.pem paths, so nothing else changes.
#
# IDEMPOTENT: re-running is a no-op once a CA-signed leaf that verifies against the
# root already exists. Safe to call from start_dashboard.sh.
#
# NEVER share instance/rootCA.key or instance/key.pem — those are private keys.
# Only the PUBLIC certs (instance/rootCA.pem, instance/cert.pem) are meant to leave
# the host.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
INST="$HERE/instance"
mkdir -p "$INST"

command -v openssl >/dev/null 2>&1 || { echo "ERR: openssl not found; cannot make TLS cert" >&2; exit 1; }

ROOT_KEY="$INST/rootCA.key"      # PRIVATE — never leaves the host
ROOT_CRT="$INST/rootCA.pem"      # public — this is what the Mac trusts once
LEAF_KEY="$INST/key.pem"         # PRIVATE — the server's key (never shared)
LEAF_CRT="$INST/cert.pem"        # public — what the server presents to browsers
LEAF_CSR="$INST/leaf.csr"        # transient

# Derive identity the SAME way start_dashboard.sh's gen_cert() does.
CN="$(hostname -f 2>/dev/null || hostname 2>/dev/null || echo localhost)"
IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
SAN="DNS:${CN},DNS:localhost,IP:127.0.0.1"
[ -n "$IP" ] && SAN="${SAN},IP:${IP}"

# --- 0. Idempotency: already have a leaf verified by our root? Then stop. -------
if [ -s "$ROOT_CRT" ] && [ -s "$LEAF_CRT" ] && [ -s "$LEAF_KEY" ] \
   && openssl verify -CAfile "$ROOT_CRT" "$LEAF_CRT" >/dev/null 2>&1; then
  echo "gen_ca_cert: CA-signed leaf already present and verifies against rootCA.pem — nothing to do."
  exit 0
fi

# --- 1. Back up any existing (self-signed) leaf ONCE, so this is reversible. ----
STAMP="bak_task350"
for f in "$LEAF_CRT" "$LEAF_KEY"; do
  if [ -s "$f" ] && [ ! -e "${f}.${STAMP}" ]; then
    cp -p "$f" "${f}.${STAMP}"
    echo "gen_ca_cert: backed up $(basename "$f") -> $(basename "$f").${STAMP}"
  fi
done

# --- 2. Local Root CA (generate once; reuse if already present). ----------------
if [ ! -s "$ROOT_KEY" ] || [ ! -s "$ROOT_CRT" ]; then
  echo "gen_ca_cert: generating local Root CA (CN=claude-infra local CA) ..."
  openssl req -x509 -newkey rsa:4096 -nodes \
    -keyout "$ROOT_KEY" -out "$ROOT_CRT" -days 3650 -sha256 \
    -subj "/CN=claude-infra local CA/O=claude-infra" \
    -addext "basicConstraints=critical,CA:TRUE" \
    -addext "keyUsage=critical,keyCertSign,cRLSign" \
    -addext "subjectKeyIdentifier=hash" >/dev/null 2>&1
  chmod 600 "$ROOT_KEY" 2>/dev/null || true
  chmod 644 "$ROOT_CRT" 2>/dev/null || true
else
  echo "gen_ca_cert: reusing existing local Root CA (rootCA.pem)."
fi

# --- 3. Fresh leaf key + CSR for the dashboard host. ----------------------------
echo "gen_ca_cert: generating leaf key + CSR (CN=$CN) ..."
openssl req -newkey rsa:2048 -nodes -keyout "$LEAF_KEY" -out "$LEAF_CSR" \
  -subj "/CN=${CN}" >/dev/null 2>&1

# --- 4. Sign the leaf with the CA. v3 exts: SAN + serverAuth + CA:FALSE so
#        Chrome/Safari accept it. -----------------------------------------------
EXT="$INST/.leaf_ext.$$"
cat > "$EXT" <<EOF
basicConstraints=critical,CA:FALSE
keyUsage=critical,digitalSignature,keyEncipherment
extendedKeyUsage=serverAuth
subjectAltName=${SAN}
subjectKeyIdentifier=hash
authorityKeyIdentifier=keyid,issuer
EOF
echo "gen_ca_cert: signing leaf (SAN=$SAN) with the local CA ..."
openssl x509 -req -in "$LEAF_CSR" -CA "$ROOT_CRT" -CAkey "$ROOT_KEY" \
  -CAcreateserial -days 825 -sha256 -extfile "$EXT" -out "$LEAF_CRT" >/dev/null 2>&1
chmod 600 "$LEAF_KEY" 2>/dev/null || true
chmod 644 "$LEAF_CRT" 2>/dev/null || true
rm -f "$EXT" "$LEAF_CSR"

# --- 5. Verify and summarize. ---------------------------------------------------
echo "gen_ca_cert: verifying ..."
openssl verify -CAfile "$ROOT_CRT" "$LEAF_CRT"
echo "--- leaf issuer / subject ---"
openssl x509 -in "$LEAF_CRT" -noout -issuer -subject
echo "--- leaf Subject Alternative Name ---"
openssl x509 -in "$LEAF_CRT" -noout -text | grep -A1 "Subject Alternative Name"
echo
echo "gen_ca_cert: DONE. Serve $LEAF_CRT (restart the dashboard); trust $ROOT_CRT once on the browsing device:"
echo "  macOS: double-click rootCA.pem -> Keychain Access (login) -> find 'claude-infra local CA'"
echo "         -> Get Info -> Trust -> 'When using this certificate: Always Trust' -> close (enter password)."
echo "         Then fully quit + reopen the browser and revisit https://${CN}:8787 ."
