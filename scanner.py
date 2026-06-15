import logging
from datetime import datetime, timedelta
from config import Config
from strategy import StrategyEngine

logger = logging.getLogger("DynamicScanner")

class DynamicPairScanner:
    def __init__(self, binance_client):
        self.binance = binance_client
        self.last_scan = None
        self.top_pairs = []

    def scan(self):
        if self.last_scan and (datetime.now() - self.last_scan) < timedelta(minutes=15 if not Config.SCALP_MODE else 2):
            return self.top_pairs

        try:
            candidates = []
            all_pairs = self.binance.get_all_usdc_pairs()
            for symbol in all_pairs[:100]:
                if symbol in Config.CORE_SYMBOLS:
                    continue
                try:
                    volume = self.binance.get_24h_volume(symbol)
                    if volume < 5_000_000:
                        continue
                    df = self.binance.get_klines(symbol, limit=60)
                    if df.empty:
                        continue
                    score = StrategyEngine.compute_score(df, symbol)
                    if score > 0:
                        candidates.append({
                            'symbol': symbol,
                            'score': score,
                            'price': float(df['close'].iloc[-1])
                        })
                except:
                    pass

            candidates.sort(key=lambda x: x['score'], reverse=True)
            self.top_pairs = candidates[:3]
            self.last_scan = datetime.now()
        except Exception as e:
            logger.error(f"Erreur scan dynamique: {e}")

        return self.top_pairs

    def get_top_symbols(self):
        return [p['symbol'] for p in self.scan()]