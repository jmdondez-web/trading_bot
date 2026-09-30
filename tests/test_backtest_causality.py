import unittest
import numpy as np
import pandas as pd
from config import Config
from strategy import StrategyEngine
from backtest_v2 import precompute, eligibility


def synthetic_df(n=300, seed=42):
    rng = np.random.default_rng(seed)
    close = 100 + np.cumsum(rng.normal(0, 0.3, n))
    high = close + rng.uniform(0.05, 0.3, n)
    low = close - rng.uniform(0.05, 0.3, n)
    open_ = close + rng.normal(0, 0.05, n)
    return pd.DataFrame({
        'timestamp': pd.date_range('2026-01-01', periods=n, freq='5min'),
        'open': open_, 'high': high, 'low': low, 'close': close,
        'volume': rng.uniform(900, 1100, n),
    })


class TestBacktestCausality(unittest.TestCase):
    """Un backtest honnete doit etre CAUSAL : la valeur d'un indicateur a
    l'instant t ne doit jamais dependre des donnees posterieures a t.
    On verifie que le precompute sur un prefixe est identique au prefixe du
    precompute sur la serie complete (condition suffisante de causalite)."""

    PREFIX = 150

    def _assert_same_prefix(self, ind_prefix, ind_full, field):
        a = np.asarray(ind_prefix[field][:self.PREFIX], dtype=float)
        b = np.asarray(ind_full[field][:self.PREFIX], dtype=float)
        mask = ~(np.isnan(a) & np.isnan(b))
        self.assertTrue(np.allclose(a[mask], b[mask], equal_nan=True),
                        f'{field} n est pas causal')

    def test_precompute_is_causal(self):
        df = synthetic_df()
        ind_prefix = precompute(df.iloc[:self.PREFIX].reset_index(drop=True))
        ind_full = precompute(df)
        for field in ('close', 'rsi', 'atr', 'dist_lower', 'price_at_lower',
                      'ctx_range', 'rising', 'valid'):
            self._assert_same_prefix(ind_prefix, ind_full, field)

    def test_eligibility_is_causal(self):
        df = synthetic_df()
        ind_prefix = precompute(df.iloc[:self.PREFIX].reset_index(drop=True))
        ind_full = precompute(df)
        e_prefix = eligibility(ind_prefix, 'BTCUSDC', 1.5, {'BTCUSDC': 35}, False)
        e_full = eligibility(ind_full, 'BTCUSDC', 1.5, {'BTCUSDC': 35}, False)
        self.assertEqual(e_prefix, e_full[:self.PREFIX])

    def test_future_change_does_not_alter_past_signals(self):
        # muter violemment le futur ne doit rien changer aux barres passees
        df = synthetic_df()
        e_before = eligibility(precompute(df), 'BTCUSDC', 1.5, {'BTCUSDC': 35}, False)
        df_mut = df.copy()
        df_mut.loc[self.PREFIX:, ['open', 'high', 'low', 'close']] *= 1.5
        e_after = eligibility(precompute(df_mut), 'BTCUSDC', 1.5, {'BTCUSDC': 35}, False)
        self.assertEqual(e_before[:self.PREFIX], e_after[:self.PREFIX])

    def test_rsi_bounds(self):
        df = synthetic_df(200)
        rsi = StrategyEngine.calculate_rsi(df['close'], 14)
        self.assertTrue(((rsi.dropna() >= 0) & (rsi.dropna() <= 100)).all())


if __name__ == '__main__':
    unittest.main()
