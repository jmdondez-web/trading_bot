#!/usr/bin/env python3
"""JM Trading Bot V3.4 - Backtesting"""
import sys
import pandas as pd
from datetime import datetime, timedelta
from config import Config
from binance_client import BinanceClientWrapper
from strategy import StrategyEngine

def backtest(symbol, days=90):
    print(f"Backtest {symbol} sur {days} jours...")
    client = BinanceClientWrapper()
    df = client.get_klines(symbol, limit=500)
    if df.empty:
        print("Aucune donnée")
        return

    trades = []
    position = None

    for i in range(50, len(df)):
        chunk = df.iloc[:i+1]
        result = StrategyEngine.analyze(chunk, symbol)

        if position is None and result['decision'] == 'ALLOW':
            position = {'entry': result['price'], 'stop': result['price'] - result['atr'] * 1.8}
        elif position is not None:
            current = result['price']
            if current <= position['stop']:
                pnl = (current - position['entry']) / position['entry'] * 100
                trades.append({'entry': position['entry'], 'exit': current, 'pnl': pnl, 'type': 'STOP'})
                position = None
            elif current >= position['entry'] * 1.03:
                pnl = 3.0
                trades.append({'entry': position['entry'], 'exit': current, 'pnl': pnl, 'type': 'TP'})
                position = None

    if trades:
        winners = len([t for t in trades if t['pnl'] > 0])
        winrate = winners / len(trades) * 100
        total_pnl = sum(t['pnl'] for t in trades)
        print(f"Trades: {len(trades)} | Winrate: {winrate:.1f}% | P&L total: {total_pnl:.1f}%")
    else:
        print("Aucun trade généré")

if __name__ == "__main__":
    symbol = sys.argv[1] if len(sys.argv) > 1 else "BTCUSDC"
    days = int(sys.argv[2]) if len(sys.argv) > 2 else 90
    backtest(symbol, days)