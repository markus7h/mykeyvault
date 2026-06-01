#!/bin/bash
# setup.sh — Deployment mykeyvault + vault-api auf einen Docker-Host
#
# Zielhost und Pfade sind über Env-Variablen konfigurierbar:
#   DEPLOY_HOST          SSH-Zielhost (Default: your-server)
#   REMOTE_COMPOSE_DIR   Compose-Pfad auf dem Host
#   REMOTE_DATA_DIR      Daten-Pfad auf dem Host
#
# Aufruf (von einem Host mit SSH-Zugang zum Zielhost):
#   DEPLOY_HOST=your-server bash setup.sh

set -euo pipefail

DEPLOY_HOST="${DEPLOY_HOST:-your-server}"
REMOTE_COMPOSE_DIR="${REMOTE_COMPOSE_DIR:-/var/local/mydocker/compose-files/mykeyvault}"
REMOTE_DATA_DIR="${REMOTE_DATA_DIR:-/var/local/mydocker/mykeyvault/data}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "=== mykeyvault Deploy auf $DEPLOY_HOST ==="

# Verzeichnisse anlegen
ssh "$DEPLOY_HOST" "mkdir -p $REMOTE_COMPOSE_DIR $REMOTE_DATA_DIR"

# vault-api Quellcode übertragen
ssh "$DEPLOY_HOST" "mkdir -p $REMOTE_COMPOSE_DIR/vault-api"
scp "$SCRIPT_DIR/docker-compose.yml" "$DEPLOY_HOST:$REMOTE_COMPOSE_DIR/docker-compose.yml"
scp "$SCRIPT_DIR/vault-api/main.py" "$DEPLOY_HOST:$REMOTE_COMPOSE_DIR/vault-api/main.py"
scp "$SCRIPT_DIR/vault-api/Dockerfile" "$DEPLOY_HOST:$REMOTE_COMPOSE_DIR/vault-api/Dockerfile"
scp "$SCRIPT_DIR/vault-api/requirements.txt" "$DEPLOY_HOST:$REMOTE_COMPOSE_DIR/vault-api/requirements.txt"
echo "✓ Dateien übertragen"

# .env anlegen falls nicht vorhanden
if ! ssh "$DEPLOY_HOST" "test -f $REMOTE_COMPOSE_DIR/.env"; then
    TOKEN=$(openssl rand -base64 32)
    ssh "$DEPLOY_HOST" "cat > $REMOTE_COMPOSE_DIR/.env << 'EOF'
VAULT_API_TOKEN=$TOKEN
BW_CLIENTID=
BW_CLIENTSECRET=
BW_PASSWORD=
EOF
chmod 600 $REMOTE_COMPOSE_DIR/.env"
    echo "✓ .env erstellt (BW_CLIENTID, BW_CLIENTSECRET, BW_PASSWORD noch eintragen!)"
    echo "  VAULT_API_TOKEN=$TOKEN"
    echo "  (Token auf Claude-Host in ~/.claude.json als VAULT_API_TOKEN eintragen)"
else
    echo "✓ .env bereits vorhanden"
fi

# Container starten / aktualisieren
ssh "$DEPLOY_HOST" "cd $REMOTE_COMPOSE_DIR && docker compose up -d --build"
echo "✓ Container gestartet"

echo ""
echo "=== Zugang ==="
echo "  Vaultwarden:  https://mykeyvault.lan  (Port 8222 direkt)"
echo "  vault-api:    http://$DEPLOY_HOST:8223/health"
echo ""
echo "=== Claude MCP ==="
echo "  ~/.claude.json mcpServers.mykeyvault.env.VAULT_API_TOKEN setzen"
echo "  VAULT_API_URL=http://$DEPLOY_HOST:8223"
