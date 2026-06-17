"""
数据库模块 - SQLite持久化存储
记录所有交易、决策日志、账户快照
"""
import sqlite3
import json
from datetime import datetime, timezone
from utils.logger import get_logger

logger = get_logger("btc_trader.db")


class Database:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self.conn = None

    def initialize(self):
        """创建表结构"""
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")  # 写入优化

        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                symbol TEXT NOT NULL,
                side TEXT,
                action TEXT,
                price REAL,
                quantity REAL,
                pnl REAL,
                reason TEXT,
                confidence INTEGER DEFAULT 0,
                stop_loss REAL,
                take_profit REAL
            );

            CREATE TABLE IF NOT EXISTS decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                trigger TEXT,
                input_json TEXT,
                output_json TEXT,
                actions_executed TEXT,
                risk_filtered TEXT
            );

            CREATE TABLE IF NOT EXISTS account_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                total_balance REAL,
                unrealized_pnl REAL,
                position_value REAL,
                daily_pnl REAL,
                margin_ratio REAL
            );

            CREATE INDEX IF NOT EXISTS idx_trades_ts ON trades(timestamp);
            CREATE INDEX IF NOT EXISTS idx_decisions_ts ON decisions(timestamp);
            CREATE INDEX IF NOT EXISTS idx_snapshots_ts ON account_snapshots(timestamp);
        """)
        self.conn.commit()
        logger.info(f"数据库初始化完成: {self.db_path}")

    def insert_trade(self, symbol: str, side: str, action: str,
                     price: float, quantity: float,
                     reason: str = "", confidence: int = 0,
                     pnl: float = None, stop_loss: float = None,
                     take_profit: float = None):
        """插入交易记录"""
        ts = datetime.now(timezone.utc).isoformat()
        try:
            self.conn.execute(
                """INSERT INTO trades
                   (timestamp, symbol, side, action, price, quantity,
                    pnl, reason, confidence, stop_loss, take_profit)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (ts, symbol, side, action, price, quantity,
                 pnl, reason, confidence, stop_loss, take_profit)
            )
            self.conn.commit()
        except Exception as e:
            logger.error(f"插入交易记录失败: {e}")

    def insert_decision(self, trigger: str, input_json: str,
                        output_json: str, actions_executed: str,
                        risk_filtered: str):
        """插入决策日志"""
        ts = datetime.now(timezone.utc).isoformat()
        try:
            self.conn.execute(
                """INSERT INTO decisions
                   (timestamp, trigger, input_json, output_json,
                    actions_executed, risk_filtered)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (ts, trigger, input_json, output_json,
                 actions_executed, risk_filtered)
            )
            self.conn.commit()
        except Exception as e:
            logger.error(f"插入决策日志失败: {e}")

    def insert_snapshot(self, total_balance: float, unrealized_pnl: float,
                        position_value: float, daily_pnl: float,
                        margin_ratio: float):
        """插入账户快照"""
        ts = datetime.now(timezone.utc).isoformat()
        try:
            self.conn.execute(
                """INSERT INTO account_snapshots
                   (timestamp, total_balance, unrealized_pnl,
                    position_value, daily_pnl, margin_ratio)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (ts, total_balance, unrealized_pnl,
                 position_value, daily_pnl, margin_ratio)
            )
            self.conn.commit()
        except Exception as e:
            logger.error(f"插入快照失败: {e}")

    def get_recent_trades(self, limit: int = 10) -> list[dict]:
        """获取最近N笔交易"""
        try:
            cursor = self.conn.execute(
                "SELECT * FROM trades ORDER BY id DESC LIMIT ?", (limit,)
            )
            cols = [d[0] for d in cursor.description]
            return [dict(zip(cols, row)) for row in cursor.fetchall()]
        except Exception as e:
            logger.error(f"查询交易记录失败: {e}")
            return []

    def get_daily_stats(self) -> dict:
        """获取今日交易统计"""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        try:
            cursor = self.conn.execute(
                """SELECT
                     COUNT(*) as trade_count,
                     SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins,
                     SUM(CASE WHEN pnl < 0 THEN 1 ELSE 0 END) as losses,
                     SUM(COALESCE(pnl, 0)) as total_pnl
                   FROM trades
                   WHERE timestamp >= ?""",
                (today,)
            )
            row = cursor.fetchone()
            return {
                "trade_count": row[0] or 0,
                "wins": row[1] or 0,
                "losses": row[2] or 0,
                "total_pnl": row[3] or 0,
            }
        except Exception as e:
            logger.error(f"查询日统计失败: {e}")
            return {"trade_count": 0, "wins": 0, "losses": 0, "total_pnl": 0}

    def close(self):
        if self.conn:
            self.conn.close()
