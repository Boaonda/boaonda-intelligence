#!/usr/bin/env bash
# =========================================================
# Inicialização do container no Railway:
# 1. Sobe o túnel Cloudflare Access em segundo plano
#    (se as variáveis estiverem configuradas)
# 2. Sobe a aplicação Flask/Gunicorn em primeiro plano
#
# Enquanto as tabelas dom_/fact_ não estiverem prontas no
# Supabase, isso só deixa a conexão pronta - o pipeline
# atualizar.py continua no fluxo manual normalmente.
# =========================================================
set -e

if [ -z "$CLOUDFLARE_TUNNEL_HOSTNAME" ] || [ -z "$CLOUDFLARE_SERVICE_TOKEN_ID" ] || [ -z "$CLOUDFLARE_SERVICE_TOKEN_SECRET" ]; then
  echo "[start.sh] Variáveis do túnel Cloudflare não configuradas - subindo sem conexão ao banco."
else
  echo "[start.sh] Iniciando túnel Cloudflare: $CLOUDFLARE_TUNNEL_HOSTNAME -> localhost:${DB_PORT:-5432}"
  cloudflared access tcp \
    --hostname "$CLOUDFLARE_TUNNEL_HOSTNAME" \
    --url "localhost:${DB_PORT:-5432}" \
    --service-token-id "$CLOUDFLARE_SERVICE_TOKEN_ID" \
    --service-token-secret "$CLOUDFLARE_SERVICE_TOKEN_SECRET" &

  sleep 3
  echo "[start.sh] Túnel iniciado em segundo plano (PID $!)"
fi

echo "[start.sh] Iniciando aplicação Flask/Gunicorn"
exec gunicorn app:app --bind "0.0.0.0:${PORT:-8080}" --timeout 900 --workers 1
