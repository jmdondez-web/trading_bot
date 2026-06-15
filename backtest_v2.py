#!/usr/bin/env python3
"""Backtest réaliste + balayage de paramètres (V2).

Améliorations vs backtest.py :
 - vrai historique (get_historical_klines, paginé) au lieu de 500 bougies
 - réplique la logique de sortie réelle : stop ATR initial, trailing ATR, TP partiel
 - frais aller-retour inclus (0.1%/leg)
 - exécution intrabar (high/low) — stop testé avant TP dans une même bougie (pessimiste)
 - balayage TP / BB_MARGIN / seuils RSI / filtre MACD / taille TP
 - indicateurs pré-calculés une seule fois (causal) -> rapide

Usage : ./venv/bin/python backtest_v2.py [jours]
"""
import sys
import numpy as np
import pandas as pd
from binance.client import Client
from config import Config
from strategy import StrategyEngine

FEE = 0.001  # 0.1% par leg
CORE = ["BTCUSDC", "ETHUSDC", "SOLUSDC", "BNBUSDC"]


def fetch(client, symbol, days):
    raw = client.get_historical_klines(symbol, Client.KLINE_INTERVAL_5MINUTE, f"{days} day ago UTC")
    df = pd.DataFrame(raw, columns=['timestamp','open','high','low','close','volume',
        'ct','qav','not','tbb','tbq','ig'])
    for c in ['open','high','low','close']:
        df[c] = df[c].astype(float)
    return df


def precompute(df):
    """Indicateurs causaux + parties config-indépendantes de la décision d'entrée,
    calculés une seule fois pour toute la série."""
    from strategy import RSI_RISING_TICKS as T
    close = df['close']
    rsi = StrategyEngine.calculate_rsi(close)
    bb = StrategyEngine.calculate_bollinger(close, Config.BB_PERIOD, Config.BB_STD)
    macd_line, signal_line, _ = StrategyEngine.calculate_macd(close)
    atr = StrategyEngine.calculate_atr(df)

    c = close.values; lo = bb.lower.values; w = bb.width.values
    ref = np.where(np.isnan(bb.ref.values), 0.05, bb.ref.values)
    rsi_v = rsi.values

    # contexte vectorisé
    is_chaos = w > ref * 1.5
    lb = Config.BAND_WALK_LOOKBACK
    n = len(c)
    band_walk = np.zeros(n, dtype=bool)
    for i in range(lb, n):
        rc = c[i-lb+1:i+1]; rl = lo[i-lb+1:i+1]
        band_walk[i] = (rc <= rl).all() and rl[-1] < rl[0]
    vr = np.where(ref > 0, w / ref, 1.0)
    ctx_range = (~is_chaos) & (~band_walk) & (vr < Config.RANGE_VR_THRESHOLD)

    # RSI rising T ticks (config-indépendant)
    rising = np.zeros(n, dtype=bool)
    for i in range(T, n):
        tail = rsi_v[i-T:i+1]
        if not np.isnan(tail).any():
            rising[i] = all(tail[k] < tail[k+1] for k in range(T))

    dist_lower = np.where(lo > 0, (c - lo) / lo, 1.0)  # distance relative à la bande basse
    macd_bull = macd_line.values > signal_line.values
    valid = ~(np.isnan(rsi_v) | np.isnan(lo) | np.isnan(w))

    return {
        'close': c.tolist(), 'high': df['high'].values.tolist(), 'low': df['low'].values.tolist(),
        'atr': atr.values.tolist(), 'rsi': rsi_v, 'dist_lower': dist_lower,
        'ctx_range': ctx_range, 'rising': rising, 'macd_bull': macd_bull, 'valid': valid,
        'price_at_lower': c <= lo, 'n': n,
    }


def eligibility(ind, symbol, margin, rsi_set, macd_filter):
    """Booléen vectorisé d'éligibilité à l'entrée pour une config donnée (entrée only)."""
    threshold = rsi_set.get(symbol, 38)
    near = ind['price_at_lower'] | ((margin > 0) & (ind['dist_lower'] <= margin / 100))
    elig = ind['valid'] & ind['ctx_range'] & near & (ind['rsi'] <= threshold) & ind['rising']
    if macd_filter:
        elig = elig & ind['macd_bull']
    return elig.tolist()


def simulate(ind, elig, p):
    close = ind['close']; high = ind['high']; low = ind['low']; atr = ind['atr']
    n = ind['n']
    smult = Config.STOP_LOSS_ATR_MULTIPLIER
    tmult = Config.TRAILING_ATR_MULTIPLIER
    activ = Config.TRAILING_STOP_ACTIVATION
    tp_frac = p['tp'] / 100.0
    size = p['tp_size'] / 100.0
    trades = []
    in_pos = False
    entry = stop = qty = tp_price = 0.0
    for i in range(Config.BB_PERIOD + 2, n):
        price = close[i]; a = atr[i]
        if not in_pos:
            if elig[i] and a == a and a > 0:  # a==a écarte NaN
                entry = price
                stop = price - a * smult
                qty = 100.0 / price
                tp_price = entry * (1 + tp_frac)
                in_pos = True
            continue
        # en position : trailing
        if a == a and a > 0 and (price - entry) / entry >= activ:
            ns = price - a * tmult
            if ns > stop:
                stop = ns
        lo = low[i]
        if lo <= stop:  # stop d'abord (pessimiste)
            ex = stop
            fees = (entry + ex) * qty * FEE
            trades.append(((ex-entry)/entry*100, (ex-entry)*qty-fees, 'STOP'))
            in_pos = False
            continue
        if high[i] >= tp_price:
            ex = tp_price
            sell = qty * size
            fees = (entry + ex) * sell * FEE
            trades.append(((ex-entry)/entry*100, (ex-entry)*sell-fees, 'TP'))
            if size >= 1.0:
                in_pos = False
            else:
                qty -= sell
                if stop < entry:
                    stop = entry  # reste sécurisé au break-even
    return trades


def stats(trades):
    # trades : liste de tuples (pnl_pct, pnl_usdc, type)
    if not trades:
        return None
    n = len(trades)
    wins = [t for t in trades if t[1] > 0]
    losses = [t for t in trades if t[1] <= 0]
    wr = len(wins) / n * 100
    avg_w = np.mean([t[1] for t in wins]) if wins else 0
    avg_l = np.mean([t[1] for t in losses]) if losses else 0
    net = sum(t[1] for t in trades)
    expectancy = net / n
    return {'n': n, 'wr': wr, 'avg_w': avg_w, 'avg_l': avg_l, 'net': net, 'exp': expectancy}


def run_config(data, p):
    agg = []
    for symbol, ind in data.items():
        elig = eligibility(ind, symbol, p['bb_margin'], p['rsi'], p['macd_filter'])
        agg += simulate(ind, elig, p)
    return stats(agg)


def main():
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 60
    client = BinanceClientWrapper_quiet()
    print(f"Chargement {days}j d'historique 5m pour {CORE}...", flush=True)
    data = {}
    for s in CORE:
        df = fetch(client, s, days)
        if len(df) < 100:
            print(f"  {s}: données insuffisantes ({len(df)})")
            continue
        data[s] = precompute(df)
        print(f"  {s}: {len(df)} bougies", flush=True)

    # config actuelle
    base = {'tp': Config.TAKE_PROFIT_PERCENT, 'bb_margin': Config.BB_MARGIN_PERCENT,
            'rsi': dict(Config.RSI_THRESHOLDS), 'macd_filter': Config.MACD_FILTER_ENABLED,
            'tp_size': Config.TAKE_PROFIT_SIZE}
    print("\n=== CONFIG ACTUELLE ===")
    s = run_config(data, base)
    print(fmt(base, s))

    # grilles
    tp_grid = [1.0, 1.3, 1.5, 2.0, 3.0]
    margin_grid = [0.0, 1.0, 1.5, 2.5]
    rsi_sets = {
        'actuel': {"BTCUSDC":45,"ETHUSDC":44,"SOLUSDC":40,"BNBUSDC":43},
        'serre':  {"BTCUSDC":40,"ETHUSDC":39,"SOLUSDC":36,"BNBUSDC":38},
        'tres_serre': {"BTCUSDC":35,"ETHUSDC":34,"SOLUSDC":32,"BNBUSDC":34},
    }
    macd_grid = [False, True]
    size_grid = [100, 50]

    results = []
    for tp in tp_grid:
        for m in margin_grid:
            for rname, rset in rsi_sets.items():
                for mf in macd_grid:
                    for sz in size_grid:
                        p = {'tp': tp, 'bb_margin': m, 'rsi': rset, 'macd_filter': mf,
                             'tp_size': sz, 'rsi_name': rname}
                        st = run_config(data, p)
                        if st and st['n'] >= 8:  # min échantillon
                            results.append((p, st))

    # tri : net P&L décroissant
    results.sort(key=lambda x: x[1]['net'], reverse=True)
    print(f"\n=== TOP 12 configs (sur {len(results)} testées, min 8 trades) — tri par P&L net ===")
    for p, st in results[:12]:
        print(fmt(p, st))

    # meilleure par winrate (min 15 trades pour fiabilité)
    by_wr = sorted([r for r in results if r[1]['n'] >= 15], key=lambda x: x[1]['wr'], reverse=True)
    print(f"\n=== TOP 8 par WINRATE (min 15 trades) ===")
    for p, st in by_wr[:8]:
        print(fmt(p, st))


def fmt(p, st):
    if not st:
        return f"  TP={p['tp']} marge={p['bb_margin']} RSI={p.get('rsi_name','?')} MACD={p['macd_filter']} size={p['tp_size']} -> aucun trade"
    return (f"  TP={p['tp']:>3} marge={p['bb_margin']:>3} RSI={p.get('rsi_name','base'):<10} "
            f"MACD={'ON ' if p['macd_filter'] else 'OFF'} size={p['tp_size']:>3} | "
            f"n={st['n']:>3} WR={st['wr']:>5.1f}% net={st['net']:>7.3f} exp={st['exp']:>6.3f} "
            f"(gain~{st['avg_w']:.3f}/perte~{st['avg_l']:.3f})")


def BinanceClientWrapper_quiet():
    # client direct (pas besoin du wrapper complet/logs)
    return Client(Config.BINANCE_API_KEY, Config.BINANCE_SECRET_KEY)


if __name__ == "__main__":
    main()
