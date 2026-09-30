import unittest
from unittest.mock import Mock
from config import Config
from risk import RiskManager


def make_pos(status='ACTIVE'):
    return {
        'symbol': 'BTCUSDC', 'entry_price': 100.0, 'quantity': 1.0,
        'initial_stop': 98.0, 'current_stop': 98.0, 'order_id': 123,
        'status': status, 'entry_time': datetime.now().isoformat(),
        'tp_triggered': False,
    }


class TestRiskManagerPending(unittest.TestCase):
    """Regression tests du fix du 30/09/2026 :
    TP/SL ne doivent JAMAIS toucher une position PENDING (ordre non execute)."""

    def setUp(self):
        self.db = Mock()
        self.binance = Mock()
        self.orders = Mock()
        self.telegram = Mock()
        self.risk = RiskManager(self.db, self.binance, self.orders, self.telegram)
        self.orders.adjust_quantity.return_value = 0.5
        self.orders.place_market_sell_order.return_value = {'orderId': 1}
        Config.TAKE_PROFIT_PERCENT = 3.0
        Config.SCALP_MODE = False

    def test_take_profit_ignores_pending(self):
        self.db.get_positions.return_value = [make_pos('PENDING')]
        self.binance.get_current_price.return_value = 104.0  # +4% >= TP
        self.risk.check_take_profit()
        self.orders.place_market_sell_order.assert_not_called()
        self.db.update_position.assert_not_called()

    def test_take_profit_sells_active(self):
        self.db.get_positions.return_value = [make_pos('ACTIVE')]
        self.binance.get_current_price.return_value = 104.0
        self.risk.check_take_profit()
        self.orders.place_market_sell_order.assert_called_once()
        self.db.add_trade.assert_called_once()

    def test_stop_loss_ignores_pending(self):
        self.db.get_positions.return_value = [make_pos('PENDING')]
        self.binance.get_current_price.return_value = 97.0  # sous le stop
        self.risk.check_stop_loss()
        self.orders.place_market_sell_order.assert_not_called()

    def test_stop_loss_sells_active(self):
        self.db.get_positions.return_value = [make_pos('ACTIVE')]
        self.binance.get_current_price.return_value = 97.0
        self.risk.check_stop_loss()
        self.orders.place_market_sell_order.assert_called_once()
        self.db.remove_position.assert_called_once_with('BTCUSDC')

    def test_no_sell_when_price_unavailable(self):
        self.db.get_positions.return_value = [make_pos('ACTIVE')]
        self.binance.get_current_price.return_value = None
        self.risk.check_stop_loss()
        self.orders.place_market_sell_order.assert_not_called()


from datetime import datetime  # noqa: E402  (utilise par make_pos)


if __name__ == '__main__':
    unittest.main()
