#!/bin/bash
# Démarrage résilient du bot dans tmux.
# - relancé automatiquement au boot via @reboot cron
# - boucle de respawn : si le bot crashe (ex: réseau pas prêt au boot), il repart
# - ne fait rien si une session 'bot' tourne déjà
DIR=/home/worff/trading_bot_v3.4
PY=$DIR/venv/bin/python
export PATH=/usr/bin:/bin:$PATH

if /usr/bin/tmux has-session -t bot 2>/dev/null; then
    echo "Session tmux 'bot' déjà active — rien à faire."
    exit 0
fi

/usr/bin/tmux new-session -d -s bot \
  "while true; do \
     echo \"[\$(date)] démarrage du bot\"; \
     $PY $DIR/bot_v3_4.py 2>&1 | tee -a $DIR/bot_output.log; \
     echo \"[\$(date)] bot arrêté — redémarrage dans 30s\"; \
     sleep 30; \
   done"

echo "Session tmux 'bot' lancée."
