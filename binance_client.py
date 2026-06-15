import pandas as pd
import time
import threading
from binance.client import Client
from binance.exceptions import BinanceAPIException
from config import Config
import logging

logger = logging.getLogger("BinanceClient")

class BinanceClientWrapper:
    def __init__(self, testnet=False):
        self.testnet = testnet
        api_key = Config.BINANCE_API_KEY
        api_secret = Config.BINANCE_SECRET_KEY
        if testnet:
            self.client = Client(api_key, api_secret, testnet=True)
        else:
            self.client = Client(api_key, api_secret)
        self.client.get_account()
        logger.info(f"Binance {'Testnet' if testnet else 'OK'}")

        # FIX: cache centralisé pour get_current_price()
        # Avant: chaque module (risk, dashboard, signals) appelait get_current_price()
        # indépendamment -> N appels API pour le même symbol dans le même cycle.
        # Maintenant: un seul appel par symbol par TTL, partagé entre tous les appelants.
        # TTL=3s : prix suffisamment frais pour le trading 5m, sans saturer l'API.
        self._price_cache = {}      # {symbol: (price, timestamp)}
        self._price_lock = threading.Lock()
        self._PRICE_TTL = 3.0       # secondes

        # FIX: cache balance USDC — appelée à chaque execute_signal() et heartbeat
        # TTL=10s : la balance ne change que lors d'un achat/vente
        self._balance_cache = {}    # {asset: (balance, timestamp)}
        self._balance_lock = threading.Lock()
        self._BALANCE_TTL = 10.0    # secondes

    def get_klines(self, symbol, limit=100, interval=None):
        if interval is None:
            interval = Client.KLINE_INTERVAL_1MINUTE if Config.SCALP_MODE \
                else Client.KLINE_INTERVAL_5MINUTE
        try:
            candles = self.client.get_klines(symbol=symbol, interval=interval, limit=limit)
            df = pd.DataFrame(candles, columns=[
                'timestamp','open','high','low','close','volume',
                'close_time','quote_asset_volume','number_of_trades',
                'taker_buy_base_asset_volume','taker_buy_quote_asset_volume','ignore'
            ])
            df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
            for col in ['close','high','low','open']:
                df[col] = df[col].astype(float)
            return df
        except Exception as e:
            logger.error(f"Erreur klines {symbol}: {e}")
            return pd.DataFrame()

    def get_account_balance(self, asset="USDC"):
        """Balance avec cache TTL=10s — évite un appel API à chaque scan de signal."""
        now = time.time()
        with self._balance_lock:
            if asset in self._balance_cache:
                balance, ts = self._balance_cache[asset]
                if now - ts < self._BALANCE_TTL:
                    return balance

        try:
            balance = float(self.client.get_asset_balance(asset=asset)['free'])
            with self._balance_lock:
                self._balance_cache[asset] = (balance, now)
            return balance
        except:
            return 0.0

    def get_current_price(self, symbol):
        """Prix avec cache TTL=3s — partagé entre risk, dashboard et signals."""
        now = time.time()
        with self._price_lock:
            if symbol in self._price_cache:
                price, ts = self._price_cache[symbol]
                if now - ts < self._PRICE_TTL:
                    return price

        try:
            price = float(self.client.get_symbol_ticker(symbol=symbol)['price'])
            with self._price_lock:
                self._price_cache[symbol] = (price, now)
            return price
        except:
            return None

    def invalidate_price_cache(self, symbol=None):
        """Invalide le cache prix après un achat/vente pour forcer un refresh immédiat."""
        with self._price_lock:
            if symbol:
                self._price_cache.pop(symbol, None)
            else:
                self._price_cache.clear()

    def invalidate_balance_cache(self, asset=None):
        """Invalide le cache balance après un ordre exécuté."""
        with self._balance_lock:
            if asset:
                self._balance_cache.pop(asset, None)
            else:
                self._balance_cache.clear()

    def get_all_usdc_pairs(self):
        try:
            info = self.client.get_exchange_info()
            return [
                s['symbol'] for s in info['symbols']
                if s['symbol'].endswith('USDC') and s['status'] == 'TRADING'
            ]
        except:
            return []

    def get_24h_volume(self, symbol):
        try:
            return float(self.client.get_ticker(symbol=symbol)['quoteVolume'])
        except:
            return 0.0

    def get_all_balances(self):
        try:
            account = self.client.get_account()
            return {
                b['asset']: float(b['free']) + float(b['locked'])
                for b in account['balances']
                if float(b['free']) + float(b['locked']) > 0
            }
        except:
            return {}
