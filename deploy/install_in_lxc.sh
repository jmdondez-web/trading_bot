#!/bin/bash
# Installation du trading bot dans un LXC Debian 12 — à lancer DANS le conteneur,
# en tant qu'utilisateur normal, depuis la racine du repo cloné.
set -euo pipefail

cd "$(dirname "$0")/.."

if [ ! -f requirements.txt ]; then
    echo "ERREUR: lance ce script depuis la racine du repo (deploy/install_in_lxc.sh)"
    exit 1
fi

echo "== 1/4 venv Python =="
python3 -m venv venv
./venv/bin/pip install --upgrade pip -q
./venv/bin/pip install -r requirements.txt -q

echo "== 2/4 tests unitaires (100% hors-ligne) =="
export BINANCE_API_KEY=test BINANCE_SECRET_KEY=test \
       TELEGRAM_BOT_TOKEN=test TELEGRAM_CHAT_ID=test
if ./venv/bin/python -m unittest discover -s tests 2>&1 | tail -3; then
    echo "Tests OK"
else
    echo "ERREUR: les tests echouent — NE PAS demarrer le bot."
    exit 1
fi

echo "== 3/4 verification .env =="
if [ ! -f .env ]; then
    echo "Aucun .env trouvé."
    if [ -f deploy/env.example ]; then
        cp deploy/env.example .env
        chmod 600 .env
        echo "=> .env cree depuis deploy/env.example — REMPLIS LES VRAIES CLES maintenant."
    fi
else
    echo ".env present"
fi

echo "== 4/4 etapes restantes =="
cat <<'EOF'
1. Remplis .env avec tes vraies clés (Binance, Telegram).
2. Vérifie que DRY_RUN=true est bien la ligne par défaut (recommandé).
3. En root : copie deploy/tradingbot.service dans /etc/systemd/system/,
   puis: systemctl daemon-reload && systemctl enable --now tradingbot
4. Logs: journalctl -u tradingbot -f
EOF
echo "Installation terminée."
