"""
Telegram通知模块
支持同步和异步发送
"""
import requests
from utils.logger import get_logger

logger = get_logger("btc_trader.notify")


class TelegramNotifier:
    def __init__(self, config):
        self.enabled = getattr(config, "enabled", False)
        self.bot_token = getattr(config, "bot_token", "")
        self.chat_id = getattr(config, "chat_id", "")
        self.base_url = f"https://api.telegram.org/bot{self.bot_token}"

    def send_sync(self, message: str):
        """同步发送消息"""
        if not self.enabled or not self.bot_token or not self.chat_id:
            logger.debug(f"Telegram未启用，消息: {message[:100]}")
            return

        try:
            # Telegram消息最大4096字符
            if len(message) > 4000:
                message = message[:4000] + "\n...(截断)"

            resp = requests.post(
                f"{self.base_url}/sendMessage",
                json={
                    "chat_id": self.chat_id,
                    "text": message,
                    "parse_mode": "HTML",
                },
                timeout=10
            )
            if resp.status_code != 200:
                logger.warning(f"Telegram发送失败: {resp.status_code} {resp.text[:200]}")
        except requests.Timeout:
            logger.warning("Telegram发送超时")
        except Exception as e:
            logger.warning(f"Telegram发送异常: {e}")

    def send_trade_open(self, side: str, price: float, quantity: float,
                        stop_loss: float, take_profit: float,
                        reason: str, confidence: int):
        msg = (
            f"🟢 <b>开仓通知</b>\n"
            f"方向: {side}\n"
            f"价格: {price}\n"
            f"数量: {quantity} BTC\n"
            f"止损: {stop_loss}\n"
            f"止盈: {take_profit}\n"
            f"原因: {reason}\n"
            f"信心: {'⭐' * confidence}"
        )
        self.send_sync(msg)

    def send_trade_close(self, side: str, price: float,
                         pnl: float, reason: str):
        emoji = "🟢" if pnl >= 0 else "🔴"
        msg = (
            f"{emoji} <b>平仓通知</b>\n"
            f"方向: {side}\n"
            f"价格: {price}\n"
            f"盈亏: {pnl:.2f} USDT\n"
            f"原因: {reason}"
        )
        self.send_sync(msg)

    def send_stop_loss(self, loss: float, consecutive: int):
        msg = (
            f"⚠️ <b>止损触发</b>\n"
            f"亏损: {loss:.2f} USDT\n"
            f"连续止损: {consecutive}次\n"
            f"冷静期: 15分钟后恢复"
        )
        self.send_sync(msg)

    def send_daily_report(self, stats: dict, balance: float):
        msg = (
            f"📊 <b>每日报告</b>\n"
            f"交易次数: {stats.get('trade_count', 0)}\n"
            f"胜/负: {stats.get('wins', 0)}/{stats.get('losses', 0)}\n"
            f"今日盈亏: {stats.get('total_pnl', 0):.2f} USDT\n"
            f"账户余额: {balance:.2f} USDT"
        )
        self.send_sync(msg)
