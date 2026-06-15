#!/usr/bin/env python3
"""JM Trading Bot V3.4 - Point d'entrée"""
import time
import logging
import traceback
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler

from config import Config
from database import Database
from binance_client import BinanceClientWrapper
from websocket_client import WebSocketClient
from orders import OrderManager
from risk import RiskManager
from signals import SignalManager
from scalping import ScalpingEngine
from dust_manager import DustManager
from scanner import DynamicPairScanner
from committee import TradingCommittee
from telegram_interface import TelegramInterface
from dashboard import Dashboard
import gspread
from oauth2client.service_account import ServiceAccountCredentials

log_handler = RotatingFileHandler("bot_v3.4.log", maxBytes=10*1024*1024, backupCount=3)
log_handler.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))
logging.basicConfig(level=logging.INFO, handlers=[log_handler, logging.StreamHandler()])
logger = logging.getLogger("TradingCoreV3.4")

class GoogleSheetsClient:
    def __init__(self):
        scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
        creds = ServiceAccountCredentials.from_json_keyfile_name(Config.GOOGLE_CREDS_FILE, scope)
        self.client = gspread.authorize(creds)
        self.sheet = self.client.open_by_key(Config.GOOGLE_SHEET_KEY).worksheet("Journal")

    def append_row(self, data):
        try: self.sheet.append_row(data, value_input_option='USER_ENTERED')
        except: pass

class TelegramNotifier:
    def __init__(self):
        self.token = Config.TELEGRAM_TOKEN
        self.chat_id = Config.TELEGRAM_CHAT_ID

    def send(self, message):
        if not self.token or not self.chat_id: return
        try:
            import requests
            requests.post(f"https://api.telegram.org/bot{self.token}/sendMessage",
                         data={'chat_id': self.chat_id, 'text': message[:4000]}, timeout=5)
        except: pass

class TradingCore:
    def __init__(self):
        self.binance = BinanceClientWrapper()
        self.sheets = GoogleSheetsClient()
        self.telegram = TelegramNotifier()
        self.db = Database()
        # FIX: binance_wrapper passé en 3e argument — sans lui, get_real_quantity()
        # retournait toujours 0.0 et la quantité DB était utilisée sans vérification Binance
        self.order_manager = OrderManager(self.binance.client, self.db, self.binance)
        self.risk = RiskManager(self.db, self.binance, self.order_manager, self.telegram)
        self.dust = DustManager(self.db, self.binance, self.order_manager, self.telegram)
        self.dynamic_scanner = DynamicPairScanner(self.binance)
        self.committee = TradingCommittee(self.db)
        self.signals = SignalManager(self.db, self.binance, self.order_manager, self.risk, self.telegram)
        self.scalping = ScalpingEngine(self.signals)
        self.websocket = WebSocketClient(self.binance, self.risk, self.dust)
        self.telegram_interface = TelegramInterface(self)
        self.dashboard = Dashboard(self.db, self.binance)
        self.last_sync = None
        self._restore_config()
        self._sync_positions_from_binance()
        self.websocket.start()
        self.dashboard.run()

    def _restore_config(self):
        conf = self.db.get_all_config()
        for key, attr in [
            ('rsi_thresholds','RSI_THRESHOLDS'),('cooldown_minutes','SIGNAL_COOLDOWN_MINUTES'),
            ('stop_atr_multiplier','STOP_LOSS_ATR_MULTIPLIER'),('trailing_activation','TRAILING_STOP_ACTIVATION'),
            ('trailing_distance','TRAILING_STOP_DISTANCE'),('macd_filter','MACD_FILTER_ENABLED'),
            ('capital_risk_percent','CAPITAL_RISK_PERCENT'),('heartbeat_hours','HEARTBEAT_HOURS'),
            ('bb_margin_percent','BB_MARGIN_PERCENT'),('take_profit_percent','TAKE_PROFIT_PERCENT'),
            ('take_profit_size','TAKE_PROFIT_SIZE'),('auto_mode','AUTO_MODE'),('auto_scan','AUTO_SCAN'),
            ('max_positions','MAX_POSITIONS'),('range_vr_threshold','RANGE_VR_THRESHOLD'),
            ('turbo_mode','TURBO_MODE'),('committee_enabled','COMMITTEE_ENABLED'),
            ('night_mode','NIGHT_MODE'),('use_trailing_atr','USE_TRAILING_ATR'),
            ('cooldown_adaptive','COOLDOWN_ADAPTIVE'),('scalp_mode','SCALP_MODE'),
            ('dust_auto_sell','DUST_AUTO_SELL'),
            # FIX: dry_run et core_symbols étaient en DB mais jamais restaurés.
            # dry_run manquant = bot redémarre en DRY_RUN (défaut code) alors
            # qu'il était en production → ordres ignorés silencieusement ou inversement.
            ('dry_run','DRY_RUN'),('core_symbols','CORE_SYMBOLS'),
        ]:
            if key in conf:
                value = conf[key]
                try:
                    import json
                    setattr(Config, attr, json.loads(value))
                except (ValueError, TypeError):
                    # FIX: les booléens Python ('True'/'False') ne sont pas du JSON valide
                    # (json exige 'true'/'false' en minuscules). Sans ce fallback, json.loads
                    # échoue et la chaîne brute "False" est stockée — toujours truthy en
                    # Python, ce qui activait TURBO_MODE/COMMITTEE_ENABLED malgré une valeur
                    # 'False' en DB (124 signaux rejetés silencieusement par le Comité).
                    try:
                        setattr(Config, attr, json.loads(value.lower()))
                    except (ValueError, TypeError, AttributeError):
                        setattr(Config, attr, value)

        # Log explicite du mode après restauration pour éviter toute ambiguïté
        mode_str = "DRY RUN" if Config.DRY_RUN else "PRODUCTION"
        turbo_str = " + TURBO" if Config.TURBO_MODE else ""
        scalp_str = " + SCALP" if Config.SCALP_MODE else ""
        logger.info(f"Config restaurée — Mode: {mode_str}{turbo_str}{scalp_str}")

    def _sync_positions_from_binance(self):
        try:
            balances = self.binance.get_all_balances()
            db_positions = {p['symbol']: p for p in self.db.get_positions()}
            for asset, amount in balances.items():
                if asset == 'USDC': continue
                symbol = asset + 'USDC'
                price = self.binance.get_current_price(symbol) or 0
                value = amount * price
                if amount > 0 and price > 0 and value >= Config.MIN_USDC_BALANCE:
                    if symbol not in db_positions:
                        # FIX: entry_price = prix courant est une approximation.
                        # Si le bot redémarre avec une position en perte, le vrai
                        # prix d'entrée est perdu et le P&L sera incorrect.
                        # On logue un avertissement et on utilise un stop plus serré (ATR)
                        # plutôt que le 2% hardcodé.
                        logger.warning(
                            f"SYNC: position {symbol} trouvée sur Binance sans entrée DB. "
                            f"entry_price approximé au prix courant ({price:.4f}). "
                            f"Le vrai P&L d'entrée est inconnu."
                        )
                        atr_stop = price * 0.985  # 1.5% par défaut, mieux que 2% flat
                        self.db.add_position({
                            'symbol': symbol, 'entry_price': price, 'quantity': amount,
                            'initial_stop': atr_stop, 'current_stop': atr_stop,
                            'order_id': 0, 'status': 'ACTIVE',
                            'entry_time': datetime.now().isoformat(), 'tp_triggered': False
                        })
                        self.telegram.send(
                            f"⚠️ Position {symbol} récupérée depuis Binance\n"
                            f"Prix approx: {price:.4f} USDC\n"
                            f"Stop: {atr_stop:.4f} (-1.5%)\n"
                            f"P&L d'entrée réel inconnu — vérifier manuellement."
                        )
            for symbol in list(db_positions.keys()):
                asset = symbol.replace('USDC', '')
                if asset not in balances:
                    self.db.remove_position(symbol)
            self.last_sync = datetime.now()
        except Exception as e:
            logger.error(f"Erreur sync: {e}")

    def check_heartbeat(self):
        last_hb = self.db.get_config('last_heartbeat')
        last_hb = datetime.fromisoformat(last_hb) if last_hb else None
        if not last_hb or (datetime.now() - last_hb) > timedelta(hours=Config.HEARTBEAT_HOURS):
            self.db.set_config('last_heartbeat', datetime.now().isoformat())
            self.telegram.send(f"Heartbeat V3.4 - Balance: {self.binance.get_account_balance('USDC'):.2f} USDC")

    def check_periodic_sync(self):
        if not self.last_sync or (datetime.now() - self.last_sync) > timedelta(minutes=Config.SYNC_BALANCE_MINUTES):
            self._sync_positions_from_binance()

    def run_forever(self):
        mode = "DRY RUN" if Config.DRY_RUN else "PRODUCTION"
        if Config.SCALP_MODE: mode += " SCALPING"
        elif Config.TURBO_MODE: mode += " TURBO"
        self.telegram.send(f"Bot V3.4\nMode: {mode}\nCommandes: /help")
        last_cmd = datetime.now()
        while True:
            try:
                self.check_heartbeat()
                self.check_periodic_sync()
                # FIX: check_entry_orders() supprimé ici.
                # Le WebSocketClient._check_orders() (polling 2s) gère déjà les ordres
                # PENDING avec timeout. Garder les deux causait une double vérification
                # et des appels API redondants à chaque cycle de 1s.
                self.risk.check_take_profit()
                self.risk.check_stop_loss()
                self.dust.check_and_clean()
                signals = self.signals.scan_all_pairs(self.dynamic_scanner)
                for signal in signals:
                    self.signals.execute_signal(signal, self.committee)
                if (datetime.now() - last_cmd).total_seconds() >= Config.TELEGRAM_CHECK_SECONDS:
                    self.telegram_interface.check()
                    last_cmd = datetime.now()
                time.sleep(Config.LOOP_SLEEP_SECONDS)  # FIX: utilise LOOP_SLEEP_SECONDS (45s)
                                                        # au lieu de time.sleep(1) qui bouclait
                                                        # 45x trop vite et saturait l'API Binance
            except KeyboardInterrupt:
                self.telegram.send("Bot arrete."); break
            except Exception as e:
                logger.critical(f"Crash: {e}\n{traceback.format_exc()}")
                self.telegram.send(f"Crash: {e}"); time.sleep(120)

if __name__ == "__main__":
    Config.validate()
    TradingCore().run_forever()