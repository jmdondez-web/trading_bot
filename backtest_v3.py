#!/usr/bin/env python3
"""Backtest V3 — capital fictif partagé qui se compose.

Différence clé vs backtest_v2.py :
 - v2 raisonnait par trade à notional fixe (100/trade), symboles indépendants.
 - v3 simule UN portefeuille fictif unique (défaut 500 USDC) partagé entre les 4
   symboles, sur une timeline chronologique commune, en répliquant la vraie
   logique d'allocation du bot :
     * MAX_POSITIONS positions simultanées max
     * CAPITAL_SPLIT (50/30/20) selon le nombre de positions déjà ouvertes
     * CAPITAL_RISK_PERCENT (95%) du cash disponible
     * MIN_USDC_BALANCE : pas d'entrée si la part calculée passe sous le minimum
     * cooldown SIGNAL_COOLDOWN_MINUTES par symbole après une sortie
 - frais 0.1%/leg sur chaque jambe (achat + vente, y compris TP partiel)
 - sortie : stop ATR initial, trailing ATR, TP partiel (mêmes maths que v2)

Le capital se compose : la taille de chaque position dépend du cash courant,
donc gains et pertes s'enchaînent comme en réel. Résultat : « 500 → X USDC ».

Usage : ./venv/bin/python backtest_v3.py [jours] [capital]
        ./venv/bin/python backtest_v3.py 60 500
"""
import sys
import numpy as np
import pandas as pd
from binance.client import Client
from config import Config
from strategy import StrategyEngine

# Réutilise les briques éprouvées de v2 (indicateurs + éligibilité d'entrée)
from backtest_v2 import fetch, precompute, eligibility, FEE, CORE


def simulate_portfolio(data, elig_by_symbol, p, start_capital, timeline=None,
                       regime_ok=None, exit_on_regime=False):
    """Simule un portefeuille fictif partagé sur une timeline chronologique.

    data            : {symbol: ind}  (sortie de precompute, + 'ts' ajouté)
    elig_by_symbol  : {symbol: [bool]} éligibilité d'entrée pour cette config
    p               : config (tp, tp_size, ...)
    timeline        : liste de timestamps à simuler (défaut : union de tous)
    regime_ok       : {ts: bool} filtre de régime de marché (BTC). Si fourni,
                      AUCUNE entrée n'est autorisée aux instants où c'est False.
                      (la gestion des sorties n'est jamais bloquée.)
    Retour          : dict stats (capital final, trades, winrate, drawdown).
    """
    # sorties : configurables via p (fallback sur Config si absent) — permet de
    # balayer le stop ATR / trailing / seuil d'activation, jusque-là figés.
    smult = p.get('stop', Config.STOP_LOSS_ATR_MULTIPLIER)
    tmult = p.get('trail', Config.TRAILING_ATR_MULTIPLIER)
    activ = p.get('activ', Config.TRAILING_STOP_ACTIVATION)
    # cap adaptatif optionnel : limite la distance de stop INITIALE à stop_cap%
    # du prix d'entrée (ne mord que sur les paires à fort ATR, sinon inerte).
    stop_cap = p.get('stop_cap')
    tp_frac = p['tp'] / 100.0
    tp_size = p['tp_size'] / 100.0
    max_pos = Config.MAX_POSITIONS
    split_tbl = Config.CAPITAL_SPLIT
    risk = Config.CAPITAL_RISK_PERCENT
    min_bal = Config.MIN_USDC_BALANCE
    cooldown_bars = max(1, Config.SIGNAL_COOLDOWN_MINUTES // 5)
    warmup = Config.BB_PERIOD + 2

    # timeline commune : union triée des timestamps, mappée vers l'index local de chaque symbole
    ts_index = {s: {t: i for i, t in enumerate(data[s]['ts'])} for s in data}
    all_ts = timeline if timeline is not None else \
        sorted(set().union(*[set(d['ts']) for d in data.values()]))

    cash = start_capital
    positions = {}          # symbol -> dict(entry, stop, qty, tp_price, tp_done, cost, proceeds)
    last_exit_bar = {}      # symbol -> index global de dernière sortie (cooldown)
    trades = []             # pnl net par trade clôturé
    equity_curve = []

    def close_value(sym, t):
        ind = data[sym]; idx = ts_index[sym].get(t)
        return None if idx is None else ind['close'][idx]

    for gbar, t in enumerate(all_ts):
        # 1) gestion des positions ouvertes (sorties d'abord : libère cash & slots)
        for sym in list(positions.keys()):
            ind = data[sym]; idx = ts_index[sym].get(t)
            if idx is None:
                continue
            pos = positions[sym]
            price = ind['close'][idx]; a = ind['atr'][idx]
            hi = ind['high'][idx]; lo = ind['low'][idx]
            # trailing ATR une fois le seuil d'activation franchi
            if a == a and a > 0 and (price - pos['entry']) / pos['entry'] >= activ:
                ns = price - a * tmult
                if ns > pos['stop']:
                    pos['stop'] = ns
            # stop d'abord (pessimiste), puis TP
            if lo <= pos['stop']:
                ex = pos['stop']
                proceeds = pos['qty'] * ex * (1 - FEE)
                pos['proceeds'] += proceeds
                trades.append(pos['proceeds'] - pos['cost'])
                cash += proceeds
                last_exit_bar[sym] = gbar
                del positions[sym]
                continue
            if hi >= pos['tp_price']:
                sell = pos['qty'] * tp_size
                proceeds = sell * pos['tp_price'] * (1 - FEE)
                pos['proceeds'] += proceeds
                cash += proceeds
                if tp_size >= 1.0:
                    trades.append(pos['proceeds'] - pos['cost'])
                    last_exit_bar[sym] = gbar
                    del positions[sym]
                else:
                    pos['qty'] -= sell
                    if pos['stop'] < pos['entry']:
                        pos['stop'] = pos['entry']  # break-even sécurisé

        # 2) entrées : bloquées globalement si le régime de marché est défavorable
        if regime_ok is not None and not regime_ok.get(t, True):
            # rôle « interrupteur + sortie » : on liquide tout si BTC bascule baissier
            if exit_on_regime:
                for sym in list(positions.keys()):
                    cv = close_value(sym, t)
                    if cv is None:
                        continue
                    pos = positions[sym]
                    proceeds = pos['qty'] * cv * (1 - FEE)
                    pos['proceeds'] += proceeds
                    trades.append(pos['proceeds'] - pos['cost'])
                    cash += proceeds
                    last_exit_bar[sym] = gbar
                    del positions[sym]
            equity_curve.append(cash + sum(
                pos['qty'] * (close_value(s, t) or pos['entry'])
                for s, pos in positions.items()))
            continue
        for sym in data:
            if sym in positions:
                continue
            if len(positions) >= max_pos:
                break
            idx = ts_index[sym].get(t)
            if idx is None or idx < warmup:
                continue
            if gbar - last_exit_bar.get(sym, -10**9) < cooldown_bars:
                continue
            if not elig_by_symbol[sym][idx]:
                continue
            a = data[sym]['atr'][idx]; price = data[sym]['close'][idx]
            if not (a == a and a > 0):
                continue
            pos_idx = len(positions)
            split = split_tbl[min(pos_idx, len(split_tbl) - 1)]
            capital = cash * risk * split
            if capital < min_bal:
                continue
            qty = capital / price
            cost = capital * (1 + FEE)   # notional + frais d'achat
            cash -= cost
            stop_dist = a * smult
            if stop_cap:
                stop_dist = min(stop_dist, price * stop_cap)   # plafond adaptatif
            positions[sym] = {
                'entry': price, 'stop': price - stop_dist, 'qty': qty,
                'tp_price': price * (1 + tp_frac), 'cost': cost, 'proceeds': 0.0,
            }

        # 3) équité courante (cash + valeur de marché des positions ouvertes)
        mv = 0.0
        for sym, pos in positions.items():
            cv = close_value(sym, t)
            mv += pos['qty'] * (cv if cv is not None else pos['entry'])
        equity_curve.append(cash + mv)

    # liquidation finale au dernier close connu
    last_t = all_ts[-1]
    for sym, pos in list(positions.items()):
        cv = close_value(sym, last_t)
        ex = cv if cv is not None else pos['entry']
        proceeds = pos['qty'] * ex * (1 - FEE)
        pos['proceeds'] += proceeds
        trades.append(pos['proceeds'] - pos['cost'])
        cash += proceeds

    final = cash
    n = len(trades)
    wins = [t for t in trades if t > 0]
    wr = len(wins) / n * 100 if n else 0.0
    # max drawdown sur la courbe d'équité
    peak = -1e18; mdd = 0.0
    for e in equity_curve:
        peak = max(peak, e)
        if peak > 0:
            mdd = max(mdd, (peak - e) / peak * 100)
    return {
        'final': final, 'ret': (final - start_capital) / start_capital * 100,
        'n': n, 'wr': wr, 'mdd': mdd,
    }


def fmt(p, st):
    if not st or st['n'] == 0:
        return (f"  TP={p['tp']:>3} marge={p['bb_margin']:>3} RSI={p.get('rsi_name','base'):<10} "
                f"MACD={'ON ' if p['macd_filter'] else 'OFF'} size={p['tp_size']:>3} -> aucun trade")
    return (f"  TP={p['tp']:>3} marge={p['bb_margin']:>3} RSI={p.get('rsi_name','base'):<10} "
            f"MACD={'ON ' if p['macd_filter'] else 'OFF'} size={p['tp_size']:>3} | "
            f"capital={st['final']:>8.2f} ({st['ret']:>+6.1f}%) "
            f"n={st['n']:>3} WR={st['wr']:>5.1f}% DD={st['mdd']:>4.1f}%")


def run_config(data, p, start_capital, timeline=None, regime_ok=None):
    elig = {s: eligibility(ind, s, p['bb_margin'], p['rsi'], p['macd_filter'])
            for s, ind in data.items()}
    return simulate_portfolio(data, elig, p, start_capital, timeline, regime_ok)


def btc_regime(data, period, slope=False):
    """Construit {ts: bool} = 'BTC porteur' à chaque instant.
    Régime favorable si close BTC > SMA(period). Si slope=True, exige en plus
    que la SMA soit montante (tendance haussière, pas juste au-dessus)."""
    btc = data['BTCUSDC']
    close = np.array(btc['close'], dtype=float)
    ts = btc['ts']
    sma = np.full(len(close), np.nan)
    if len(close) >= period:
        c = np.cumsum(np.insert(close, 0, 0))
        sma[period - 1:] = (c[period:] - c[:-period]) / period
    ok = {}
    for i, t in enumerate(ts):
        if np.isnan(sma[i]):
            ok[t] = False
            continue
        fav = close[i] > sma[i]
        if slope and i > 0 and not np.isnan(sma[i - 1]):
            fav = fav and sma[i] > sma[i - 1]
        ok[t] = bool(fav)
    return ok


# seuils RSI réutilisés
RSI_SETS = {
    'actuel': {"BTCUSDC": 45, "ETHUSDC": 44, "SOLUSDC": 40, "BNBUSDC": 43},
    'serre':  {"BTCUSDC": 40, "ETHUSDC": 39, "SOLUSDC": 36, "BNBUSDC": 38},
    'tres_serre': {"BTCUSDC": 35, "ETHUSDC": 34, "SOLUSDC": 32, "BNBUSDC": 34},
}


def robust(days, start_capital):
    """Teste une shortlist de configs sur plusieurs fenêtres (30/60/90j)
    pour juger la robustesse temporelle de l'edge."""
    client = Client(Config.BINANCE_API_KEY, Config.BINANCE_SECRET_KEY)
    print(f"Chargement {days}j d'historique 5m pour {CORE}  |  capital fictif = {start_capital:.0f} USDC\n", flush=True)
    data = {}
    full_ts = set()
    for s in CORE:
        df = fetch(client, s, days)
        if len(df) < 100:
            print(f"  {s}: données insuffisantes ({len(df)})"); continue
        ind = precompute(df); ind['ts'] = df['timestamp'].tolist()
        data[s] = ind; full_ts |= set(ind['ts'])
        print(f"  {s}: {len(df)} bougies", flush=True)
    full_ts = sorted(full_ts)
    max_ts = full_ts[-1]

    periods = [30, 60, 90]
    shortlist = [
        ('LIVE REELLE (DB, MACD-OFF)', {'tp': 1.0, 'bb_margin': 1.5, 'rsi': RSI_SETS['tres_serre'],
                             'macd_filter': False, 'tp_size': 50, 'rsi_name': 'tres_serre'}),
        ('config.py defaut (TP3/MACD-OFF)', {'tp': 3.0, 'bb_margin': 2.5, 'rsi': RSI_SETS['actuel'],
                             'macd_filter': False, 'tp_size': 100, 'rsi_name': 'actuel'}),
        ('profil A voulu (MACD-ON, size50)', {'tp': 1.0, 'bb_margin': 1.5, 'rsi': RSI_SETS['tres_serre'],
                             'macd_filter': True, 'tp_size': 50, 'rsi_name': 'tres_serre'}),
        ('profil B (MACD-ON, size100)', {'tp': 1.0, 'bb_margin': 1.5, 'rsi': RSI_SETS['tres_serre'],
                             'macd_filter': True, 'tp_size': 100, 'rsi_name': 'tres_serre'}),
        ('tp1.3/tres_serre/MACD-ON', {'tp': 1.3, 'bb_margin': 1.5, 'rsi': RSI_SETS['tres_serre'],
                             'macd_filter': True, 'tp_size': 100, 'rsi_name': 'tres_serre'}),
    ]

    print(f"\n=== ROBUSTESSE — capital final / retour% / n trades / winrate / drawdown ===")
    print("  {:<28}".format("CONFIG") + "".join(f"| {str(d)+'j':<24}" for d in periods))
    for name, p in shortlist:
        cells = []
        for d in periods:
            cutoff = max_ts - d * 86400 * 1000
            tl = [t for t in full_ts if t >= cutoff]
            st = run_config(data, p, start_capital, tl)
            if st and st['n']:
                cells.append(f"{st['final']:>7.1f} ({st['ret']:>+5.1f}%) n={st['n']:>3} WR{st['wr']:>4.0f}% DD{st['mdd']:>4.0f}%")
            else:
                cells.append("            — aucun trade —")
        print("  {:<28}".format(name) + "".join(f"| {c:<24}" for c in cells))


def _load(days):
    client = Client(Config.BINANCE_API_KEY, Config.BINANCE_SECRET_KEY)
    print(f"Chargement {days}j d'historique 5m pour {CORE}...", flush=True)
    data = {}; full = set()
    for s in CORE:
        df = fetch(client, s, days)
        if len(df) < 100:
            print(f"  {s}: données insuffisantes ({len(df)})"); continue
        ind = precompute(df); ind['ts'] = df['timestamp'].tolist()
        data[s] = ind; full |= set(ind['ts'])
        print(f"  {s}: {len(df)} bougies", flush=True)
    return data, sorted(full)


def _windows(data, full_ts, p, start_capital, periods=(30, 60, 90)):
    """Retourne {periode: stats} pour une config sur plusieurs fenêtres glissantes."""
    max_ts = full_ts[-1]
    out = {}
    for d in periods:
        cutoff = max_ts - d * 86400 * 1000
        tl = [t for t in full_ts if t >= cutoff]
        out[d] = run_config(data, p, start_capital, tl)
    return out


def exitsweep(start_capital):
    """Balaye la logique de SORTIE (stop ATR / trailing / activation) × TP,
    avec le filtre d'entrée fort (RSI très serré + MACD ON). Classe sur 90j."""
    data, full_ts = _load(90)
    cutoff60 = full_ts[-1] - 60 * 86400 * 1000
    cutoff90 = full_ts[0]
    tl90 = full_ts  # 90j = tout

    rsi = RSI_SETS['tres_serre']
    tp_grid = [0.8, 1.0, 1.5, 2.0]
    stop_grid = [1.0, 1.5, 1.8, 2.5, 3.5]
    trail_grid = [1.0, 1.5, 2.5]
    activ_grid = [0.005, 0.010, 0.015, 0.025]

    results = []
    for tp in tp_grid:
        for stop in stop_grid:
            for trail in trail_grid:
                for activ in activ_grid:
                    p = {'tp': tp, 'bb_margin': 1.5, 'rsi': rsi, 'macd_filter': True,
                         'tp_size': 100, 'rsi_name': 'tres_serre',
                         'stop': stop, 'trail': trail, 'activ': activ}
                    st = run_config(data, p, start_capital, tl90)
                    if st and st['n'] >= 20:
                        results.append((p, st))
    results.sort(key=lambda x: x[1]['final'], reverse=True)

    print(f"\n=== EXIT SWEEP — TOP 15 sur 90j (capital {start_capital:.0f}, RSI très serré + MACD ON, min 20 trades) ===")
    print(f"  {'TP':>4} {'stop':>5} {'trail':>6} {'activ':>6} | {'cap90':>8} {'ret%':>7} {'n':>4} {'WR':>5} {'DD':>5}")
    for p, st in results[:15]:
        print(f"  {p['tp']:>4} {p['stop']:>5} {p['trail']:>6} {p['activ']:>6} | "
              f"{st['final']:>8.1f} {st['ret']:>+6.1f}% {st['n']:>4} {st['wr']:>4.0f}% {st['mdd']:>4.0f}%")

    print(f"\n=== ROBUSTESSE 30/60/90j des 6 meilleures ===")
    print("  {:<34}".format("config (tp/stop/trail/activ)") + "".join(f"| {str(d)+'j':<22}" for d in (30, 60, 90)))
    for p, _ in results[:6]:
        w = _windows(data, full_ts, p, start_capital)
        name = f"tp{p['tp']}/s{p['stop']}/t{p['trail']}/a{p['activ']}"
        cells = []
        for d in (30, 60, 90):
            st = w[d]
            cells.append(f"{st['ret']:>+5.1f}% n={st['n']:>3} WR{st['wr']:>4.0f}% DD{st['mdd']:>3.0f}%" if st and st['n'] else "— aucun —")
        print("  {:<34}".format(name) + "".join(f"| {c:<22}" for c in cells))


def alts(start_capital):
    """Teste la stratégie (config profil A : RSI serré + MACD ON, TP=1.0) sur un
    univers d'alts plus volatils que les 4 grosses caps, fenêtres 30/60/90j.
    Chaque paire est aussi testée en SOLO (capital plein dédié) pour voir son edge
    propre, sans dilution par le partage de portefeuille."""
    global CORE
    universe = ["DOGEUSDC", "AVAXUSDC", "LINKUSDC", "INJUSDC", "SUIUSDC",
                "TIAUSDC", "NEARUSDC", "WLDUSDC"]
    client = Client(Config.BINANCE_API_KEY, Config.BINANCE_SECRET_KEY)
    print(f"Chargement 90j d'historique 5m, univers alts  |  capital {start_capital:.0f} USDC\n", flush=True)
    data = {}; full = set()
    for s in universe:
        try:
            df = fetch(client, s, 90)
        except Exception as e:
            print(f"  {s}: indisponible ({str(e)[:40]})"); continue
        if len(df) < 100:
            print(f"  {s}: données insuffisantes ({len(df)})"); continue
        ind = precompute(df); ind['ts'] = df['timestamp'].tolist()
        data[s] = ind; full |= set(ind['ts'])
        print(f"  {s}: {len(df)} bougies", flush=True)
    full_ts = sorted(full)

    # seuil RSI "très serré" appliqué uniformément aux alts (mean-reversion)
    rsi = {s: 34 for s in data}
    p = {'tp': 1.0, 'bb_margin': 1.5, 'rsi': rsi, 'macd_filter': True,
         'tp_size': 100, 'rsi_name': 'alts_serre'}

    # 1) edge SOLO par paire (portefeuille dédié à 1 seule paire)
    print(f"\n=== EDGE SOLO par paire (90j, capital {start_capital:.0f} dédié, RSI<=34 + MACD ON, TP=1.0) ===")
    print(f"  {'paire':<10} | {'cap90':>8} {'ret%':>7} {'n':>4} {'WR':>5} {'DD':>5}")
    solo_rows = []
    for s in data:
        CORE_bak = CORE; CORE = [s]
        st = simulate_portfolio({s: data[s]},
                                {s: eligibility(data[s], s, p['bb_margin'], p['rsi'], p['macd_filter'])},
                                p, start_capital, sorted(data[s]['ts']))
        CORE = CORE_bak
        solo_rows.append((s, st))
    for s, st in sorted(solo_rows, key=lambda x: x[1]['final'], reverse=True):
        if st and st['n']:
            print(f"  {s:<10} | {st['final']:>8.1f} {st['ret']:>+6.1f}% {st['n']:>4} {st['wr']:>4.0f}% {st['mdd']:>4.0f}%")
        else:
            print(f"  {s:<10} | — aucun trade —")

    # 2) portefeuille partagé sur tout l'univers, robustesse 30/60/90j
    CORE = list(data.keys())
    print(f"\n=== PORTEFEUILLE PARTAGÉ univers alts — robustesse 30/60/90j ===")
    w = _windows(data, full_ts, p, start_capital)
    for d in (30, 60, 90):
        st = w[d]
        if st and st['n']:
            print(f"  {d:>2}j : {st['final']:>8.1f} ({st['ret']:>+6.1f}%) n={st['n']:>3} WR={st['wr']:>4.0f}% DD={st['mdd']:>4.0f}%")
        else:
            print(f"  {d:>2}j : aucun trade")


def regime(start_capital):
    """Compare la config profil A (RSI très serré + MACD ON, TP=1.0) AVEC vs SANS
    filtre de régime BTC, sur 30/60/90j. Balaye la longueur de tendance BTC.
    1 bougie = 5 min ; period en bougies (48=4h, 96=8h, 200≈17h, 288=24h)."""
    data, full_ts = _load(90)
    p = {'tp': 1.0, 'bb_margin': 1.5, 'rsi': RSI_SETS['tres_serre'],
         'macd_filter': True, 'tp_size': 100, 'rsi_name': 'tres_serre'}

    variants = [('SANS filtre (baseline)', None)]
    for per in (48, 96, 200, 288):
        variants.append((f'BTC>SMA{per} ({per*5//60}h)', btc_regime(data, per)))
        variants.append((f'BTC>SMA{per} montante', btc_regime(data, per, slope=True)))

    print(f"\n=== FILTRE RÉGIME BTC — profil A, capital {start_capital:.0f} USDC ===")
    print("  {:<26}".format("filtre régime") + "".join(f"| {str(d)+'j':<24}" for d in (30, 60, 90)))
    for name, rok in variants:
        cells = []
        for d in (30, 60, 90):
            cutoff = full_ts[-1] - d * 86400 * 1000
            tl = [t for t in full_ts if t >= cutoff]
            st = run_config(data, p, start_capital, tl, rok)
            cells.append(
                f"{st['ret']:>+5.1f}% n={st['n']:>3} WR{st['wr']:>4.0f}% DD{st['mdd']:>3.0f}%"
                if st and st['n'] else "— aucun trade —")
        print("  {:<26}".format(name) + "".join(f"| {c:<24}" for c in cells))


# ───────────────────────── STRATÉGIE MOMENTUM ─────────────────────────
# Inverse de la mean-reversion : on achète la FORCE (cassure / tendance / momentum)
# au lieu de la faiblesse, et on laisse courir via le trailing (sorties réutilisées).

_INTERVALS = {'5m': Client.KLINE_INTERVAL_5MINUTE, '1h': Client.KLINE_INTERVAL_1HOUR}


def fetch_tf(client, symbol, days, tf):
    raw = client.get_historical_klines(symbol, _INTERVALS[tf], f"{days} day ago UTC")
    df = pd.DataFrame(raw, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume',
        'ct', 'qav', 'not', 'tbb', 'tbq', 'ig'])
    for c in ['open', 'high', 'low', 'close']:
        df[c] = df[c].astype(float)
    return df


def precompute_mom(df, ema_fast=9, ema_slow=21, hh_lookback=20):
    """Indicateurs causaux pour le momentum (aucun lookahead)."""
    close = df['close']
    rsi = StrategyEngine.calculate_rsi(close).values
    atr = StrategyEngine.calculate_atr(df).values
    macd_line, signal_line, _ = StrategyEngine.calculate_macd(close)
    macd_bull = (macd_line.values > signal_line.values)
    ema_f = close.ewm(span=ema_fast, adjust=False).mean().values
    ema_s = close.ewm(span=ema_slow, adjust=False).mean().values
    # plus haut des N bougies PRÉCÉDENTES (shift(1) => causal, exclut la bougie courante)
    roll_high = df['high'].rolling(hh_lookback).max().shift(1).values
    rising = np.zeros(len(rsi), dtype=bool)
    rising[1:] = rsi[1:] > rsi[:-1]   # RSI en hausse vs bougie précédente
    return {
        'close': close.values.tolist(), 'high': df['high'].values.tolist(),
        'low': df['low'].values.tolist(), 'atr': atr.tolist(),
        'rsi': rsi, 'ema_f': ema_f, 'ema_s': ema_s, 'roll_high': roll_high,
        'macd_bull': macd_bull, 'rising': rising, 'n': len(close),
        'ts': df['timestamp'].tolist(),
    }


def elig_mom(ind, entry_type):
    """Booléen d'entrée momentum selon le type (breakout / cross / momentum)."""
    c = np.array(ind['close']); rsi = ind['rsi']
    if entry_type == 'breakout':
        rh = ind['roll_high']
        sig = (~np.isnan(rh)) & (c > rh)               # cassure du plus haut récent
    elif entry_type == 'cross':
        ef, es = ind['ema_f'], ind['ema_s']
        prev = np.zeros(len(ef), dtype=bool)
        prev[1:] = (ef[:-1] <= es[:-1])
        sig = (ef > es) & prev                          # croisement haussier EMA
    elif entry_type == 'momentum':
        sig = (rsi > 55) & ind['rising'] & ind['macd_bull']   # accélération confirmée
    else:
        raise ValueError(entry_type)
    sig = sig & (~np.isnan(rsi))
    return sig.tolist()


def mom(start_capital):
    """Construit + balaye la stratégie momentum. Classe par COMPROMIS (gain/drawdown)."""
    global CORE
    entries = ['breakout', 'cross', 'momentum']
    # rôle BTC : off (aucun filtre) | switch (entrées seulement) | switch_exit (+ coupe)
    #            | btc_only (on ne trade que BTC lui-même)
    roles = ['off', 'switch', 'switch_exit', 'btc_only']
    stop_grid = [1.0, 1.5, 2.0]
    trail_grid = [1.5, 2.5, 3.5]
    activ_grid = [0.005, 0.015]
    tf_setup = [('5m', 90, 200), ('1h', 90, 50)]   # (timeframe, jours, période SMA BTC)

    rows = []   # (tf, entry, role, p, st90, data_key)
    store = {}  # (tf) -> (data, full_ts, regime_ok)

    for tf, days, btc_per in tf_setup:
        client = Client(Config.BINANCE_API_KEY, Config.BINANCE_SECRET_KEY)
        print(f"[{tf}] chargement {days}j {CORE}...", flush=True)
        data = {}; full = set()
        for s in CORE:
            df = fetch_tf(client, s, days, tf)
            if len(df) < 60:
                print(f"  {s}: insuffisant ({len(df)})"); continue
            ind = precompute_mom(df)
            data[s] = ind; full |= set(ind['ts'])
            print(f"  {s}: {len(df)} bougies", flush=True)
        full_ts = sorted(full)
        rok = btc_regime(data, btc_per, slope=True)   # BTC > SMA ET montante
        store[tf] = (data, full_ts)

        for entry in entries:
            elig_all = {s: elig_mom(ind, entry) for s, ind in data.items()}
            for role in roles:
                if role == 'btc_only':
                    sim_data = {'BTCUSDC': data['BTCUSDC']}
                    sim_elig = {'BTCUSDC': elig_all['BTCUSDC']}
                    rgate, xreg, tl = None, False, sorted(data['BTCUSDC']['ts'])
                else:
                    sim_data, sim_elig, tl = data, elig_all, full_ts
                    rgate = None if role == 'off' else rok
                    xreg = (role == 'switch_exit')
                for stop in stop_grid:
                    for trail in trail_grid:
                        for activ in activ_grid:
                            p = {'tp': 99.0, 'bb_margin': 0, 'rsi': {}, 'macd_filter': False,
                                 'tp_size': 100, 'stop': stop, 'trail': trail, 'activ': activ}
                            cur_core = CORE
                            CORE = list(sim_data.keys())
                            st = simulate_portfolio(sim_data, sim_elig, p, start_capital,
                                                    tl, rgate, xreg)
                            CORE = cur_core
                            if st and st['n'] >= 12:
                                rows.append((tf, entry, role, p, st))

    # score compromis : gain par unité de drawdown (favorise gain ET faible plongeon)
    def score(st):
        return st['ret'] / max(st['mdd'], 1.0)
    rows.sort(key=lambda r: score(r[4]), reverse=True)

    print(f"\n=== MOMENTUM — TOP 18 (capital {start_capital:.0f}, 90j, classé COMPROMIS gain/drawdown) ===")
    print(f"  {'tf':>3} {'entrée':<9} {'BTC':<11} {'stop':>4} {'trail':>5} {'act':>5} | "
          f"{'cap':>7} {'ret%':>6} {'DD':>4} {'n':>3} {'WR':>4} {'score':>6}")
    for tf, entry, role, p, st in rows[:18]:
        print(f"  {tf:>3} {entry:<9} {role:<11} {p['stop']:>4} {p['trail']:>5} {p['activ']:>5.3f} | "
              f"{st['final']:>7.1f} {st['ret']:>+5.1f}% {st['mdd']:>3.0f}% {st['n']:>3} "
              f"{st['wr']:>3.0f}% {score(st):>6.2f}")

    if rows:
        print(f"\n=== ROBUSTESSE 30/60/90j des 5 meilleures ===")
        for tf, entry, role, p, _ in rows[:5]:
            data, full_ts = store[tf]
            if role == 'btc_only':
                sim_data = {'BTCUSDC': data['BTCUSDC']}; tlbase = sorted(data['BTCUSDC']['ts'])
                rgate, xreg = None, False
            else:
                sim_data = data; tlbase = full_ts
                rok = btc_regime(data, 200 if tf == '5m' else 50, slope=True)
                rgate = None if role == 'off' else rok
                xreg = (role == 'switch_exit')
            sim_elig = {s: elig_mom(ind, entry) for s, ind in sim_data.items()}
            name = f"{tf}/{entry}/{role} s{p['stop']}/t{p['trail']}/a{p['activ']}"
            cells = []
            for d in (30, 60, 90):
                cutoff = tlbase[-1] - d * 86400 * 1000
                tl = [t for t in tlbase if t >= cutoff]
                cur = CORE; CORE = list(sim_data.keys())
                st = simulate_portfolio(sim_data, sim_elig, p, start_capital, tl, rgate, xreg)
                CORE = cur
                cells.append(f"{st['ret']:>+5.1f}% DD{st['mdd']:>3.0f}% n={st['n']:>3} WR{st['wr']:>3.0f}%"
                             if st and st['n'] else "— aucun —")
            print(f"  {name:<40}" + " | ".join(cells))


def momtune(start_capital):
    """Tuning ciblé de la structure gagnante : 1h / EMA cross / BTC switch.
    Balaye les params INTERNES du momentum (spans EMA, période tendance BTC,
    TP partiel) + stop/trail. Classé par compromis gain/drawdown, robustesse 30/60/90j."""
    global CORE
    client = Client(Config.BINANCE_API_KEY, Config.BINANCE_SECRET_KEY)
    print(f"[1h] chargement 90j {CORE}...", flush=True)
    raw = {};
    for s in CORE:
        df = fetch_tf(client, s, 90, '1h')
        if len(df) < 60:
            print(f"  {s}: insuffisant ({len(df)})"); continue
        raw[s] = df
        print(f"  {s}: {len(df)} bougies", flush=True)

    # grilles des params internes
    ema_pairs = [(9, 21), (5, 20), (8, 34), (12, 26), (20, 50)]
    btc_per_grid = [24, 50, 100, 200]      # longueur SMA BTC (en bougies 1h)
    stop_grid = [1.5, 2.0, 2.5]
    trail_grid = [2.5, 3.5, 5.0]
    tp_grid = [(99.0, 100), (4.0, 50), (3.0, 50)]   # (TP%, taille%) — 99=pas de TP

    # cache : precompute par paire de spans EMA (indép. du reste)
    pc_cache = {}
    def get_data(ef, es):
        key = (ef, es)
        if key not in pc_cache:
            d = {}; full = set()
            for s, df in raw.items():
                ind = precompute_mom(df, ema_fast=ef, ema_slow=es)
                d[s] = ind; full |= set(ind['ts'])
            pc_cache[key] = (d, sorted(full))
        return pc_cache[key]

    rows = []
    for (ef, es) in ema_pairs:
        data, full_ts = get_data(ef, es)
        elig_all = {s: elig_mom(ind, 'cross') for s, ind in data.items()}
        for btc_per in btc_per_grid:
            rok = btc_regime(data, btc_per, slope=True)
            for stop in stop_grid:
                for trail in trail_grid:
                    for (tp, tpsz) in tp_grid:
                        p = {'tp': tp, 'bb_margin': 0, 'rsi': {}, 'macd_filter': False,
                             'tp_size': tpsz, 'stop': stop, 'trail': trail, 'activ': 0.015}
                        cur = CORE; CORE = list(data.keys())
                        st = simulate_portfolio(data, elig_all, p, start_capital,
                                                full_ts, rok, False)
                        CORE = cur
                        if st and st['n'] >= 15:
                            rows.append(((ef, es), btc_per, p, st))

    def score(st):
        return st['ret'] / max(st['mdd'], 1.0)
    rows.sort(key=lambda r: score(r[3]), reverse=True)

    print(f"\n=== MOMENTUM TUNÉ — TOP 18 (1h/cross/BTC-switch, capital {start_capital:.0f}, 90j) ===")
    print(f"  {'EMA':>7} {'btcSMA':>6} {'stop':>4} {'trail':>5} {'TP':>8} | "
          f"{'cap':>7} {'ret%':>6} {'DD':>4} {'n':>3} {'WR':>4} {'score':>6}")
    for (ef, es), bp, p, st in rows[:18]:
        tpstr = "trail" if p['tp'] >= 99 else f"{p['tp']:.0f}%x{p['tp_size']}"
        print(f"  {ef:>2}/{es:<3} {bp:>6} {p['stop']:>4} {p['trail']:>5} {tpstr:>8} | "
              f"{st['final']:>7.1f} {st['ret']:>+5.1f}% {st['mdd']:>3.0f}% {st['n']:>3} "
              f"{st['wr']:>3.0f}% {score(st):>6.2f}")

    if rows:
        print(f"\n=== ROBUSTESSE 30/60/90j des 6 meilleures ===")
        for (ef, es), bp, p, _ in rows[:6]:
            data, full_ts = get_data(ef, es)
            elig_all = {s: elig_mom(ind, 'cross') for s, ind in data.items()}
            rok = btc_regime(data, bp, slope=True)
            tpstr = "trail" if p['tp'] >= 99 else f"{p['tp']:.0f}%x{p['tp_size']}"
            name = f"EMA{ef}/{es} btc{bp} s{p['stop']}/t{p['trail']} {tpstr}"
            cells = []
            for d in (30, 60, 90):
                cutoff = full_ts[-1] - d * 86400 * 1000
                tl = [t for t in full_ts if t >= cutoff]
                cur = CORE; CORE = list(data.keys())
                st = simulate_portfolio(data, elig_all, p, start_capital, tl, rok, False)
                CORE = cur
                cells.append(f"{st['ret']:>+5.1f}% DD{st['mdd']:>3.0f}% n={st['n']:>3} WR{st['wr']:>3.0f}%"
                             if st and st['n'] else "— aucun —")
            print(f"  {name:<38}" + " | ".join(cells))


def momalts(start_capital):
    """Valide la config momentum gagnante sur un univers d'alts (1h).
    BTC sert UNIQUEMENT de filtre de régime (chef d'orchestre), pas tradé."""
    global CORE
    universe = ["DOGEUSDC", "AVAXUSDC", "LINKUSDC", "INJUSDC", "SUIUSDC",
                "TIAUSDC", "NEARUSDC", "WLDUSDC"]
    client = Client(Config.BINANCE_API_KEY, Config.BINANCE_SECRET_KEY)
    print(f"[1h] BTC (filtre régime) + univers alts, 90j  |  capital {start_capital:.0f}\n", flush=True)

    # BTC : uniquement pour le régime (SMA200 1h, montante)
    btc_df = fetch_tf(client, "BTCUSDC", 90, '1h')
    rok = btc_regime({'BTCUSDC': precompute_mom(btc_df)}, 200, slope=True)
    print(f"  BTCUSDC: {len(btc_df)} bougies (filtre régime)", flush=True)

    data = {}; full = set()
    for s in universe:
        try:
            df = fetch_tf(client, s, 90, '1h')
        except Exception as e:
            print(f"  {s}: indisponible ({str(e)[:40]})"); continue
        if len(df) < 60:
            print(f"  {s}: insuffisant ({len(df)})"); continue
        ind = precompute_mom(df, ema_fast=5, ema_slow=20)
        data[s] = ind; full |= set(ind['ts'])
        print(f"  {s}: {len(df)} bougies", flush=True)
    full_ts = sorted(full)

    # config gagnante validée sur les majors
    p = {'tp': 4.0, 'bb_margin': 0, 'rsi': {}, 'macd_filter': False,
         'tp_size': 50, 'stop': 2.0, 'trail': 5.0, 'activ': 0.015}
    elig_all = {s: elig_mom(ind, 'cross') for s, ind in data.items()}

    # 1) edge SOLO par paire (capital dédié, filtre BTC actif)
    print(f"\n=== EDGE SOLO par paire (90j, EMA5/20 + BTC switch SMA200, TP4%x50) ===")
    print(f"  {'paire':<10} | {'cap90':>8} {'ret%':>7} {'DD':>4} {'n':>4} {'WR':>5}")
    solo = []
    for s in data:
        cur = CORE; CORE = [s]
        st = simulate_portfolio({s: data[s]}, {s: elig_all[s]}, p,
                                start_capital, sorted(data[s]['ts']), rok, False)
        CORE = cur
        solo.append((s, st))
    for s, st in sorted(solo, key=lambda x: (x[1]['final'] if x[1] else 0), reverse=True):
        if st and st['n']:
            print(f"  {s:<10} | {st['final']:>8.1f} {st['ret']:>+6.1f}% {st['mdd']:>3.0f}% {st['n']:>4} {st['wr']:>4.0f}%")
        else:
            print(f"  {s:<10} | — aucun trade —")

    # 2) portefeuille partagé sur tout l'univers alts, robustesse 30/60/90j
    cur = CORE; CORE = list(data.keys())
    print(f"\n=== PORTEFEUILLE PARTAGÉ univers alts — robustesse 30/60/90j ===")
    for d in (30, 60, 90):
        cutoff = full_ts[-1] - d * 86400 * 1000
        tl = [t for t in full_ts if t >= cutoff]
        st = simulate_portfolio(data, elig_all, p, start_capital, tl, rok, False)
        if st and st['n']:
            print(f"  {d:>2}j : {st['final']:>8.1f} ({st['ret']:>+6.1f}%) DD={st['mdd']:>3.0f}% n={st['n']:>3} WR={st['wr']:>4.0f}%")
        else:
            print(f"  {d:>2}j : aucun trade")
    CORE = cur


def main():
    if len(sys.argv) > 1 and sys.argv[1] == 'momalts':
        momalts(float(sys.argv[2]) if len(sys.argv) > 2 else 500.0)
        return
    if len(sys.argv) > 1 and sys.argv[1] == 'momtune':
        momtune(float(sys.argv[2]) if len(sys.argv) > 2 else 500.0)
        return
    if len(sys.argv) > 1 and sys.argv[1] == 'mom':
        mom(float(sys.argv[2]) if len(sys.argv) > 2 else 500.0)
        return
    if len(sys.argv) > 1 and sys.argv[1] == 'regime':
        regime(float(sys.argv[2]) if len(sys.argv) > 2 else 500.0)
        return
    if len(sys.argv) > 1 and sys.argv[1] == 'alts':
        alts(float(sys.argv[2]) if len(sys.argv) > 2 else 500.0)
        return
    if len(sys.argv) > 1 and sys.argv[1] == 'exitsweep':
        exitsweep(float(sys.argv[2]) if len(sys.argv) > 2 else 500.0)
        return
    if len(sys.argv) > 1 and sys.argv[1] == 'robust':
        start_capital = float(sys.argv[2]) if len(sys.argv) > 2 else 500.0
        robust(90, start_capital)
        return
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 60
    start_capital = float(sys.argv[2]) if len(sys.argv) > 2 else 500.0
    client = Client(Config.BINANCE_API_KEY, Config.BINANCE_SECRET_KEY)
    print(f"Chargement {days}j d'historique 5m pour {CORE}  |  capital fictif = {start_capital:.0f} USDC\n", flush=True)

    data = {}
    for s in CORE:
        df = fetch(client, s, days)
        if len(df) < 100:
            print(f"  {s}: données insuffisantes ({len(df)})")
            continue
        ind = precompute(df)
        ind['ts'] = df['timestamp'].tolist()   # pour l'alignement temporel
        data[s] = ind
        print(f"  {s}: {len(df)} bougies", flush=True)

    base = {'tp': Config.TAKE_PROFIT_PERCENT, 'bb_margin': Config.BB_MARGIN_PERCENT,
            'rsi': dict(Config.RSI_THRESHOLDS), 'macd_filter': Config.MACD_FILTER_ENABLED,
            'tp_size': Config.TAKE_PROFIT_SIZE, 'rsi_name': 'actuel'}
    print(f"\n=== CONFIG ACTUELLE (départ {start_capital:.0f} USDC) ===")
    print(fmt(base, run_config(data, base, start_capital)))

    tp_grid = [1.0, 1.3, 1.5, 2.0, 3.0]
    margin_grid = [0.0, 1.0, 1.5, 2.5]
    rsi_sets = {
        'actuel': {"BTCUSDC": 45, "ETHUSDC": 44, "SOLUSDC": 40, "BNBUSDC": 43},
        'serre':  {"BTCUSDC": 40, "ETHUSDC": 39, "SOLUSDC": 36, "BNBUSDC": 38},
        'tres_serre': {"BTCUSDC": 35, "ETHUSDC": 34, "SOLUSDC": 32, "BNBUSDC": 34},
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
                        st = run_config(data, p, start_capital)
                        if st and st['n'] >= 8:
                            results.append((p, st))

    results.sort(key=lambda x: x[1]['final'], reverse=True)
    print(f"\n=== TOP 15 configs (sur {len(results)} testées, min 8 trades) — tri par CAPITAL FINAL ===")
    for p, st in results[:15]:
        print(fmt(p, st))

    by_wr = sorted([r for r in results if r[1]['n'] >= 15], key=lambda x: x[1]['wr'], reverse=True)
    print(f"\n=== TOP 8 par WINRATE (min 15 trades) ===")
    for p, st in by_wr[:8]:
        print(fmt(p, st))


if __name__ == "__main__":
    main()
