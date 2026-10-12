import sqlite3
from pathlib import Path


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute("""CREATE TABLE IF NOT EXISTS cycles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            chain TEXT NOT NULL,
            provider TEXT NOT NULL,
            amount_in INTEGER NOT NULL,
            amount_mid INTEGER NOT NULL,
            amount_out INTEGER NOT NULL,
            estimated_cost INTEGER NOT NULL,
            pnl INTEGER NOT NULL,
            threshold INTEGER NOT NULL,
            status TEXT NOT NULL,
            mode TEXT NOT NULL DEFAULT 'paper',
            quote_pnl INTEGER NOT NULL DEFAULT 0,
            quote_token TEXT NOT NULL DEFAULT '',
            target_token TEXT NOT NULL DEFAULT ''
        )""")
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(cycles)")}
        migrations = {
            "mode": "TEXT NOT NULL DEFAULT 'paper'",
            "quote_pnl": "INTEGER NOT NULL DEFAULT 0",
            "quote_token": "TEXT NOT NULL DEFAULT ''",
            "target_token": "TEXT NOT NULL DEFAULT ''",
        }
        for column, definition in migrations.items():
            if column not in columns:
                self.db.execute(f"ALTER TABLE cycles ADD COLUMN {column} {definition}")
        if "quote_pnl" not in columns:
            self.db.execute("UPDATE cycles SET quote_pnl=pnl WHERE mode='paper'")
        self.db.commit()

    def record(self, values: tuple, mode: str = "paper", *, quote_pnl: int = 0,
               quote_token: str = "", target_token: str = "") -> tuple[int, int]:
        self.db.execute(
            "INSERT INTO cycles(chain,provider,amount_in,amount_mid,amount_out,estimated_cost,pnl,threshold,status,mode,quote_pnl,quote_token,target_token) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", (*values, mode, quote_pnl, quote_token, target_token),
        )
        self.db.commit()
        if mode == "live":
            row = self.db.execute(
                "SELECT last_insert_rowid(), COALESCE(SUM(pnl),0) FROM cycles "
                "WHERE mode='live' AND status='live_executed'"
            ).fetchone()
        else:
            row = self.db.execute(
                "SELECT last_insert_rowid(), COALESCE(SUM(pnl),0) FROM cycles WHERE mode='paper'"
            ).fetchone()
        return int(row[0]), int(row[1])

    def live_pnl_today(self) -> int:
        row = self.db.execute(
            "SELECT COALESCE(SUM(pnl),0) FROM cycles "
            "WHERE mode='live' AND status IN ('live_executed','holding_after_buy') AND date(created_at)=date('now')"
        ).fetchone()
        return int(row[0])

    def live_trades_last_hour(self) -> int:
        row = self.db.execute(
            "SELECT COUNT(*) FROM cycles WHERE mode='live' AND status IN ('live_executed','holding_after_buy') "
            "AND created_at >= datetime('now','-1 hour')"
        ).fetchone()
        return int(row[0])

    def recent_quote_pnls(self, chain: str, quote_token: str, target_token: str, limit: int) -> list[int]:
        rows = self.db.execute(
            "SELECT quote_pnl FROM cycles WHERE chain=? AND quote_token=? AND target_token=? "
            "ORDER BY id DESC LIMIT ?",
            (chain, quote_token, target_token, limit),
        ).fetchall()
        return [int(row[0]) for row in rows]

    def realized_live_pnls(self, chain: str, quote_token: str, target_token: str) -> list[int]:
        rows = self.db.execute(
            "SELECT pnl FROM cycles WHERE chain=? AND quote_token=? AND target_token=? "
            "AND mode='live' AND status='live_executed' ORDER BY id",
            (chain, quote_token, target_token),
        ).fetchall()
        return [int(row[0]) for row in rows]

    def close(self) -> None:
        self.db.close()
