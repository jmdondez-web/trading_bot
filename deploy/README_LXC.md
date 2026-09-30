# Déploiement du trading bot dans un LXC Proxmox

## 1. Sur l'hôte Proxmox — créer le conteneur CT 104

Via l'interface web (Datacenter → local → CT) ou en ligne de commande :

```bash
pct create 104 local:vztmpl/debian-12-standard_12.7-1_amd64.tar.zst \
  --hostname tradingbot \
  --memory 1024 --swap 512 \
  --cores 1 \
  --rootfs local-lvm:8 \
  --net0 name=eth0,bridge=vmbr1,ip=192.168.1.104/24,gw=192.168.1.1 \
  --unprivileged 1 \
  --features nesting=1 \
  --start 1
pct enter 104
```

> `nesting=1` est requis pour les venv Python et systemd dans le conteneur.
> Adapte l'IP/vmbr1 à ton réseau (comme pour le LXC 103 du mentor IA).

## 2. DANS le conteneur — installation

```bash
apt update && apt -y install python3 python3-venv git curl
useradd -m -s /bin/bash worff          # ou ton utilisateur habituel
su - worff
git clone https://github.com/jmdondez-web/trading_bot.git ~/trading_bot
cd ~/trading_bot
bash deploy/install_in_lxc.sh
```

Le script crée le venv, installe les dépendances, et vérifie les tests unitaires.
Il affiche ensuite les étapes restantes (fichier .env + service systemd).

## 3. Fichier .env

```bash
cp deploy/env.example .env   # puis remplir les vraies clés
chmod 600 .env               # secrets lisibles uniquement par l'utilisateur
```

## 4. Services systemd (remplacent tmux)

En root dans le conteneur :

```bash
cp deploy/tradingbot.service /etc/systemd/system/
cp deploy/forward-test.service /etc/systemd/system/  # si forward-test souhaité
systemctl daemon-reload
systemctl enable --now tradingbot
systemctl enable --now forward-test   # optionnel
```

Suivi :

```bash
systemctl status tradingbot
journalctl -u tradingbot -f
```

## Points d'attention

- **DRY_RUN=true** par défaut : le bot ne passe pas en réel tant que tu ne
  changes pas la valeur (via Telegram ou la DB). Laisse-le en dry-run.
- **forward-test = la vraie validation** de la stratégie momentum (4-8 semaines).
  Il tourne en paper trading (500 USDC virtuels), sans risque.
- Sortie Internet directe : aucun Tailscale/tun nécessaire dans le LXC.
- Les mises à jour du code : `cd ~/trading_bot && git pull && sudo systemctl restart tradingbot`.
