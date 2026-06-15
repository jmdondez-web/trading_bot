import unittest
from unittest.mock import Mock, patch
from risk import RiskManager

class TestRiskManager(unittest.TestCase):
    def setUp(self):
        self.db = Mock()
        self.binance = Mock()
        self.orders = Mock()
        self.telegram = Mock()
        self.risk = RiskManager(self.db, self.binance, self.orders, self.telegram)

    def test_check_entry_orders_pending_to_filled(self):
        self.db.get_positions.return_value = [{'symbol': 'BTCUSDC', 'order_id': 123, 'status': 'PENDING'}]
        self.orders.get_order_status.return_value = 'FILLED'
        self.risk.check_entry_orders()
        self.telegram.send.assert_called()
        self.db.update_position.assert_called()

    def test_check_entry_orders_cancelled(self):
        self.db.get_positions.return_value = [{'symbol': 'BTCUSDC', 'order_id': 123, 'status': 'PENDING'}]
        self.orders.get_order_status.return_value = 'CANCELED'
        self.risk.check_entry_orders()
        self.db.remove_position.assert_called()

if __name__ == '__main__':
    unittest.main()