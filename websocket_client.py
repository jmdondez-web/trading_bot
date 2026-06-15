import threading
import time
import logging
from datetime import datetime
from config import Config

logger = logging.getLogger("WebSocketClient")

class WebSocketClient:
    def __init__(self, binance_client, risk_manager, dust_manager):
        self.binance = binance_client
        self.risk = risk_manager
        self.dust = dust_manager
        self.running = False
        self.callbacks = []

    def start(self):
        if not Config.USE_WEBSOCKET:
            logger.info("WebSocket désactivé")
            return

        self.running = True
        threading.Thread(target=self._listen, daemon=True).start()
        logger.info("WebSocket démarré (polling 2s)")

    def _listen(self):
        while self.running:
            try:
                self._check_orders()
                # FIX: dust.check_and_clean() supprimé d'ici.
                # Il était appelé toutes les 2s EN PARALLÈLE de la boucle principale
                # → risque de double vente du même asset si DUST_AUTO_SELL=True.
                # La boucle principale (bot_v3.4.py) reste le seul appelant.
            except Exception as e:
                logger.debug(f"WebSocket poll: {e}")
            time.sleep(2)

    def _check_orders(self):
        """Vérifie les ordres PENDING et applique le timeout ORDER_TIMEOUT_SECONDS.

        FIX: ajout du timeout — un ordre LIMIT non exécuté bloquait indéfiniment
        un slot MAX_POSITIONS et immobilisait du capital USDC sans jamais être annulé.
        """
        for pos in self.risk.db.get_positions():
            if pos.get('status') != 'PENDING':
                continue

            symbol = pos['symbol']
            order_id = pos['order_id']
            status = self.risk.orders.get_order_status(symbol, order_id)

            if status == "FILLED":
                self.risk.db.update_position(symbol, {'status': 'ACTIVE'})
                logger.info(f"Ordre {symbol} exécuté → ACTIVE")
                for cb in self.callbacks:
                    cb(symbol, 'BUY', float(pos['entry_price']), float(pos['quantity']))

            elif status in ["CANCELED", "EXPIRED", "REJECTED"]:
                logger.info(f"Ordre {symbol} annulé par Binance ({status})")
                self.risk.db.remove_position(symbol)

            else:
                # FIX: timeout sur ordres PENDING trop anciens
                try:
                    entry_time = datetime.fromisoformat(pos['entry_time'])
                    age_seconds = (datetime.now() - entry_time).total_seconds()
                    if age_seconds > Config.ORDER_TIMEOUT_SECONDS:
                        logger.warning(
                            f"Ordre {symbol} timeout après {age_seconds:.0f}s "
                            f"(limite: {Config.ORDER_TIMEOUT_SECONDS}s) — annulation"
                        )
                        if not Config.DRY_RUN:
                            try:
                                self.binance.client.cancel_order(symbol=symbol, orderId=order_id)
                            except Exception as cancel_err:
                                logger.error(f"Échec annulation {symbol}: {cancel_err}")
                        self.risk.db.remove_position(symbol)
                        if hasattr(self.risk, 'telegram'):
                            self.risk.telegram.send(
                                f"⏱ Ordre {symbol} annulé (timeout {Config.ORDER_TIMEOUT_SECONDS}s)"
                            )
                except (ValueError, TypeError) as e:
                    logger.error(f"entry_time invalide pour {symbol}: {e}")

    def on_fill(self, callback):
        self.callbacks.append(callback)

    def stop(self):
        self.running = False
