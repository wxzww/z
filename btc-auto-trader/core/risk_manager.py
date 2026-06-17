"""
风控校验模块 - Claude决策执行前的最后一道防线
所有硬性规则都在这里，即使Claude"发疯"也能拦住
"""
import time
from datetime import datetime, timezone
from utils.logger import get_logger
from utils.config import RiskConfig
from models.schemas import RiskState

logger = get_logger("btc_trader.risk")


class RiskManager:
    def __init__(self, config: RiskConfig):
        self.config = config
        self.state = RiskState.NORMAL

        # 运行时追踪
        self.last_stop_loss_time: float = 0           # 上次止损时间戳
        self.consecutive_losses: int = 0              # 连续止损次数
        self.daily_realized_pnl: float = 0            # 今日已实现盈亏
        self.daily_reset_date: str = ""               # 日期（UTC），用于重置
        self.trade_count_today: int = 0               # 今日交易次数

    def validate_decision(self, decision: dict, account: dict,
                          positions: list, orders: list,
                          current_price: float) -> dict:
        """
        校验Claude的决策，过滤不合规的action

        Returns:
            过滤后的安全决策
        """
        self._check_daily_reset()

        safe_actions = []
        filtered_reasons = []

        for action in decision.get("actions", []):
            act_type = action.get("action", "")

            # 无操作类直接通过
            if act_type in ("hold", "no_action"):
                safe_actions.append(action)
                continue

            # 逐项检查
            passed, reason = self._check_action(
                action, act_type, account, positions, orders, current_price
            )

            if passed:
                safe_actions.append(action)
            else:
                filtered_reasons.append(f"{act_type}: {reason}")
                logger.warning(f"风控拦截: {act_type} - {reason}")

        if filtered_reasons:
            decision.setdefault("risk_warnings", []).extend(
                [f"[风控拦截] {r}" for r in filtered_reasons]
            )

        decision["actions"] = safe_actions
        return decision

    def _check_action(self, action: dict, act_type: str,
                      account: dict, positions: list,
                      orders: list, current_price: float) -> tuple[bool, str]:
        """检查单个action"""

        # ===== 系统状态检查 =====
        if self.state == RiskState.EMERGENCY:
            if act_type not in ("cancel_order", "cancel_all", "close_long", "close_short"):
                return False, "紧急停止状态，仅允许平仓和撤单"

        if self.state == RiskState.DAILY_LIMIT:
            if act_type in ("open_long", "open_short", "add_position", "place_limit_entry"):
                return False, "日亏损达限，暂停开仓"

        if self.state == RiskState.CONSECUTIVE_LOSS:
            if act_type in ("open_long", "open_short", "add_position", "place_limit_entry"):
                return False, f"连续止损{self.consecutive_losses}次，需人工重启"

        # ===== 开仓类检查 =====
        if act_type in ("open_long", "open_short", "add_position", "place_limit_entry"):
            return self._check_opening(action, act_type, account, positions, current_price)

        # ===== 移动止损检查 =====
        if act_type == "move_stop_loss":
            return self._check_move_stop(action, positions, current_price, account)

        # ===== 撤单/平仓类 - 通常放行 =====
        return True, ""

    def _check_opening(self, action: dict, act_type: str,
                       account: dict, positions: list,
                       current_price: float) -> tuple[bool, str]:
        """开仓类风控检查"""

        total_balance = account.get("total_balance", 0)
        if total_balance <= 0:
            return False, "账户余额为0"

        qty_pct = action.get("quantity_pct", 0)
        sl_price = action.get("stop_loss_price")
        tp_price = action.get("take_profit_price")
        entry_price = action.get("price") or current_price

        # 1. 必须有止损和止盈
        if not sl_price:
            return False, "开仓必须设置止损价"
        if not tp_price:
            return False, "开仓必须设置止盈价"

        # 2. 冷静期检查
        if self.last_stop_loss_time > 0:
            elapsed = time.time() - self.last_stop_loss_time
            if elapsed < self.config.cooldown_seconds:
                remaining = int(self.config.cooldown_seconds - elapsed)
                return False, f"止损冷静期，还需等待{remaining}秒"

        # 3. 最大持仓检查
        current_pos_value = sum(
            p["quantity"] * p["entry_price"] for p in positions
        )
        new_value = qty_pct * total_balance
        total_pos_value = current_pos_value + new_value
        if total_pos_value > total_balance * self.config.max_position_pct:
            return False, (
                f"总仓位({total_pos_value:.0f})将超过上限"
                f"({total_balance * self.config.max_position_pct:.0f})"
            )

        # 4. 单笔止损金额检查
        sl_distance = abs(entry_price - sl_price)
        sl_pct = sl_distance / entry_price if entry_price > 0 else 0
        # 估算止损金额 = 仓位价值 × 止损百分比
        estimated_loss = new_value * sl_pct
        max_loss = total_balance * self.config.max_single_loss_pct
        if estimated_loss > max_loss:
            return False, (
                f"止损金额({estimated_loss:.2f})超过上限({max_loss:.2f})，"
                f"请缩小仓位或扩大止损距离"
            )

        # 5. 最小止损距离
        if sl_pct < self.config.min_stop_distance_pct:
            return False, (
                f"止损距离({sl_pct:.4%})小于最小值({self.config.min_stop_distance_pct:.4%})"
            )

        # 6. 日亏损检查
        if self.daily_realized_pnl < -(total_balance * self.config.max_daily_loss_pct):
            self.state = RiskState.DAILY_LIMIT
            return False, "今日亏损已达上限"

        # 7. 止损方向合理性
        if act_type in ("open_long", "add_position") or (act_type == "place_limit_entry" and action.get("side") == "buy"):
            if sl_price >= entry_price:
                return False, f"做多止损({sl_price})必须低于入场价({entry_price})"
            if tp_price <= entry_price:
                return False, f"做多止盈({tp_price})必须高于入场价({entry_price})"
        elif act_type == "open_short" or (act_type == "place_limit_entry" and action.get("side") == "sell"):
            if sl_price <= entry_price:
                return False, f"做空止损({sl_price})必须高于入场价({entry_price})"
            if tp_price >= entry_price:
                return False, f"做空止盈({tp_price})必须低于入场价({entry_price})"

        return True, ""

    def _check_move_stop(self, action: dict, positions: list,
                         current_price: float, account: dict) -> tuple[bool, str]:
        """移动止损检查"""
        new_price = action.get("new_stop_price")
        if not new_price:
            return False, "缺少new_stop_price"

        if not positions:
            return False, "无持仓，无法移动止损"

        pos = positions[0]
        entry = pos["entry_price"]
        total_balance = account.get("total_balance", 0)

        # 检查移动后的止损金额是否超限
        sl_distance = abs(entry - new_price)
        sl_pct = sl_distance / entry if entry > 0 else 0
        pos_value = pos["quantity"] * entry
        estimated_loss = pos_value * sl_pct
        max_loss = total_balance * self.config.max_single_loss_pct

        if estimated_loss > max_loss * 1.5:  # 移动止损允许比开仓宽50%
            return False, f"移动后止损金额({estimated_loss:.2f})过大"

        return True, ""

    # ============================================================
    # 状态更新方法
    # ============================================================

    def record_stop_loss(self, loss_amount: float):
        """记录一次止损"""
        self.last_stop_loss_time = time.time()
        self.consecutive_losses += 1
        self.daily_realized_pnl += loss_amount  # loss_amount是负数
        self.trade_count_today += 1

        logger.info(
            f"止损记录: loss={loss_amount:.2f}, "
            f"consecutive={self.consecutive_losses}, "
            f"daily_pnl={self.daily_realized_pnl:.2f}"
        )

        if self.consecutive_losses >= self.config.max_consecutive_losses:
            self.state = RiskState.CONSECUTIVE_LOSS
            logger.critical(f"连续止损{self.consecutive_losses}次，系统暂停")

        self.state = RiskState.COOLDOWN

    def record_profit(self, profit_amount: float):
        """记录一次盈利"""
        self.consecutive_losses = 0  # 重置连续亏损
        self.daily_realized_pnl += profit_amount
        self.trade_count_today += 1

    def check_cooldown_expired(self):
        """检查冷静期是否结束"""
        if self.state == RiskState.COOLDOWN:
            if time.time() - self.last_stop_loss_time >= self.config.cooldown_seconds:
                self.state = RiskState.NORMAL
                logger.info("冷静期结束，恢复正常交易")

    def trigger_emergency(self, reason: str):
        """触发紧急停止"""
        self.state = RiskState.EMERGENCY
        logger.critical(f"紧急停止: {reason}")

    def resume_normal(self):
        """恢复正常状态"""
        self.state = RiskState.NORMAL
        self.consecutive_losses = 0
        logger.info("系统恢复正常交易状态")

    def _check_daily_reset(self):
        """检查是否需要重置每日计数器"""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if today != self.daily_reset_date:
            self.daily_realized_pnl = 0
            self.trade_count_today = 0
            self.daily_reset_date = today
            if self.state == RiskState.DAILY_LIMIT:
                self.state = RiskState.NORMAL
                logger.info("新的交易日，日亏损限制重置")
