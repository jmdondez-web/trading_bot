import json
import requests
from datetime import datetime, timedelta
from config import Config
from strategy import StrategyEngine
import logging

logger = logging.getLogger("TelegramInterface")

class TelegramInterface:
    def __init__(self, bot):
        self.bot = bot
        self.last_update_id = 0

    def check(self):
        try:
            url = f"https://api.telegram.org/bot{Config.TELEGRAM_TOKEN}/getUpdates"
            resp = requests.get(url, params={'offset': self.last_update_id + 1, 'timeout': 5}, timeout=10)
            if resp.status_code != 200: return
            for u in resp.json().get('result', []):
                self.last_update_id = u['update_id']
                text = u.get('message', {}).get('text', '')
                if str(u.get('message', {}).get('chat', {}).get('id', '')) != Config.TELEGRAM_CHAT_ID: continue
                self._dispatch(text)
        except: pass

    def _dispatch(self, text):
        parts = text.strip().split()
        if not parts or not parts[0].startswith('/'): return
        cmd = parts[0].lower()
        try:
            if cmd == '/status': self._cmd_status()
            elif cmd == '/positions': self._cmd_positions()
            elif cmd == '/pools': self._cmd_pools()
            elif cmd == '/pause': Config.DRY_RUN = True; self.bot.db.set_config('dry_run','True'); self.bot.telegram.send("Pause")
            elif cmd == '/resume': Config.DRY_RUN = False; self.bot.db.set_config('dry_run','False'); self.bot.telegram.send("Production")
            elif cmd == '/rsi' and len(parts) >= 3: self._set_rsi(parts)
            elif cmd == '/addpair' and len(parts) >= 3: self._add_pair(parts)
            elif cmd == '/removepair' and len(parts) >= 2: self._remove_pair(parts)
            elif cmd == '/cooldown' and len(parts) >= 2: self._set_cooldown(parts)
            elif cmd == '/stopatr' and len(parts) >= 2: self._set_stopatr(parts)
            elif cmd == '/trail' and len(parts) >= 2: self._set_trail(parts)
            elif cmd == '/traildist' and len(parts) >= 2: self._set_traildist(parts)
            elif cmd == '/macd' and len(parts) >= 2: self._set_macd(parts)
            elif cmd == '/capital' and len(parts) >= 2: self._set_capital(parts)
            elif cmd == '/heartbeat' and len(parts) >= 2: self._set_heartbeat(parts)
            elif cmd == '/bbmargin' and len(parts) >= 2: self._set_bbmargin(parts)
            elif cmd == '/takeprofit' and len(parts) >= 2: self._set_takeprofit(parts)
            elif cmd == '/tpsize' and len(parts) >= 2: self._set_tpsize(parts)
            elif cmd == '/maxpos' and len(parts) >= 2: self._set_maxpos(parts)
            elif cmd == '/vrange' and len(parts) >= 2: self._set_vrange(parts)
            elif cmd == '/sell' and len(parts) >= 2: self._cmd_sell(parts)
            elif cmd == '/rejects': self._cmd_rejects()
            elif cmd == '/rejects' and len(parts) >= 2 and parts[1] == 'clear': self.bot.db.clear_rejected_orders(); self.bot.telegram.send("Rejets effacés")
            elif cmd == '/resetparams': self._reset_params()
            elif cmd == '/params': self._cmd_params()
            elif cmd == '/help': self._cmd_help()
        except Exception as e:
            self.bot.telegram.send(f"Erreur: {e}")

    def _cmd_status(self):
        mode = "DRY RUN" if Config.DRY_RUN else "PRODUCTION"
        balance = self.bot.binance.get_account_balance("USDC")
        positions = self.bot.db.get_positions()
        total_bot = balance + sum(float(p['quantity']) * (self.bot.binance.get_current_price(p['symbol']) or 0) for p in positions)
        cumulative = self.bot.db.get_cumulative_pnl()
        msg = f"STATUT BOT V3.4\nMode: {mode}\nBalance: {balance:.2f} USDC\nCapital bot: ~{total_bot:.2f} USDC\nPositions: {len(positions)}\nP&L cumulé: {cumulative:.2f} USDC\n"
        self.bot._sync_positions_from_binance()
        balances = self.bot.binance.get_all_balances()
        total_binance = 0
        msg += "\nCOMPTE BINANCE:\n"
        for asset, amount in balances.items():
            if asset == 'USDC': total_binance += amount; msg += f"USDC: {amount:.2f}\n"
            else:
                price = self.bot.binance.get_current_price(asset+'USDC') or 0
                value = amount * price; total_binance += value
                msg += f"{asset}: {amount:.6f} (~{value:.2f} USDC)\n"
        msg += f"Total: ~{total_binance:.2f} USDC"
        self.bot.telegram.send(msg)

    def _cmd_positions(self):
        positions = self.bot.db.get_positions()
        if not positions: self.bot.telegram.send("Aucune position."); return
        msg = "POSITIONS V3.4"
        for p in positions:
            cp = self.bot.binance.get_current_price(p['symbol'])
            if cp:
                pnl = (cp - float(p['entry_price'])) / float(p['entry_price']) * 100
                msg += f"\n{p['symbol']}: {float(p['entry_price']):.4f} -> {cp:.4f} | P&L {pnl:.2f}% | Stop: {float(p['current_stop']):.4f}"
        self.bot.telegram.send(msg)

    def _cmd_pools(self):
        pairs = list(Config.CORE_SYMBOLS)
        msg = f"POOL V3.4 ({len(pairs)} paires)\n"
        for symbol in pairs:
            df = self.bot.binance.get_klines(symbol, limit=60)
            if df.empty: continue
            result = StrategyEngine.analyze(df, symbol)
            score = StrategyEngine.compute_score(df, symbol)
            stars = "???" if score > 3 else ("??" if score > 1.5 else ("?" if score > 0 else "-"))
            msg += f"{stars} {symbol}: Score {score:.1f} | RSI {result['rsi']:.1f} | {result['context_state']}\n"
        self.bot.telegram.send(msg)

    def _cmd_rejects(self):
        rejects = self.bot.db.get_rejected_orders(10)
        if not rejects: self.bot.telegram.send("Aucun ordre rejeté."); return
        msg = "ORDRES REJETÉS"
        for r in rejects:
            msg += f"\n{r['symbol']}: {r['reason']} ({r['timestamp'][:16]})"
        self.bot.telegram.send(msg)

    def _cmd_sell(self, parts):
        """Vente manuelle d'une position (100% ou partielle), via Telegram."""
        symbol = parts[1].upper()
        if not symbol.endswith('USDC'): symbol += 'USDC'
        percentage = float(parts[2]) if len(parts) >= 3 else 100
        bot = self.bot
        pos = bot.db.get_position(symbol)
        if not pos:
            bot.telegram.send("Position non trouvée")
            return
        qty = bot.orders.adjust_quantity(symbol, float(pos['quantity']) * (percentage / 100))
        if qty <= 0:
            bot.telegram.send("Quantité trop petite")
            return
        order = bot.orders.place_market_sell_order(symbol, qty)
        if order:
            cp = bot.binance.get_current_price(symbol)
            entry = float(pos['entry_price'])
            pnl = (cp - entry) / entry * 100 if cp else 0
            if percentage >= 100:
                bot.db.remove_position(symbol)
            else:
                bot.db.update_position(symbol, {'quantity': float(pos['quantity']) - qty})
            bot.db.add_trade({
                'symbol': symbol, 'entry_price': entry, 'exit_price': cp or 0,
                'pnl_pct': pnl, 'pnl_usdc': (cp - entry) * qty if cp else 0,
                'exit_reason': 'MANUAL_SELL'
            })
            bot.telegram.send(f"{symbol} vendu {qty:.6f} @ {cp:.4f} | P&L: {pnl:.2f}%")
        else:
            bot.telegram.send("Erreur lors de la vente")

    def _cmd_params(self):
        msg = (f"PARAMETRES V3.4\n"
               f"Paires: {Config.CORE_SYMBOLS}\n"
               f"RSI: {Config.RSI_THRESHOLDS}\n"
               f"Cooldown: {Config.SIGNAL_COOLDOWN_MINUTES} min\n"
               f"Stop ATR: x{Config.STOP_LOSS_ATR_MULTIPLIER}\n"
               f"Capital: {Config.CAPITAL_RISK_PERCENT*100:.0f}%\n"
               f"BB Margin: {Config.BB_MARGIN_PERCENT}%\n"
               f"TP: {Config.TAKE_PROFIT_PERCENT}% (vente {Config.TAKE_PROFIT_SIZE}%)")
        self.bot.telegram.send(msg)

    def _cmd_help(self):
        msg = ("V3.4: /status /positions /pools /params /help\n"
               "/pause /resume\n"
               "/sell BTCUSDC /sell BTCUSDC 50\n"
               "/rsi BTC 45 /addpair ADAUSDC 38 /removepair ADAUSDC\n"
               "/cooldown 30 /stopatr 1.8 /trail 0.015 /traildist 0.012\n"
               "/macd on|off /capital 95 /heartbeat 1 /bbmargin 2.5\n"
               "/takeprofit 3 /tpsize 50 /maxpos 3 /vrange 1.0\n"
               "/rejects /resetparams")
        self.bot.telegram.send(msg)

    def _reset_params(self):
        for k, v in Config.DEFAULTS.items():
            setattr(Config, k, v)
            self.bot.db.set_config(k.lower(), json.dumps(v) if isinstance(v, dict) else str(v))
        Config.CORE_SYMBOLS = ["BTCUSDC","ETHUSDC","SOLUSDC","BNBUSDC"]
        Config.RSI_THRESHOLDS = {"BTCUSDC":45,"ETHUSDC":44,"SOLUSDC":40,"BNBUSDC":43}
        self.bot.telegram.send("Parametres reinitialises")
        self._cmd_params()

    def _set_rsi(self, parts): sym = parts[1].upper() + ('USDC' if not parts[1].upper().endswith('USDC') else ''); Config.RSI_THRESHOLDS[sym] = float(parts[2]); self.bot.db.set_config('rsi_thresholds', json.dumps(Config.RSI_THRESHOLDS)); self.bot.telegram.send(f"RSI {sym} = {parts[2]}")
    def _add_pair(self, parts): sym = parts[1].upper() + ('USDC' if not parts[1].upper().endswith('USDC') else ''); Config.RSI_THRESHOLDS[sym] = float(parts[2]); Config.CORE_SYMBOLS.append(sym) if sym not in Config.CORE_SYMBOLS else None; self.bot.db.set_config('rsi_thresholds', json.dumps(Config.RSI_THRESHOLDS)); self.bot.db.set_config('core_symbols', json.dumps(Config.CORE_SYMBOLS)); self.bot.telegram.send(f"Paire ajoutee: {sym}")
    def _remove_pair(self, parts): sym = parts[1].upper() + ('USDC' if not parts[1].upper().endswith('USDC') else ''); Config.CORE_SYMBOLS.remove(sym) if sym in Config.CORE_SYMBOLS else None; Config.RSI_THRESHOLDS.pop(sym, None); self.bot.db.set_config('core_symbols', json.dumps(Config.CORE_SYMBOLS)); self.bot.telegram.send(f"Paire retiree: {sym}")
    def _set_cooldown(self, parts): Config.SIGNAL_COOLDOWN_MINUTES = int(parts[1]); self.bot.db.set_config('cooldown_minutes', parts[1]); self.bot.telegram.send(f"Cooldown = {parts[1]} min")
    def _set_stopatr(self, parts): Config.STOP_LOSS_ATR_MULTIPLIER = float(parts[1]); self.bot.db.set_config('stop_atr_multiplier', parts[1]); self.bot.telegram.send(f"Stop ATR = x{parts[1]}")
    def _set_trail(self, parts): Config.TRAILING_STOP_ACTIVATION = float(parts[1]); self.bot.db.set_config('trailing_activation', parts[1]); self.bot.telegram.send(f"Trailing act = {float(parts[1])*100:.1f}%")
    def _set_traildist(self, parts): Config.TRAILING_STOP_DISTANCE = float(parts[1]); self.bot.db.set_config('trailing_distance', parts[1]); self.bot.telegram.send(f"Trailing dist = {float(parts[1])*100:.1f}%")
    def _set_macd(self, parts): Config.MACD_FILTER_ENABLED = parts[1].lower() == 'on'; self.bot.db.set_config('macd_filter', str(Config.MACD_FILTER_ENABLED)); self.bot.telegram.send(f"MACD = {'ON' if Config.MACD_FILTER_ENABLED else 'OFF'}")
    def _set_capital(self, parts): Config.CAPITAL_RISK_PERCENT = float(parts[1]) / 100.0; self.bot.db.set_config('capital_risk_percent', str(Config.CAPITAL_RISK_PERCENT)); self.bot.telegram.send(f"Capital = {parts[1]}%")
    def _set_heartbeat(self, parts): Config.HEARTBEAT_HOURS = int(parts[1]); self.bot.db.set_config('heartbeat_hours', parts[1]); self.bot.telegram.send(f"Heartbeat = {parts[1]}h")
    def _set_bbmargin(self, parts): Config.BB_MARGIN_PERCENT = float(parts[1]); self.bot.db.set_config('bb_margin_percent', parts[1]); self.bot.telegram.send(f"Marge BB = {parts[1]}%")
    def _set_takeprofit(self, parts): Config.TAKE_PROFIT_PERCENT = float(parts[1]); self.bot.db.set_config('take_profit_percent', parts[1]); self.bot.telegram.send(f"Take Profit = {parts[1]}%")
    def _set_tpsize(self, parts): Config.TAKE_PROFIT_SIZE = float(parts[1]); self.bot.db.set_config('take_profit_size', parts[1]); self.bot.telegram.send(f"TP Size = {parts[1]}%")
    def _set_maxpos(self, parts): Config.MAX_POSITIONS = int(parts[1]); self.bot.db.set_config('max_positions', parts[1]); self.bot.telegram.send(f"Max positions = {parts[1]}")
    def _set_vrange(self, parts): Config.RANGE_VR_THRESHOLD = float(parts[1]); self.bot.db.set_config('range_vr_threshold', parts[1]); self.bot.telegram.send(f"Seuil RANGE = {parts[1]}")
