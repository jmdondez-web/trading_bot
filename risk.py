import logging
import time
from datetime import datetime
from config import Config
from strategy import StrategyEngine

logger = logging.getLogger("RiskManager")

# Frais Binance spot standard (0.1% par leg, soit 0.2% aller-retour)
# Avec BNB pour payer les frais : 0.075% par leg
BINANCE_FEE_RATE = 0.001  # 0.1% — conservateur, sans BNB discount

class RiskManager:
    def __init__(self, db, binance_client, order_manager, telegram):
        self.db = db
        self.binance = binance_client
        self.orders = order_manager
        self.telegram = telegram
        # FIX: cache ATR et prix pour éviter N appels API par cycle
        # Structure: {symbol: (valeur, timestamp)}
        self._atr_cache = {}
        self._price_cache = {}
        self._ATR_TTL = 60    # secondes — l'ATR sur 5m bouge peu en 1 minute
        self._PRICE_TTL = 3   # secondes — prix frais mais pas un appel par check

    def _get_cached_price(self, symbol):
        """Prix avec cache TTL=3s — partagé entre check_take_profit et check_stop_loss."""
        now = time.time()
        if symbol in self._price_cache:
            price, ts = self._price_cache[symbol]
            if now - ts < self._PRICE_TTL:
                return price
        price = self.binance.get_current_price(symbol)
        if price:
            self._price_cache[symbol] = (price, now)
        return price

    def _calc_fees(self, entry_price, exit_price, quantity):
        """Calcule les frais aller-retour sur une position."""
        buy_fee  = entry_price * quantity * BINANCE_FEE_RATE
        sell_fee = exit_price  * quantity * BINANCE_FEE_RATE
        return buy_fee + sell_fee

    def check_take_profit(self):
        for pos in self.db.get_positions():
            if pos.get('tp_triggered'): continue
            tp = Config.SCALP_TAKE_PROFIT if Config.SCALP_MODE else Config.TAKE_PROFIT_PERCENT
            if tp <= 0: continue
            # FIX: prix depuis cache — évite un appel API séparé de check_stop_loss
            cp = self._get_cached_price(pos['symbol'])
            if not cp: continue
            entry = float(pos['entry_price'])
            gain = (cp - entry) / entry * 100
            if gain >= tp:
                self.db.update_position(pos['symbol'], {'tp_triggered': True})
                sell_qty = self.orders.adjust_quantity(pos['symbol'], float(pos['quantity']) * (Config.TAKE_PROFIT_SIZE / 100))
                if sell_qty * cp < Config.MIN_USDC_BALANCE:
                    sell_qty = float(pos['quantity'])
                    remaining = 0
                else:
                    remaining = max(0, float(pos['quantity']) - sell_qty)
                if sell_qty <= 0: continue
                # FIX: pnl_usdc inclut maintenant les frais aller-retour
                fees = self._calc_fees(entry, cp, sell_qty)
                pnl_usdc_net = (cp - entry) * sell_qty - fees
                self.telegram.send(
                    f"TAKE PROFIT +{tp}%\n{pos['symbol']}\n"
                    f"Vendu: {sell_qty:.6f} @ {cp:.4f}\n"
                    f"Gain brut: {gain:.2f}% | Net frais: {pnl_usdc_net:.4f} USDC"
                )
                order = self.orders.place_market_sell_order(pos['symbol'], sell_qty)
                if order:
                    self.db.add_trade({
                        'symbol': pos['symbol'], 'entry_price': entry, 'exit_price': cp,
                        'pnl_pct': gain, 'pnl_usdc': pnl_usdc_net,  # FIX: net
                        'exit_reason': 'TAKE_PROFIT'
                    })
                    if remaining <= 0: self.db.remove_position(pos['symbol'])
                    else: self.db.update_position(pos['symbol'], {'quantity': remaining, 'tp_triggered': False})

    def check_stop_loss(self):
        for pos in self.db.get_positions():
            # FIX: prix depuis cache — si check_take_profit vient de passer,
            # le prix est déjà là, pas de second appel API
            cp = self._get_cached_price(pos['symbol'])
            if not cp: continue
            entry = float(pos['entry_price'])
            current_stop = float(pos.get('current_stop', pos['initial_stop']))

            if cp > entry:
                atr_mult = Config.SCALP_STOP_LOSS_ATR if Config.SCALP_MODE else Config.TRAILING_ATR_MULTIPLIER
                gain_pct = (cp - entry) / entry

                if Config.USE_TRAILING_ATR:
                    if gain_pct >= Config.TRAILING_STOP_ACTIVATION:
                        # FIX: ATR depuis cache — évite get_klines() à chaque cycle
                        atr = self._get_atr(pos['symbol'])
                        if atr > 0:
                            new_stop = cp - (atr * atr_mult)
                            if new_stop > current_stop:
                                self.db.update_position(pos['symbol'], {'current_stop': new_stop})
                else:
                    if gain_pct >= Config.TRAILING_STOP_ACTIVATION:
                        new_stop = cp * (1 - Config.TRAILING_STOP_DISTANCE)
                        if new_stop > current_stop:
                            self.db.update_position(pos['symbol'], {'current_stop': new_stop})

            if cp <= current_stop:
                reason = "STOP LOSS" if cp <= float(pos['initial_stop']) else "TRAILING STOP"
                qty = float(pos['quantity'])
                pnl_pct = (cp - entry) / entry * 100
                # FIX: pnl_usdc net de frais
                fees = self._calc_fees(entry, cp, qty)
                pnl_usdc_net = (cp - entry) * qty - fees
                self.telegram.send(
                    f"{reason}\n{pos['symbol']}\n"
                    f"Sortie: {cp:.4f}\n"
                    f"P&L: {pnl_pct:.2f}% | Net frais: {pnl_usdc_net:.4f} USDC"
                )
                order = self.orders.place_market_sell_order(pos['symbol'], qty)
                if order:
                    self.db.add_trade({
                        'symbol': pos['symbol'], 'entry_price': entry, 'exit_price': cp,
                        'pnl_pct': pnl_pct, 'pnl_usdc': pnl_usdc_net,  # FIX: net
                        'exit_reason': reason
                    })
                    self.db.remove_position(pos['symbol'])
                    # Invalide le cache prix après fermeture de position
                    self._price_cache.pop(pos['symbol'], None)

    def _get_atr(self, symbol):
        """ATR avec cache TTL=60s — évite get_klines() à chaque cycle pour chaque position."""
        now = time.time()
        if symbol in self._atr_cache:
            atr_val, ts = self._atr_cache[symbol]
            if now - ts < self._ATR_TTL:
                return atr_val
        # Cache expiré ou absent : recalcul
        df = self.binance.get_klines(symbol, limit=50)
        if df.empty:
            return 0
        atr_series = StrategyEngine.calculate_atr(df)
        atr_val = float(atr_series.iloc[-1]) if len(atr_series) > 0 else 0
        self._atr_cache[symbol] = (atr_val, now)
        return atr_val

    def check_entry_orders(self):
        for pos in self.db.get_positions():
            if pos.get('status') == 'PENDING':
                status = self.orders.get_order_status(pos['symbol'], pos['order_id'])
                if status == "FILLED":
                    self.telegram.send(f"ACHAT EXECUTE\n{pos['symbol']}\nStop: {float(pos['initial_stop']):.2f}")
                    self.db.update_position(pos['symbol'], {'status': 'ACTIVE'})
                elif status in ["CANCELED","EXPIRED","REJECTED"]:
                    self.db.add_rejected_order(pos['symbol'], f"Ordre {status}")
                    self.telegram.send(f"Ordre {pos['symbol']} annule ({status})")
                    self.db.remove_position(pos['symbol'])

    def invalidate_price_cache(self, symbol=None):
        """Permet au code externe d'invalider le cache prix (ex: après un achat)."""
        if symbol:
            self._price_cache.pop(symbol, None)
        else:
            self._price_cache.clear()