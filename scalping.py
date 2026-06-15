import logging
from datetime import datetime, timedelta
from config import Config

logger = logging.getLogger("ScalpingEngine")

class ScalpingEngine:
    def __init__(self, signal_manager):
        self.signal_manager = signal_manager
        self.active = Config.SCALP_MODE

    def is_active(self):
        return self.active and Config.SCALP_MODE

    def get_params(self):
        return {
            "mode": "SCALPING" if Config.SCALP_MODE else "NORMAL",
            "timeframe": Config.SCALP_TIMEFRAME if Config.SCALP_MODE else "5m",
            "rsi_period": Config.SCALP_RSI_PERIOD if Config.SCALP_MODE else Config.RSI_PERIOD,
            "rsi_threshold": Config.SCALP_RSI_THRESHOLD if Config.SCALP_MODE else "par paire",
            "take_profit": f"{Config.SCALP_TAKE_PROFIT}%" if Config.SCALP_MODE else f"{Config.TAKE_PROFIT_PERCENT}%",
            "stop_loss_atr": Config.SCALP_STOP_LOSS_ATR if Config.SCALP_MODE else Config.STOP_LOSS_ATR_MULTIPLIER,
            "max_trades_hour": Config.SCALP_MAX_TRADES_HOUR if Config.SCALP_MODE else "illimité",
            "cooldown": f"{Config.SCALP_COOLDOWN} min" if Config.SCALP_MODE else f"{Config.SIGNAL_COOLDOWN_MINUTES} min",
            "max_positions": Config.SCALP_MAX_POSITIONS if Config.SCALP_MODE else Config.MAX_POSITIONS,
        }

    def activate(self):
        Config.SCALP_MODE = True
        Config.TURBO_MODE = False
        self.active = True
        logger.info("Mode Scalping activé")

    def deactivate(self):
        Config.SCALP_MODE = False
        self.active = False
        logger.info("Mode Scalping désactivé")