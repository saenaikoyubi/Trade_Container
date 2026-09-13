#!/usr/bin/env bash
# setup-secrets.sh - Setup local development and Paper testing secrets for Trade_Container
set -euo pipefail

SECRETS_ROOT="${1:-${HOME}/bot/secrets}"

DB_SECRET_DIR="${SECRETS_ROOT}/local/database"
API_SECRET_DIR="${SECRETS_ROOT}/local/trade-api"

mkdir -p "${DB_SECRET_DIR}" "${API_SECRET_DIR}"

PG_PASSWORD_FILE="${DB_SECRET_DIR}/postgres_password"
if [ ! -f "${PG_PASSWORD_FILE}" ]; then
    openssl rand -hex 16 > "${PG_PASSWORD_FILE}"
    chmod 600 "${PG_PASSWORD_FILE}"
    echo "Created: ${PG_PASSWORD_FILE}"
else
    echo "Already exists: ${PG_PASSWORD_FILE}"
fi

API_TOKEN_FILE="${API_SECRET_DIR}/api_token"
if [ ! -f "${API_TOKEN_FILE}" ]; then
    openssl rand -hex 32 > "${API_TOKEN_FILE}"
    chmod 600 "${API_TOKEN_FILE}"
    echo "Created: ${API_TOKEN_FILE}"
else
    echo "Already exists: ${API_TOKEN_FILE}"
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
ENV_LOCAL_FILE="${REPO_ROOT}/.env.local"

cat <<EOF > "${ENV_LOCAL_FILE}"
TRADE_SECRETS_DIR=${SECRETS_ROOT}
EOF

echo "Created: ${ENV_LOCAL_FILE}"
echo ""
echo "Setup complete. You can now start Trade_Container with:"
echo "  docker compose --env-file .env.local -f docker/compose.yaml up -d"
