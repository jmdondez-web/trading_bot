#!/usr/bin/env python3
"""Forward-test (paper-trading live) de la stratégie MOMENTUM validée — Phase A live.

Indépendant du bot principal : ne touche ni à la vraie balance Binance, ni à
trading.db. Portefeuille 100% VIRTUEL (500 USDC fictif au départ).

Stratégie (config validée par backtest_v3.py, +10.7%/90j, DD 2%, robuste 30/60/90j) :
  - Timeframe 1h
  - Entrée : croisement haussier EMA 5/20 (acheter la FORCE)
  - Filtre de régime BTC (vue extérieure) : on n'ouvre QUE si BTC > SMA200(1h) ET montante
  - Sortie : stop initial = min(ATR×2.0, 3% du prix) [cap adaptatif fort ATR] (coupe vite)
             + trailing ATR×5.0 (laisse courir),
             + TP partiel à +4% sur 50% de la position, activation trailing à +1.5%
  - Portefeuille partagé : max 3 positions, allocation 50/30/20, 95% du cash, frais 0.1%/leg

Fidèle au backtest : agit UNIQUEMENT sur bougies clôturées, mêmes maths de sortie.
État persisté dans forward_state.json (résiste aux redémarrages).

Usage : ./venv/bin/python forward_test.py
"""
import json
import time
import logging
import os
from datetime import datetime

import requests
import pandas as pd
from binance.client import Client
from binance_client import BinanceClientWrapper
from strategy import StrategyEngine
from config import Config

# ───────────────────── CONFIG STRATÉGIE (validée backtest) ─────────────────────
START_CAPITAL   = 500.0
UNIVERSE        = ["ETHUSDC", "SOLUSDC", "BNBUSDC", "AVAXUSDC",
                   "INJUSDC", "LINKUSDC", "DOGEUSDC", "TIAUSDC"]
BTC_SYMBOL      = "BTCUSDC"          # filtre de régime uniquement (non tradé)
EMA_FAST        = 5
EMA_SLOW        = 20
BTC_SMA         = 200
STOP_ATR        = 2.0
STOP_CAP        = 0.03              # cap adaptatif : stop initial = min(ATR×2, 3% du prix)
TRAIL_ATR       = 5.0
TRAIL_ACTIV     = 0.015              # +1.5% avant d'armer le trailing
TP_PCT          = 0.04              # TP partiel à +4%
TP_SIZE         = 0.50              # sur 50% de la position
MAX_POSITIONS   = 3
CAPITAL_SPLIT   = [0.50, 0.30, 0.20]
CAPITAL_RISK    = 0.95
MIN_TRADE       = 12.0
FEE             = 0.001              # 0.1% par leg
POLL_SECONDS    = 60
KLINE_LIMIT     = 300               # assez pour SMA200 + EMA warmup

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "forward_state.json")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.FileHandler("forward_test.log"), logging.StreamHandler()],
)
log = logging.getLogger("ForwardTest")


def tg_send(text):
    """Envoi best-effort sur Telegram (préfixé pour distinguer du bot principal)."""
    if not (Config.TELEGRAM_TOKEN and Config.TELEGRAM_CHAT_ID):
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{Config.TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": Config.TELEGRAM_CHAT_ID, "text": f"🧪 [FORWARD-TEST] {text}"},
            timeout=5,
        )
    except Exception as e:
        log.warning(f"Telegram échec: {e}")


# ───────────────────────────── indicateurs ─────────────────────────────
def closed(df):
    """Retire la bougie en cours de formation (dernière ligne)."""
    return df.iloc[:-1] if len(df) > 1 else df


def ema_cross_up(df):
    """True si l'EMA rapide vient de croiser au-dessus de l'EMA lente sur la
    DERNIÈRE bougie clôturée (causal)."""
    c = df['close']
    ef = c.ewm(span=EMA_FAST, adjust=False).mean()
    es = c.ewm(span=EMA_SLOW, adjust=False).mean()
    if len(c) < EMA_SLOW + 2:
        return False
    return bool(ef.iloc[-1] > es.iloc[-1] and ef.iloc[-2] <= es.iloc[-2])


def atr_value(df):
    a = StrategyEngine.calculate_atr(df)
    v = a.iloc[-1]
    return float(v) if v == v else 0.0   # NaN -> 0


def btc_regime_ok(df):
    """BTC porteur = close > SMA200 ET SMA200 montante (sur bougie clôturée)."""
    c = df['close']
    if len(c) < BTC_SMA + 2:
        return False
    sma = c.rolling(BTC_SMA).mean()
    return bool(c.iloc[-1] > sma.iloc[-1] and sma.iloc[-1] > sma.iloc[-2])


# ───────────────────────────── état (persistance) ─────────────────────────────
def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"cash": START_CAPITAL, "positions": {}, "closed_trades": [],
            "last_candle_ts": None, "started": datetime.utcnow().isoformat()}


def save_state(state):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, STATE_FILE)   # écriture atomique


def equity(state, prices):
    mv = sum(p['qty'] * prices.get(s, p['entry']) for s, p in state['positions'].items())
    return state['cash'] + mv


def daily_summary(state, prices, regime):
    """Texte de résumé quotidien (équité, P&L réalisé, positions, trades clos)."""
    eq = equity(state, prices)
    ret = (eq - START_CAPITAL) / START_CAPITAL * 100
    closed_n = len(state['closed_trades'])
    realized = sum(t['pnl'] for t in state['closed_trades'])
    wins = sum(1 for t in state['closed_trades'] if t['pnl'] > 0)
    wr = (wins / closed_n * 100) if closed_n else 0
    started = state.get('started', '?')[:10]
    pos = ", ".join(state['positions']) or "aucune"
    return (f"📊 RÉSUMÉ (depuis {started})\n"
            f"Équité: {eq:.2f} USDC ({ret:+.2f}% vs 500)\n"
            f"Cash: {state['cash']:.2f} | Positions: {pos}\n"
            f"Trades clos: {closed_n} (WR {wr:.0f}%) | P&L réalisé: {realized:+.2f} USDC\n"
            f"BTC porteur: {'oui' if regime else 'non'}")


# ───────────────────────────── logique de trading ─────────────────────────────
def process_exits(state, sym, df):
    """Réplique la gestion de sortie du backtest sur la dernière bougie clôturée."""
    pos = state['positions'][sym]
    price = float(df['close'].iloc[-1])
    hi = float(df['high'].iloc[-1])
    lo = float(df['low'].iloc[-1])
    a = atr_value(df)

    # trailing une fois le seuil d'activation franchi
    if a > 0 and (price - pos['entry']) / pos['entry'] >= TRAIL_ACTIV:
        ns = price - a * TRAIL_ATR
        if ns > pos['stop']:
            pos['stop'] = ns

    # stop d'abord (pessimiste), puis TP
    if lo <= pos['stop']:
        ex = pos['stop']
        proceeds = pos['qty'] * ex * (1 - FEE)
        pos['proceeds'] += proceeds
        state['cash'] += proceeds
        _close_trade(state, sym, ex, "STOP")
        return
    if hi >= pos['tp_price']:
        sell = pos['qty'] * TP_SIZE
        proceeds = sell * pos['tp_price'] * (1 - FEE)
        pos['proceeds'] += proceeds
        state['cash'] += proceeds
        pos['qty'] -= sell
        if pos['stop'] < pos['entry']:
            pos['stop'] = pos['entry']      # sécurisé au break-even
        log.info(f"TP partiel {sym} +{TP_PCT*100:.0f}% sur {TP_SIZE*100:.0f}% @ {pos['tp_price']:.4f} "
                 f"| reste qty={pos['qty']:.6f}")
        if pos['qty'] <= 1e-9:
            _close_trade(state, sym, pos['tp_price'], "TP_FULL")


def _close_trade(state, sym, exit_price, reason):
    pos = state['positions'].pop(sym)
    pnl = pos['proceeds'] - pos['cost']
    state['closed_trades'].append({
        "symbol": sym, "entry": pos['entry'], "exit": exit_price, "reason": reason,
        "pnl": round(pnl, 4), "opened": pos['opened'],
        "closed": datetime.utcnow().isoformat(),
    })
    log.info(f"SORTIE {sym} [{reason}] @ {exit_price:.4f} | P&L {pnl:+.2f} USDC "
             f"| cash {state['cash']:.2f}")
    tg_send(f"SORTIE {sym} [{reason}] @ {exit_price:.4f}\nP&L {pnl:+.2f} USDC | cash {state['cash']:.2f}")


def try_entry(state, sym, df):
    price = float(df['close'].iloc[-1])
    a = atr_value(df)
    if a <= 0 or not ema_cross_up(df):
        return
    pos_idx = len(state['positions'])
    split = CAPITAL_SPLIT[min(pos_idx, len(CAPITAL_SPLIT) - 1)]
    capital = state['cash'] * CAPITAL_RISK * split
    if capital < MIN_TRADE:
        return
    qty = capital / price
    cost = capital * (1 + FEE)
    state['cash'] -= cost
    stop_dist = min(a * STOP_ATR, price * STOP_CAP)   # cap adaptatif (mord sur fort ATR)
    stop_price = price - stop_dist
    state['positions'][sym] = {
        "entry": price, "qty": qty, "stop": stop_price,
        "tp_price": price * (1 + TP_PCT), "cost": cost, "proceeds": 0.0,
        "opened": datetime.utcnow().isoformat(),
    }
    log.info(f"ENTRÉE {sym} @ {price:.4f} | qty={qty:.6f} stop={stop_price:.4f} "
             f"({stop_dist/price*100:.1f}%) tp={price*(1+TP_PCT):.4f} | capital={capital:.2f} (split {split})")
    tg_send(f"ENTRÉE {sym} @ {price:.4f}\nstop {stop_price:.4f} | tp {price*(1+TP_PCT):.4f} "
            f"| mise {capital:.2f} USDC")


def main():
    log.info("=" * 70)
    log.info("FORWARD-TEST MOMENTUM (paper-trading) démarré")
    log.info(f"Capital virtuel départ: {START_CAPITAL} USDC | univers: {UNIVERSE}")
    log.info(f"Config: 1h EMA{EMA_FAST}/{EMA_SLOW} | filtre BTC>SMA{BTC_SMA} montante | "
             f"stop min(ATR×{STOP_ATR}, {STOP_CAP*100:.0f}%) trail ATR×{TRAIL_ATR} "
             f"TP+{TP_PCT*100:.0f}%×{TP_SIZE*100:.0f}%")
    binance = BinanceClientWrapper()
    state = load_state()
    log.info(f"État chargé: cash={state['cash']:.2f} positions={list(state['positions'])} "
             f"trades clos={len(state['closed_trades'])}")
    tg_send(f"Démarré. Capital virtuel {state['cash']:.2f} USDC | "
            f"{len(state['closed_trades'])} trades clos | univers {len(UNIVERSE)} paires (1h).")

    while True:
        try:
            btc_df = closed(binance.get_klines(BTC_SYMBOL, KLINE_LIMIT, Client.KLINE_INTERVAL_1HOUR))
            if btc_df.empty:
                time.sleep(POLL_SECONDS); continue
            candle_ts = str(btc_df['timestamp'].iloc[-1])

            # n'agir qu'une fois par nouvelle bougie 1h clôturée
            if candle_ts == state.get('last_candle_ts'):
                time.sleep(POLL_SECONDS); continue

            regime = btc_regime_ok(btc_df)
            dfs, prices = {}, {}
            for s in UNIVERSE:
                d = closed(binance.get_klines(s, KLINE_LIMIT, Client.KLINE_INTERVAL_1HOUR))
                if not d.empty:
                    dfs[s] = d
                    prices[s] = float(d['close'].iloc[-1])

            # 1) sorties d'abord (libère cash et slots)
            for s in list(state['positions'].keys()):
                if s in dfs:
                    process_exits(state, s, dfs[s])

            # 2) entrées si BTC porteur et slots dispo
            if regime:
                for s in UNIVERSE:
                    if len(state['positions']) >= MAX_POSITIONS:
                        break
                    if s in state['positions'] or s not in dfs:
                        continue
                    try_entry(state, s, dfs[s])

            state['last_candle_ts'] = candle_ts

            # résumé quotidien sur Telegram (une fois par jour UTC)
            today = datetime.utcnow().strftime("%Y-%m-%d")
            if state.get('last_summary_date') != today:
                tg_send(daily_summary(state, prices, regime))
                state['last_summary_date'] = today

            save_state(state)
            eq = equity(state, prices)
            ret = (eq - START_CAPITAL) / START_CAPITAL * 100
            log.info(f"[{candle_ts}] BTC porteur={regime} | équité={eq:.2f} USDC "
                     f"({ret:+.2f}%) | cash={state['cash']:.2f} | "
                     f"positions={list(state['positions'])}")
        except Exception as e:
            log.error(f"Erreur boucle: {e}")
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
