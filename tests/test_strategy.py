import unittest
import pandas as pd
import numpy as np
from strategy import StrategyEngine

class TestStrategyEngine(unittest.TestCase):
    def setUp(self):
        dates = pd.date_range('2026-01-01', periods=100, freq='5min')
        self.df = pd.DataFrame({
            'open': np.random.normal(100, 5, 100),
            'high': np.random.normal(102, 5, 100),
            'low': np.random.normal(98, 5, 100),
            'close': np.random.normal(100, 5, 100),
            'volume': np.random.normal(1000, 100, 100)
        }, index=dates)

    def test_calculate_rsi(self):
        rsi = StrategyEngine.calculate_rsi(self.df['close'], 14)
        self.assertEqual(len(rsi), 100)
        self.assertTrue(0 <= rsi.iloc[-1] <= 100)

    def test_calculate_bollinger(self):
        bb = StrategyEngine.calculate_bollinger(self.df['close'])
        self.assertIsNotNone(bb.sma)
        self.assertIsNotNone(bb.upper)
        self.assertIsNotNone(bb.lower)

    def test_analyze_returns_dict(self):
        result = StrategyEngine.analyze(self.df, 'BTCUSDC')
        self.assertIn('decision', result)
        self.assertIn('context_state', result)
        self.assertIn('rsi', result)

    def test_compute_score(self):
        score = StrategyEngine.compute_score(self.df, 'BTCUSDC')
        self.assertIsInstance(score, float)

if __name__ == '__main__':
    unittest.main()