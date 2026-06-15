import logging
from config import Config

logger = logging.getLogger("DustManager")

class DustManager:
    def __init__(self, db, binance_client, order_manager, telegram):
        self.db = db
        self.binance = binance_client
        self.orders = order_manager
        self.telegram = telegram

    def check_and_clean(self):
        if not Config.DUST_AUTO_SELL:
            return

        balances = self.binance.get_all_balances()
        for asset, amount in balances.items():
            if asset == 'USDC':
                continue
            symbol = asset + 'USDC'
            price = self.binance.get_current_price(symbol) or 0
            value = amount * price

            if 0 < value < Config.DUST_THRESHOLD:
                logger.info(f"Poussière détectée: {asset} ({value:.2f} USDC)")
                qty = self.orders.adjust_quantity(symbol, amount)
                if qty > 0:
                    order = self.orders.place_market_sell_order(symbol, qty)
                    if order:
                        self.telegram.send(f"Poussière vendue: {asset} ({value:.2f} USDC)")
                        self.db.add_trade({
                            'symbol': symbol, 'entry_price': price, 'exit_price': price,
                            'pnl_pct': 0, 'pnl_usdc': 0, 'exit_reason': 'DUST_CLEAN',
                            'timestamp': __import__('datetime').datetime.now().isoformat()
                        })

    def sell_all_dust(self):
        balances = self.binance.get_all_balances()
        sold = []
        for asset, amount in balances.items():
            if asset == 'USDC':
                continue
            symbol = asset + 'USDC'
            qty = self.orders.adjust_quantity(symbol, amount)
            if qty > 0:
                order = self.orders.place_market_sell_order(symbol, qty)
                if order:
                    sold.append(f"{asset}: {amount:.6f}")
                    self.db.add_trade({
                        'symbol': symbol, 'entry_price': 0, 'exit_price': 0,
                        'pnl_pct': 0, 'pnl_usdc': 0, 'exit_reason': 'DUST_MANUAL',
                        'timestamp': __import__('datetime').datetime.now().isoformat()
                    })
        return sold

    def sell_position(self, symbol, percentage=100):
        pos = self.db.get_position(symbol)
        if not pos:
            return False, "Position non trouvée"
        qty = float(pos['quantity']) * (percentage / 100)
        qty = self.orders.adjust_quantity(symbol, qty)
        if qty <= 0:
            return False, "Quantité trop petite"
        order = self.orders.place_market_sell_order(symbol, qty)
        if order:
            cp = self.binance.get_current_price(symbol)
            entry = float(pos['entry_price'])
            pnl = (cp - entry) / entry * 100 if cp else 0
            if percentage >= 100:
                self.db.remove_position(symbol)
            else:
                remaining = float(pos['quantity']) - qty
                self.db.update_position(symbol, {'quantity': remaining})
            self.db.add_trade({
                'symbol': symbol, 'entry_price': entry, 'exit_price': cp or 0,
                'pnl_pct': pnl, 'pnl_usdc': (cp - entry) * qty if cp else 0,
                'exit_reason': 'MANUAL_SELL', 'timestamp': __import__('datetime').datetime.now().isoformat()
            })
            return True, f"{symbol} vendu {qty:.6f} @ {cp:.4f} | P&L: {pnl:.2f}%"
        return False, "Erreur lors de la vente"

    def sell_all_positions(self):
        results = []
        for pos in self.db.get_positions():
            success, msg = self.sell_position(pos['symbol'], 100)
            results.append(msg)
        return results