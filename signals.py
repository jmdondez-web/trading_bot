import logging
from datetime import datetime, timedelta
from config import Config
from strategy import StrategyEngine

logger = logging.getLogger("SignalManager")

# Groupes de paires corrélées — ouvrir 2 paires du même groupe
# double l'exposition sur un même mouvement de marché
CORRELATION_GROUPS = [
    {'BTCUSDC', 'ETHUSDC'},          # corrélation ~0.85 en tendance
    {'SOLUSDC', 'BNBUSDC'},          # corrélation modérée ~0.6
]

class SignalManager:
    def __init__(self, db, binance_client, order_manager, risk_manager, telegram):
        self.db = db
        self.binance = binance_client
        self.orders = order_manager
        self.risk = risk_manager
        self.telegram = telegram

    def _get_cooldown(self, symbol, analysis_result):
        """Calcule le cooldown à partir d'un résultat analyze() déjà calculé.

        FIX (30/09/2026): ancienne version rappelait StrategyEngine.analyze() ici
        alors que scan_all_pairs() l'appelle aussi juste après -> double calcul par
        paire. On passe maintenant le résultat déjà disponible.
        """
        if not Config.COOLDOWN_ADAPTIVE:
            return Config.SIGNAL_COOLDOWN_MINUTES
        state = analysis_result.get('context_state', 'NEUTRE')
        if state == 'RANGE_CALME':
            return max(15, Config.SIGNAL_COOLDOWN_MINUTES // 2)
        elif state in ('CHAOS', 'TENDANCE_FORTE'):
            return Config.SIGNAL_COOLDOWN_MINUTES * 2
        return Config.SIGNAL_COOLDOWN_MINUTES

    def _check_anti_correlation(self, symbol, open_symbols):
        """Vérifie si le symbol est corrélé à une position déjà ouverte.

        FIX: Config.ANTI_CORRELATION était déclaré mais jamais utilisé.
        Ouvrir BTC + ETH simultanément double l'exposition sur un même
        mouvement de marché (corrélation ~0.85).
        """
        if not Config.ANTI_CORRELATION:
            return True  # filtre désactivé -> signal autorisé

        for group in CORRELATION_GROUPS:
            if symbol in group:
                conflicting = [s for s in open_symbols if s in group]
                if conflicting:
                    logger.info(
                        f"ANTI_CORRELATION: {symbol} bloqué — "
                        f"{conflicting[0]} déjà ouvert dans le même groupe"
                    )
                    self.db.add_rejected_order(
                        symbol,
                        f"ANTI_CORRELATION avec {conflicting[0]}"
                    )
                    return False
        return True

    def scan_all_pairs(self):
        eligible = list(Config.CORE_SYMBOLS)
        pos_symbols = [p['symbol'] for p in self.db.get_positions()]
        signals = []

        for symbol in eligible:
            if symbol in pos_symbols:
                continue

            last = self.db.get_last_signal(symbol)
            df = self.binance.get_klines(symbol)
            if df.empty:
                continue

            # FIX: analyze() appelé UNE SEULE FOIS par paire.
            # Ancienne version : _get_cooldown() rappelait analyze() puis
            # scan_all_pairs() le rappelait -> 2 calculs complets par paire.
            result = StrategyEngine.analyze(df, symbol)
            cooldown = self._get_cooldown(symbol, result)

            if last and (datetime.now() - last) < timedelta(minutes=cooldown):
                continue

            result['cooldown'] = cooldown

            if result['decision'] == "ALLOW":
                # FIX: score calculé inline sans repasser par compute_score()
                # qui appelle analyze() une 2e fois en interne.
                threshold = Config.RSI_THRESHOLDS.get(symbol, 38)
                rsi_score = max(0, (threshold - result['rsi'])) / 10
                bb_score = 2 if result['bb_position'] == "BELOW_LOWER" \
                    else (1 if Config.BB_MARGIN_PERCENT > 0 else 0)
                result['score'] = round(rsi_score + bb_score, 2)
                signals.append(result)

        signals.sort(key=lambda x: x.get('score', 0), reverse=True)
        return signals

    def execute_signal(self, signal):
        symbol = signal['symbol']

        if self.db.count_positions() >= Config.MAX_POSITIONS:
            return False

        # vérification anti-corrélation avant le calcul du capital
        open_symbols = [p['symbol'] for p in self.db.get_positions()]
        if not self._check_anti_correlation(symbol, open_symbols):
            return False

        balance = self.binance.get_account_balance("USDC")
        if balance < Config.MIN_USDC_BALANCE:
            return False

        pos_idx = self.db.count_positions()
        split = Config.CAPITAL_SPLIT[min(pos_idx, len(Config.CAPITAL_SPLIT)-1)]
        capital = balance * Config.CAPITAL_RISK_PERCENT * split

        # FIX: log explicite si capital sous le minimum
        # (cas fréquent avec petit capital + CAPITAL_SPLIT)
        if capital < Config.MIN_USDC_BALANCE:
            logger.info(
                f"Capital insuffisant pour {symbol}: {capital:.2f} USDC "
                f"< MIN {Config.MIN_USDC_BALANCE} USDC "
                f"(balance={balance:.2f}, split={split})"
            )
            return False

        qty = self.orders.calculate_order_quantity(symbol, capital, signal['price'])
        if not qty:
            return False

        stop_price = signal['price'] - (signal['atr'] * Config.STOP_LOSS_ATR_MULTIPLIER)

        if Config.USE_MARKET_ORDERS:
            order = self.orders.place_market_buy_order(symbol, qty)
        else:
            order = self.orders.place_limit_buy_order(symbol, qty, signal['price'])

        if order:
            entry_price = signal['price'] if Config.USE_MARKET_ORDERS \
                else float(order['price'])
            msg = (
                f"NOUVEL ORDRE\n{symbol}\n"
                f"Qte: {qty}\nPrix: {entry_price:.2f}\nStop: {stop_price:.2f}"
            )
            if Config.DRY_RUN:
                msg = "DRY RUN - " + msg
            self.telegram.send(msg)
            self.db.add_position({
                'symbol': symbol, 'entry_price': entry_price, 'quantity': qty,
                'initial_stop': stop_price, 'current_stop': stop_price,
                'order_id': order['orderId'], 'status': 'PENDING',
                'entry_time': datetime.now().isoformat(), 'tp_triggered': False
            })
            self.db.set_last_signal(symbol)
            # Invalide le cache prix du RiskManager pour ce symbol
            if hasattr(self.risk, 'invalidate_price_cache'):
                self.risk.invalidate_price_cache(symbol)
            return True
        return False
