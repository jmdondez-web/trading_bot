import numpy as np
import pandas as pd
from dataclasses import dataclass
from config import Config

@dataclass
class BollingerResult:
    sma: pd.Series; upper: pd.Series; lower: pd.Series; width: pd.Series; ref: pd.Series

# Nombre de ticks RSI consécutifs en hausse requis pour valider un signal d'entrée.
# FIX: ancienne valeur = 1 tick (trop peu — un rebond d'un seul tick peut être du bruit).
# 2 ticks consécutifs filtrent mieux les "catching falling knives" sans trop retarder l'entrée.
RSI_RISING_TICKS = 2

class StrategyEngine:
    @staticmethod
    def calculate_rsi(series, period=None):
        if period is None:
            period = Config.SCALP_RSI_PERIOD if Config.SCALP_MODE else Config.RSI_PERIOD
        delta = series.diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
        # FIX: évite ZeroDivisionError si loss==0 (toutes les bougies vertes)
        # pandas retourne inf dans ce cas, ce qui donne RSI=100 — valeur valide
        # mais on s'assure que le résultat est bien borné entre 0 et 100
        rsi = 100 - (100 / (1 + gain / loss.replace(0, float('inf'))))
        return rsi.clip(0, 100)

    @staticmethod
    def calculate_bollinger(series, period=20, std=2):
        sma = series.rolling(window=period).mean()
        std_dev = series.rolling(window=period).std()
        upper = sma + (std_dev * std)
        lower = sma - (std_dev * std)
        width = (upper - lower) / sma
        ref = width.rolling(window=20).mean()
        return BollingerResult(sma=sma, upper=upper, lower=lower, width=width, ref=ref)

    @staticmethod
    def calculate_macd(series, fast=12, slow=26, signal=9):
        ema_fast = series.ewm(span=fast, adjust=False).mean()
        ema_slow = series.ewm(span=slow, adjust=False).mean()
        macd_line = ema_fast - ema_slow
        signal_line = macd_line.ewm(span=signal, adjust=False).mean()
        return macd_line, signal_line, macd_line - signal_line

    @staticmethod
    def calculate_atr(df, period=14):
        high_low = df['high'] - df['low']
        high_close = abs(df['high'] - df['close'].shift())
        low_close = abs(df['low'] - df['close'].shift())
        tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
        return tr.rolling(window=period).mean()

    @classmethod
    def analyze(cls, df, symbol):
        min_len = Config.SCALP_RSI_PERIOD if Config.SCALP_MODE else Config.BB_PERIOD
        # FIX: RSI_RISING_TICKS ticks requis -> besoin d'au moins min_len + RSI_RISING_TICKS bougies
        if df.empty or len(df) < (min_len + RSI_RISING_TICKS):
            return {"context": "INSUFFICIENT_DATA", "decision": "DENY",
                    "rsi": 0, "price": 0, "atr": 0, "symbol": symbol,
                    "context_state": "INSUFFICIENT_DATA", "bb_position": "INSIDE",
                    "reason": "Not enough data", "macd_bullish": False,
                    "timestamp": pd.Timestamp.now().isoformat()}

        close = df['close']
        rsi_series = cls.calculate_rsi(close)
        bb = cls.calculate_bollinger(close)
        macd_line, signal_line, _ = cls.calculate_macd(close)
        atr_series = cls.calculate_atr(df)

        last_price = close.iloc[-1]
        last_rsi = rsi_series.iloc[-1]
        last_atr = atr_series.iloc[-1]

        bb_position = "INSIDE"
        if last_price <= bb.lower.iloc[-1]:
            bb_position = "BELOW_LOWER"
        elif last_price >= bb.upper.iloc[-1]:
            bb_position = "ABOVE_UPPER"

        last_bb_width = bb.width.iloc[-1]
        last_bb_ref = bb.ref.iloc[-1] if not pd.isna(bb.ref.iloc[-1]) else 0.05

        is_chaos = last_bb_width > (last_bb_ref * 1.5)
        is_band_walk = False
        if len(df) > Config.BAND_WALK_LOOKBACK:
            recent_closes = close.iloc[-Config.BAND_WALK_LOOKBACK:]
            recent_lower = bb.lower.iloc[-Config.BAND_WALK_LOOKBACK:]
            is_band_walk = (
                (recent_closes <= recent_lower).all()
                and (recent_lower.iloc[-1] < recent_lower.iloc[0])
            )

        if is_chaos:
            context_state = "CHAOS"
        elif is_band_walk:
            context_state = "TENDANCE_FORTE"
        else:
            vr = last_bb_width / last_bb_ref if last_bb_ref > 0 else 1
            if vr < Config.RANGE_VR_THRESHOLD:
                context_state = "RANGE_CALME"
            elif vr > 1.8:
                context_state = "ILLISIBLE"
            else:
                context_state = "NEUTRE"

        # FIX: log contexte ILLISIBLE — avant, le bot ne tradait pas sans
        # jamais expliquer pourquoi. Maintenant visible dans les logs.
        if context_state == "ILLISIBLE":
            pass  # loggé dans signals.py via le champ reason

        decision, reason = "DENY", ""
        threshold = Config.SCALP_RSI_THRESHOLD if Config.SCALP_MODE \
            else Config.RSI_THRESHOLDS.get(symbol, 38)
        macd_bullish = macd_line.iloc[-1] > signal_line.iloc[-1]

        allowed_contexts = ["RANGE_CALME"] if not Config.SCALP_MODE \
            else ["RANGE_CALME", "NEUTRE"]

        if context_state in allowed_contexts:
            price_near_lower = bb_position == "BELOW_LOWER"

            # FIX: BB_MARGIN_PERCENT — on documente la logique clairement.
            # Si le prix est à moins de BB_MARGIN_PERCENT% AU-DESSUS du Lower Band,
            # on considère quand même que le prix est "près du bas".
            # Risque : avec BB_MARGIN=2.5%, on peut entrer jusqu'à +2.5% du Lower Band
            # ce qui dilue la qualité Mean Reversion. Valeur recommandée : <=1.5%.
            if not price_near_lower and Config.BB_MARGIN_PERCENT > 0 and not Config.SCALP_MODE:
                distance = (last_price - bb.lower.iloc[-1]) / bb.lower.iloc[-1]
                if distance <= Config.BB_MARGIN_PERCENT / 100:
                    price_near_lower = True

            if last_rsi <= threshold and price_near_lower:
                # FIX: RSI_RISING_TICKS ticks consécutifs en hausse requis.
                # Ancienne version : 1 seul tick suffisait — trop sensible au bruit.
                # Nouveau : on vérifie que les derniers RSI_RISING_TICKS sont
                # strictement croissants (momentum haussier confirmé).
                rsi_tail = rsi_series.iloc[-(RSI_RISING_TICKS + 1):]
                rsi_rising = all(
                    rsi_tail.iloc[i] < rsi_tail.iloc[i + 1]
                    for i in range(RSI_RISING_TICKS)
                )
                if rsi_rising:
                    if Config.MACD_FILTER_ENABLED and not macd_bullish:
                        decision, reason = "DENY", "MACD filter: bearish"
                    else:
                        decision, reason = "ALLOW", "Mean Reversion Entry"
                else:
                    reason = f"RSI not rising ({RSI_RISING_TICKS} ticks requis)"
            else:
                reason = f"RSI={last_rsi:.2f} Pos={bb_position}"
        else:
            reason = f"Context={context_state}"

        return {
            "symbol": symbol,
            "timestamp": pd.Timestamp.now().isoformat(),
            "price": last_price,
            "rsi": last_rsi,
            "bb_position": bb_position,
            "context_state": context_state,
            "decision": decision,
            "reason": reason,
            "atr": last_atr,
            "macd_bullish": macd_bullish
        }

    @classmethod
    def compute_score(cls, df, symbol):
        """Conservé pour compatibilité avec backtest.py et scanner.py.
        signals.py calcule désormais le score inline sans rappeler analyze().
        """
        result = cls.analyze(df, symbol)
        if result['decision'] == "DENY":
            return 0
        threshold = Config.SCALP_RSI_THRESHOLD if Config.SCALP_MODE \
            else Config.RSI_THRESHOLDS.get(symbol, 38)
        rsi_score = max(0, (threshold - result['rsi'])) / 10
        bb_score = 2 if result['bb_position'] == "BELOW_LOWER" \
            else (1 if Config.BB_MARGIN_PERCENT > 0 else 0)
        return round(rsi_score + bb_score, 2)
