import numpy as np
import threading
from datetime import datetime
from binance.client import Client
from binance.exceptions import BinanceAPIException
from config import Config
import logging

logger = logging.getLogger("OrderManager")

class OrderManager:
    def __init__(self, client: Client, db=None, binance_wrapper=None):
        self.client = client
        self.db = db
        self.binance = binance_wrapper
        self.exchange_info = {}
        self._load_filters()
        self._error_counts = {}

    def _load_filters(self):
        try:
            for s in self.client.get_exchange_info()['symbols']:
                f = {x['filterType']: x for x in s['filters']}
                self.exchange_info[s['symbol']] = {
                    'lot_size': float(f['LOT_SIZE']['stepSize']),
                    'min_qty': float(f['LOT_SIZE']['minQty']),
                    'min_notional': float(f.get('MIN_NOTIONAL',{}).get('minNotional',10.0))
                }
        except Exception as e:
            logger.error(f"Erreur filtres: {e}")

    def calculate_order_quantity(self, symbol, usdc_amount, current_price):
        if symbol not in self.exchange_info: return None
        r = self.exchange_info[symbol]
        raw = usdc_amount / current_price
        step = r['lot_size']
        prec = int(round(-np.log10(step)))
        qty = round(np.floor(raw / step) * step, prec)
        if qty < r['min_qty'] or (qty * current_price) < r['min_notional']:
            if self.db:
                self.db.add_rejected_order(symbol, f"Quantité {qty} sous minimum")
            return None
        return qty

    def adjust_quantity(self, symbol, quantity):
        if symbol not in self.exchange_info: return 0.0
        step = self.exchange_info[symbol]['lot_size']
        prec = int(round(-np.log10(step)))
        return round(np.floor(quantity / step) * step, prec)

    def get_real_quantity(self, symbol):
        """Récupère la quantité réelle disponible sur Binance."""
        if self.binance:
            balances = self.binance.get_all_balances()
            asset = symbol.replace('USDC', '')
            return balances.get(asset, 0.0)
        return 0.0

    def _handle_sell_error(self, symbol, error_msg):
        self._error_counts[symbol] = self._error_counts.get(symbol, 0) + 1
        if self._error_counts[symbol] >= 3:
            logger.warning(f"Position fantôme {symbol} après 3 échecs - suppression")
            if self.db:
                self.db.add_rejected_order(symbol, "Position fantôme - supprimée après 3 échecs")
                self.db.remove_position(symbol)
            self._error_counts[symbol] = 0
            return True
        return False

    def place_limit_buy_order(self, symbol, quantity, current_price):
        qty = self.adjust_quantity(symbol, quantity)
        if qty <= 0: return None
        if Config.DRY_RUN:
            logger.info(f"DRY RUN - ACHAT {symbol} x{qty}")
            return {"orderId": int(datetime.now().timestamp()), "price": current_price, "symbol": symbol, "origQty": qty}
        try:
            limit_price = round(current_price * 0.9995, 2)
            order = self.client.create_order(symbol=symbol, side=Client.SIDE_BUY, type=Client.ORDER_TYPE_LIMIT,
                                             timeInForce=Client.TIME_IN_FORCE_GTC, quantity=qty, price=limit_price)
            logger.info(f"ORDRE ACHAT: {symbol} {qty} @ {limit_price}")
            return order
        except BinanceAPIException as e:
            logger.error(f"Erreur achat: {e.message}")
            return None

    def place_market_buy_order(self, symbol, quantity):
        qty = self.adjust_quantity(symbol, quantity)
        if qty <= 0: return None
        if Config.DRY_RUN:
            logger.info(f"DRY RUN - ACHAT MARKET {symbol} x{qty}")
            return {"orderId": int(datetime.now().timestamp()), "price": 0, "symbol": symbol, "origQty": qty}
        try:
            order = self.client.create_order(symbol=symbol, side=Client.SIDE_BUY, type=Client.ORDER_TYPE_MARKET, quantity=qty)
            logger.info(f"ORDRE ACHAT MARKET: {symbol} {qty}")
            return order
        except BinanceAPIException as e:
            logger.error(f"Erreur achat market: {e.message}")
            return None

    def place_market_sell_order(self, symbol, quantity):
        if Config.DRY_RUN:
            # FIX: en DRY_RUN la position est simulée et ne correspond à aucun
            # solde réel sur Binance. Utiliser get_real_quantity() ici reprenait
            # la poussière du compte réel (ex: 6.65e-06 BTC), arrondie à 0 par
            # adjust_quantity (step 1e-05) → "Quantité nulle (vente)" en boucle,
            # la position n'était jamais clôturée et le STOP LOSS était renvoyé
            # par Telegram à chaque cycle (~1 min).
            qty = self.adjust_quantity(symbol, quantity)
        else:
            # Utiliser la quantité réelle disponible sur Binance
            real_qty = self.get_real_quantity(symbol)
            if real_qty > 0:
                qty = self.adjust_quantity(symbol, real_qty)
            else:
                qty = self.adjust_quantity(symbol, quantity)

        if qty <= 0:
            if self.db: self.db.add_rejected_order(symbol, "Quantité nulle (vente)")
            return None
        if Config.DRY_RUN:
            logger.info(f"DRY RUN - VENTE {symbol} x{qty}")
            return {"orderId": int(datetime.now().timestamp()), "price": 0, "symbol": symbol, "origQty": qty}
        try:
            order = self.client.create_order(symbol=symbol, side=Client.SIDE_SELL, type=Client.ORDER_TYPE_MARKET, quantity=qty)
            logger.info(f"ORDRE VENTE: {symbol} {qty}")
            self._error_counts[symbol] = 0
            return order
        except BinanceAPIException as e:
            logger.error(f"Erreur vente: {e.message}")
            self._handle_sell_error(symbol, e.message)
            if self.db: self.db.add_rejected_order(symbol, f"API Vente: {e.message}")
            return None

    def get_order_status(self, symbol, order_id):
        if Config.DRY_RUN: return "FILLED"
        try: return self.client.get_order(symbol=symbol, orderId=order_id)['status']
        except: return "UNKNOWN"
