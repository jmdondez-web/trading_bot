import unittest
from unittest.mock import Mock, patch
from orders import OrderManager

class TestOrderManager(unittest.TestCase):
    def setUp(self):
        self.client = Mock()
        self.db = Mock()
        self.order_manager = OrderManager(self.client, self.db)

    def test_adjust_quantity(self):
        self.order_manager.exchange_info = {'BTCUSDC': {'lot_size': 0.00001, 'min_qty': 0.0001, 'min_notional': 10.0}}
        result = self.order_manager.adjust_quantity('BTCUSDC', 0.00012345)
        self.assertEqual(result, 0.00012)

    def test_calculate_order_quantity_too_small(self):
        self.order_manager.exchange_info = {'BTCUSDC': {'lot_size': 0.00001, 'min_qty': 0.001, 'min_notional': 10.0}}
        result = self.order_manager.calculate_order_quantity('BTCUSDC', 5.0, 50000)
        self.assertIsNone(result)

if __name__ == '__main__':
    unittest.main()