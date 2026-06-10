#!/bin/sh
# Проверка nginx-шаблона без домена/certbot: self-signed cert генерится
# python'ом из app-образа (cryptography уже в зависимостях), файлы живут
# в docker-volume — ни хост-openssl, ни MSYS-путей Windows.
# Требует собранный образ: docker compose -f deploy/docker-compose.prod.yml build app
set -e
DOMAIN="${NAVBAT_DOMAIN:-smoke.localhost}"
SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
VOL=navbat-nginx-check

docker image inspect navbat:latest >/dev/null 2>&1 || {
    echo "[FAIL] нет образа navbat:latest — собери: docker compose -f deploy/docker-compose.prod.yml build app" >&2
    exit 1
}

docker volume create "$VOL" >/dev/null
trap 'docker volume rm "$VOL" >/dev/null' EXIT

docker run --rm -i --user root -v "$VOL:/certs" -e DOMAIN="$DOMAIN" \
    --entrypoint python navbat:latest - <<'PY'
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

domain = os.environ["DOMAIN"]
key = ec.generate_private_key(ec.SECP256R1())
name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, domain)])
now = datetime.now(timezone.utc)
cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now).not_valid_after(now + timedelta(days=30))
        .sign(key, hashes.SHA256()))
live = Path("/certs/live") / domain
live.mkdir(parents=True, exist_ok=True)
(live / "fullchain.pem").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
(live / "privkey.pem").write_bytes(key.private_bytes(
    serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
    serialization.NoEncryption()))
print("[OK] self-signed cert:", domain)
PY

docker run --rm \
    -v "$SCRIPT_DIR/../nginx/templates:/etc/nginx/templates:ro" \
    -v "$VOL:/etc/letsencrypt:ro" \
    -e NAVBAT_DOMAIN="$DOMAIN" \
    --entrypoint sh nginx:1.27-alpine \
    -c "mkdir -p /var/www/certbot && /docker-entrypoint.d/20-envsubst-on-templates.sh >/dev/null && nginx -t"
echo "[OK] nginx-шаблон валиден"
