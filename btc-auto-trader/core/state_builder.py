"""
状态组装模块 - 将K线数据+持仓+挂单组装成发给Claude的完整状态
"""
from datetime import datetime, timezone
from utils.logger import get_logger

logger = get_logger("btc_trader.state")


class StateBuilder:
    """
    每次调用Claude之前，组装完整的输入状态
    """

    def __init__(self, data_collector, risk_state: str = "normal"):
        self.dc = data_collector
        self.risk_state = risk_state

    def build(self, trigger: str) -> dict:
        """
        构建完整状态JSON

        Args:
            trigger: 触发周期 "15m" / "1h" / "4h"

        Returns:
            发给Claude的完整状态dict
        """
        logger.info(f"开始组装状态 [trigger={trigger}]")

        # 1. 市场数据：被触发的周期拉新数据，其余用缓存
        market_data = {}
        for tf in ["4h", "1h", "15m"]:
            if tf == trigger:
                # 触发周期：拉取新数据
                n_candles = {"4h": 5, "1h": 8, "15m": 10}[tf]
                indicators = self.dc.fetch_and_extract(tf, n_candles)
            else:
                # 非触发周期：使用缓存
                n_candles = {"4h": 5, "1h": 8, "15m": 10}[tf]
                indicators = self.dc.get_cached_indicators(tf, n_candles)

            if indicators:
                market_data[tf] = indicators
            else:
                logger.warning(f"无法获取 [{tf}] 数据")

        # 2. 当前价格
        current_price = self.dc.get_current_price()

        # 3. 持仓
        positions = self.dc.get_positions()

        # 4. 挂单
        open_orders = self.dc.get_open_orders()

        # 5. 账户
        account = self.dc.get_account_info()

        # 6. 最近交易
        recent_trades = self.dc.get_recent_trades(limit=5)

        # 7. 资金费率
        funding_rate = self.dc.get_funding_rate()

        # 8. 一致性检查
        issues = self._check_consistency(positions, open_orders, account)
        if issues:
            for issue in issues:
                logger.warning(f"状态一致性: {issue}")

        state = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "trigger": trigger,
            "symbol": self.dc.symbol,
            "current_price": round(current_price, 2),
            "market_data": market_data,
            "positions": positions,
            "open_orders": open_orders,
            "account": account,
            "recent_trades": recent_trades,
            "funding_rate": round(funding_rate, 6),
            "risk_state": self.risk_state,
            "state_issues": issues,
        }

        logger.info(
            f"状态组装完成: price={current_price:.2f}, "
            f"positions={len(positions)}, orders={len(open_orders)}, "
            f"balance={account.get('total_balance', 0):.2f}"
        )
        return state

    def _check_consistency(self, positions: list, orders: list, account: dict) -> list[str]:
        """持仓/挂单/账户一致性检查"""
        issues = []

        # 有持仓但无止损
        if positions:
            has_stop = any(
                o.get("purpose") == "stop_loss" or o.get("order_type") == "STOP_MARKET"
                for o in orders
            )
            if not has_stop:
                issues.append("CRITICAL: 有持仓但无止损单，需要立即补挂")

        # 无持仓但有非入场挂单
        if not positions:
            orphan_orders = [
                o for o in orders
                if o.get("purpose") not in ("limit_entry_or_tp", "entry", "unknown")
                and o.get("order_type") in ("STOP_MARKET", "TAKE_PROFIT_MARKET")
            ]
            if orphan_orders:
                issues.append(f"WARNING: 无持仓但有{len(orphan_orders)}个孤儿止损/止盈单")

        # 止损数量 vs 持仓数量
        if positions:
            pos_qty = sum(p["quantity"] for p in positions)
            stop_qty = sum(
                o["quantity"] for o in orders
                if o.get("order_type") == "STOP_MARKET"
            )
            if stop_qty > 0 and abs(stop_qty - pos_qty) / pos_qty > 0.05:
                issues.append(
                    f"WARNING: 止损数量({stop_qty:.6f})与持仓({pos_qty:.6f})不匹配"
                )

        # 保证金率
        margin_ratio = account.get("margin_ratio", 0)
        if margin_ratio > 0.8:
            issues.append(f"CRITICAL: 保证金率过高 {margin_ratio:.2%}，接近爆仓")
        elif margin_ratio > 0.5:
            issues.append(f"WARNING: 保证金率偏高 {margin_ratio:.2%}")

        return issues
