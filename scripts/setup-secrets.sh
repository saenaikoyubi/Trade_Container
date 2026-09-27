#!/usr/bin/env bash
# setup-secrets.sh - Setup local development and Paper testing secrets for Trade_Container
set -euo pipefail

SECRETS_ROOT="${1:-${HOME}/bot/secrets}"

DB_SECRET_DIR="${SECRETS_ROOT}/local/database"
API_SECRET_DIR="${SECRETS_ROOT}/local/trade-api"
UI_SECRET_DIR="${SECRETS_ROOT}/local/trade-ui"

mkdir -p "${DB_SECRET_DIR}" "${API_SECRET_DIR}" "${UI_SECRET_DIR}"
chmod 700 "${DB_SECRET_DIR}" "${API_SECRET_DIR}" "${UI_SECRET_DIR}"

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

UI_PASSWORD_FILE="${UI_SECRET_DIR}/ui_password"
if [ ! -f "${UI_PASSWORD_FILE}" ]; then
    openssl rand -hex 24 > "${UI_PASSWORD_FILE}"
    chmod 600 "${UI_PASSWORD_FILE}"
    echo "Created: ${UI_PASSWORD_FILE}"
else
    echo "Already exists: ${UI_PASSWORD_FILE}"
fi

UI_SESSION_SECRET_FILE="${UI_SECRET_DIR}/ui_session_secret"
if [ ! -f "${UI_SESSION_SECRET_FILE}" ]; then
    openssl rand -hex 32 > "${UI_SESSION_SECRET_FILE}"
    chmod 600 "${UI_SESSION_SECRET_FILE}"
    echo "Created: ${UI_SESSION_SECRET_FILE}"
else
    echo "Already exists: ${UI_SESSION_SECRET_FILE}"
fi
chmod 600 "${UI_PASSWORD_FILE}" "${UI_SESSION_SECRET_FILE}"

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
