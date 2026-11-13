import os
from dotenv import load_dotenv

load_dotenv()

class Config:
    # Secrets
    BINANCE_API_KEY = os.getenv("BINANCE_API_KEY")
    BINANCE_SECRET_KEY = os.getenv("BINANCE_SECRET_KEY")
    TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
    TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

    # Noyau dur
    CORE_SYMBOLS = ["BTCUSDC", "ETHUSDC", "SOLUSDC", "BNBUSDC"]

    # Stratégie Mean Reversion
    RSI_THRESHOLDS = {"BTCUSDC": 45, "ETHUSDC": 44, "SOLUSDC": 40, "BNBUSDC": 43}
    SIGNAL_COOLDOWN_MINUTES = 30
    STOP_LOSS_ATR_MULTIPLIER = 1.8
    TRAILING_STOP_ACTIVATION = 0.015  # FIX: était 0.005 (0.5%) — trop tôt, coupait avant de couvrir les frais
                                       # Nouveau: 1.5% — le trailing ne démarre qu'après avoir absorbé les frais (0.2%) et dégagé un vrai gain
    TRAILING_STOP_DISTANCE = 0.012    # FIX: était 0.008 (0.8%) — trop serré sur la volatilité 5m
                                       # Nouveau: 1.2% — laisse respirer sans sacrifier trop de gain
    USE_TRAILING_ATR = True
    TRAILING_ATR_MULTIPLIER = 1.5
    MACD_FILTER_ENABLED = False
    CAPITAL_RISK_PERCENT = 0.95
    HEARTBEAT_HOURS = 1
    MIN_USDC_BALANCE = 12.0
    BB_MARGIN_PERCENT = 2.5
    TAKE_PROFIT_PERCENT = 3.0
    TAKE_PROFIT_SIZE = 100
    MAX_POSITIONS = 3
    CAPITAL_SPLIT = [0.50, 0.30, 0.20]
    ANTI_CORRELATION = True
    ORDER_TIMEOUT_SECONDS = 300
    USE_MARKET_ORDERS = False
    SYNC_BALANCE_MINUTES = 15
    RANGE_VR_THRESHOLD = 1.0
    COOLDOWN_ADAPTIVE = True

    # Paramètres fixes
    BB_PERIOD = 20
    BB_STD = 2.0
    RSI_PERIOD = 14
    BAND_WALK_LOOKBACK = 4
    MACD_FAST = 12
    MACD_SLOW = 26
    MACD_SIGNAL = 9
    LOOP_SLEEP_SECONDS = 45
    TELEGRAM_CHECK_SECONDS = 8
    DB_FILE = "trading.db"
    DRY_RUN = True
    USE_WEBSOCKET = True

    DEFAULTS = {
        "RSI_THRESHOLDS": {"BTCUSDC": 45, "ETHUSDC": 44, "SOLUSDC": 40, "BNBUSDC": 43},
        "SIGNAL_COOLDOWN_MINUTES": 30, "STOP_LOSS_ATR_MULTIPLIER": 1.8,
        "TRAILING_STOP_ACTIVATION": 0.015, "TRAILING_STOP_DISTANCE": 0.012,
        "MACD_FILTER_ENABLED": False, "CAPITAL_RISK_PERCENT": 0.95,
        "HEARTBEAT_HOURS": 1, "BB_MARGIN_PERCENT": 2.5,
        "TAKE_PROFIT_PERCENT": 3.0, "TAKE_PROFIT_SIZE": 50,
        "MAX_POSITIONS": 3, "RANGE_VR_THRESHOLD": 1.0,
        "COOLDOWN_ADAPTIVE": True, "USE_TRAILING_ATR": True,
        "USE_WEBSOCKET": True,
    }

    @classmethod
    def validate(cls):
        if not all([cls.BINANCE_API_KEY, cls.BINANCE_SECRET_KEY, cls.TELEGRAM_TOKEN, cls.TELEGRAM_CHAT_ID]):
            raise ValueError("Fichier .env incomplet.")
