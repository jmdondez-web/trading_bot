#!/bin/bash
# Démarrage résilient du forward-test (paper-trading momentum) dans tmux.
# - relancé automatiquement au boot via le service systemd user forward-test.service
# - boucle de respawn : si le forward-test crashe, il repart
# - ne fait rien si une session 'forward' tourne déjà
DIR=/home/worff/trading_bot_v3.4
PY=$DIR/venv/bin/python
export PATH=/usr/bin:/bin:$PATH

if /usr/bin/tmux has-session -t forward 2>/dev/null; then
    echo "Session tmux 'forward' déjà active — rien à faire."
    exit 0
fi

/usr/bin/tmux new-session -d -s forward \
  "while true; do \
     echo \"[\$(date)] démarrage forward-test\"; \
     $PY $DIR/forward_test.py 2>&1 | tee -a $DIR/forward_console.log; \
     echo \"[\$(date)] forward-test arrêté — redémarrage dans 30s\"; \
     sleep 30; \
   done"

echo "Session tmux 'forward' lancée."
