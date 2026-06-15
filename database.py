import sqlite3
import threading
from datetime import datetime
from config import Config

class Database:
    def __init__(self):
        self.lock = threading.Lock()
        with self.lock:
            with sqlite3.connect(Config.DB_FILE) as conn:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("""CREATE TABLE IF NOT EXISTS positions (
                    symbol TEXT PRIMARY KEY, entry_price REAL, quantity REAL,
                    initial_stop REAL, current_stop REAL, order_id INTEGER,
                    status TEXT, entry_time TEXT, tp_triggered INTEGER DEFAULT 0)""")
                conn.execute("""CREATE TABLE IF NOT EXISTS trades_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT,
                    entry_price REAL, exit_price REAL, pnl_pct REAL, pnl_usdc REAL,
                    exit_reason TEXT, timestamp TEXT)""")
                conn.execute("""CREATE TABLE IF NOT EXISTS config (
                    key TEXT PRIMARY KEY, value TEXT)""")
                conn.execute("""CREATE TABLE IF NOT EXISTS last_signal (
                    symbol TEXT PRIMARY KEY, timestamp TEXT)""")
                conn.execute("""CREATE TABLE IF NOT EXISTS committee_votes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT,
                    timestamp TEXT, vote1 TEXT, vote2 TEXT, vote3 TEXT, result TEXT)""")
                conn.execute("""CREATE TABLE IF NOT EXISTS rejected_orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT,
                    reason TEXT, timestamp TEXT)""")
                conn.execute("""CREATE INDEX IF NOT EXISTS idx_trades_timestamp
                    ON trades_history(timestamp)""")
                conn.commit()

    def get_positions(self):
        with self.lock:
            with sqlite3.connect(Config.DB_FILE) as conn:
                rows = conn.execute("SELECT * FROM positions").fetchall()
                return [dict(zip(['symbol','entry_price','quantity','initial_stop','current_stop','order_id','status','entry_time','tp_triggered'], row)) for row in rows]

    def add_position(self, pos):
        with self.lock:
            with sqlite3.connect(Config.DB_FILE) as conn:
                conn.execute("INSERT OR REPLACE INTO positions VALUES (?,?,?,?,?,?,?,?,?)",
                    (pos['symbol'], float(pos['entry_price']), float(pos['quantity']),
                     float(pos['initial_stop']), float(pos['current_stop']),
                     int(pos.get('order_id',0)), pos.get('status','ACTIVE'),
                     str(pos.get('entry_time', datetime.now().isoformat())),
                     int(pos.get('tp_triggered', False))))
                conn.commit()

    def update_position(self, symbol, updates):
        pos = self.get_position(symbol)
        if pos:
            pos.update(updates)
            self.add_position(pos)

    def remove_position(self, symbol):
        with self.lock:
            with sqlite3.connect(Config.DB_FILE) as conn:
                conn.execute("DELETE FROM positions WHERE symbol=?", (symbol,))
                conn.commit()

    def get_position(self, symbol):
        with self.lock:
            with sqlite3.connect(Config.DB_FILE) as conn:
                row = conn.execute("SELECT * FROM positions WHERE symbol=?", (symbol,)).fetchone()
                return dict(zip(['symbol','entry_price','quantity','initial_stop','current_stop','order_id','status','entry_time','tp_triggered'], row)) if row else None

    def count_positions(self):
        return len(self.get_positions())

    def add_trade(self, trade):
        with self.lock:
            with sqlite3.connect(Config.DB_FILE) as conn:
                conn.execute("INSERT INTO trades_history (symbol,entry_price,exit_price,pnl_pct,pnl_usdc,exit_reason,timestamp) VALUES (?,?,?,?,?,?,?)",
                    (trade['symbol'], float(trade['entry_price']), float(trade['exit_price']),
                     float(trade['pnl_pct']), float(trade.get('pnl_usdc',0)),
                     trade['exit_reason'], trade.get('timestamp', datetime.now().isoformat())))
                conn.commit()

    def get_trades(self, limit=50):
        with self.lock:
            with sqlite3.connect(Config.DB_FILE) as conn:
                rows = conn.execute("SELECT * FROM trades_history ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
                return [dict(zip(['id','symbol','entry_price','exit_price','pnl_pct','pnl_usdc','exit_reason','timestamp'], row)) for row in rows]

    def get_cumulative_pnl(self):
        with self.lock:
            with sqlite3.connect(Config.DB_FILE) as conn:
                row = conn.execute("SELECT SUM(pnl_usdc) FROM trades_history").fetchone()
                return row[0] if row[0] else 0.0

    def get_config(self, key):
        with self.lock:
            with sqlite3.connect(Config.DB_FILE) as conn:
                row = conn.execute("SELECT value FROM config WHERE key=?", (key,)).fetchone()
                return row[0] if row else None

    def set_config(self, key, value):
        with self.lock:
            with sqlite3.connect(Config.DB_FILE) as conn:
                conn.execute("INSERT OR REPLACE INTO config VALUES (?,?)", (key, str(value)))
                conn.commit()

    def get_all_config(self):
        with self.lock:
            with sqlite3.connect(Config.DB_FILE) as conn:
                return {row[0]: row[1] for row in conn.execute("SELECT * FROM config").fetchall()}

    def get_last_signal(self, symbol):
        with self.lock:
            with sqlite3.connect(Config.DB_FILE) as conn:
                row = conn.execute("SELECT timestamp FROM last_signal WHERE symbol=?", (symbol,)).fetchone()
                if row:
                    try: return datetime.fromisoformat(row[0])
                    except: return None
                return None

    def set_last_signal(self, symbol):
        with self.lock:
            with sqlite3.connect(Config.DB_FILE) as conn:
                conn.execute("INSERT OR REPLACE INTO last_signal VALUES (?,?)", (symbol, datetime.now().isoformat()))
                conn.commit()

    def add_committee_vote(self, symbol, votes, result):
        with self.lock:
            with sqlite3.connect(Config.DB_FILE) as conn:
                conn.execute("INSERT INTO committee_votes (symbol,timestamp,vote1,vote2,vote3,result) VALUES (?,?,?,?,?,?)",
                    (symbol, datetime.now().isoformat(), str(votes[0]), str(votes[1]), str(votes[2]), result))
                conn.commit()

    def get_committee_stats(self):
        with self.lock:
            with sqlite3.connect(Config.DB_FILE) as conn:
                total = conn.execute("SELECT COUNT(*) FROM committee_votes").fetchone()[0]
                if total == 0: return {"total": 0, "accept_rate": 0}
                accepted = conn.execute("SELECT COUNT(*) FROM committee_votes WHERE result='ACCEPTED'").fetchone()[0]
                return {"total": total, "accept_rate": round(accepted / total * 100, 1)}

    def add_rejected_order(self, symbol, reason):
        with self.lock:
            with sqlite3.connect(Config.DB_FILE) as conn:
                conn.execute("INSERT INTO rejected_orders (symbol,reason,timestamp) VALUES (?,?,?)",
                    (symbol, reason, datetime.now().isoformat()))
                conn.commit()

    def get_rejected_orders(self, limit=10):
        with self.lock:
            with sqlite3.connect(Config.DB_FILE) as conn:
                rows = conn.execute("SELECT * FROM rejected_orders ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
                return [dict(zip(['id','symbol','reason','timestamp'], row)) for row in rows]

    def clear_rejected_orders(self):
        with self.lock:
            with sqlite3.connect(Config.DB_FILE) as conn:
                conn.execute("DELETE FROM rejected_orders")
                conn.commit()

    def reset_trades(self, confirm=False):
        """Efface tout l'historique des trades.

        FIX: requiert confirm=True pour éviter un effacement accidentel
        via une commande Telegram mal interprétée.
        """
        if not confirm:
            raise ValueError(
                "reset_trades() requiert confirm=True — "
                "appeler reset_trades(confirm=True) pour confirmer l'effacement."
            )
        with self.lock:
            with sqlite3.connect(Config.DB_FILE) as conn:
                conn.execute("DELETE FROM trades_history")
                conn.commit()