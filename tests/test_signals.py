import unittest
from datetime import datetime, timedelta
import pandas as pd
from unittest.mock import Mock
from config import Config
from signals import SignalManager


class TestSignalManager(unittest.TestCase):
    def setUp(self):
        self.db = Mock()
        self.binance = Mock()
        self.orders = Mock()
        self.risk = Mock()
        self.telegram = Mock()
        self.sm = SignalManager(self.db, self.binance, self.orders, self.risk, self.telegram)
        self.scanner = Mock()
        self.scanner.scan.return_value = []
        Config.AUTO_SCAN = False
        self.db.get_positions.return_value = []
        self.db.get_last_signal.return_value = None
        self.db.count_positions.return_value = 0

    def tearDown(self):
        # restaure la config par defaut pour ne pas fuir entre les tests
        Config.ANTI_CORRELATION = True
        Config.COOLDOWN_ADAPTIVE = True
        Config.SIGNAL_COOLDOWN_MINUTES = 30
        Config.SCALP_MODE = False
        Config.TURBO_MODE = False
        Config.MAX_POSITIONS = 3
        Config.AUTO_SCAN = True

    def test_anti_correlation_blocks_same_group(self):
        # BTC ouvert -> ETH (meme groupe) bloque
        blocked = self.sm._check_anti_correlation('ETHUSDC', ['BTCUSDC'])
        self.assertFalse(blocked)
        self.db.add_rejected_order.assert_called_once()

    def test_anti_correlation_allows_uncorrelated(self):
        ok = self.sm._check_anti_correlation('SOLUSDC', ['BTCUSDC'])
        self.assertTrue(ok)
        self.db.add_rejected_order.assert_not_called()

    def test_anti_correlation_disabled(self):
        Config.ANTI_CORRELATION = False
        ok = self.sm._check_anti_correlation('ETHUSDC', ['BTCUSDC'])
        self.assertTrue(ok)

    def test_cooldown_adaptive(self):
        # RANGE_CALME -> moitie du cooldown (min 15)
        cd = self.sm._get_cooldown('BTCUSDC', {'context_state': 'RANGE_CALME'})
        self.assertEqual(cd, 15)
        # CHAOS -> double
        cd = self.sm._get_cooldown('BTCUSDC', {'context_state': 'CHAOS'})
        self.assertEqual(cd, 60)
        # NEUTRE -> valeur de base
        cd = self.sm._get_cooldown('BTCUSDC', {'context_state': 'NEUTRE'})
        self.assertEqual(cd, 30)

    def test_cooldown_non_adaptive(self):
        Config.COOLDOWN_ADAPTIVE = False
        cd = self.sm._get_cooldown('BTCUSDC', {'context_state': 'CHAOS'})
        self.assertEqual(cd, 30)

    def test_execute_signal_max_positions(self):
        self.db.count_positions.return_value = 3
        Config.MAX_POSITIONS = 3
        Config.AUTO_SCAN = True
        self.assertFalse(self.sm.execute_signal({'symbol': 'BTCUSDC', 'price': 100.0, 'atr': 1.0}))

    def test_scan_respects_open_positions(self):
        # une paire deja en position ne doit pas etre re-scannee
        self.db.get_positions.return_value = [{'symbol': 'BTCUSDC'}]
        self.binance.get_klines.return_value = pd.DataFrame()  # jamais appele si skip
        signals = self.sm.scan_all_pairs(self.scanner)
        self.assertEqual(signals, [])  # df vide -> aucun signal
        scanned = [c.args[0] for c in self.binance.get_klines.call_args_list]
        self.assertNotIn('BTCUSDC', scanned)  # la paire ouverte n est jamais re-scannee
        self.assertIn('ETHUSDC', scanned)     # les autres paires le sont


if __name__ == '__main__':
    unittest.main()
