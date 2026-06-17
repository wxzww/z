"""
主流水线 - 编排完整的分析→决策→风控→执行流程
"""
import json
import time
import threading
from utils.logger import get_logger
from utils.config import AppConfig
from core.data_collector import DataCollector
from core.state_builder import StateBuilder
from core.strategy_engine import StrategyEngine
from core.risk_manager import RiskManager
from core.order_executor import OrderExecutor
from storage.database import Database
from notifications.telegram_notifier import TelegramNotifier

logger = get_logger("btc_trader.pipeline")


class TradingPipeline:

    def __init__(self, config: AppConfig):
        self.config = config
        self.lock = threading.Lock()

        self.data_collector = DataCollector(config)
        self.state_builder = StateBuilder(self.data_collector)
        self.strategy_engine = StrategyEngine(config)
        self.risk_manager = RiskManager(config.risk)
        self.order_executor = OrderExecutor(config, self.data_collector.client)
        self.db = Database(config.db_path)
        self.notifier = TelegramNotifier(config.telegram)

        # 动态监控开关（LLM 通过 next_analysis 控制）
        self._monitor = {"1h": True}

        logger.info("TradingPipeline初始化完成")

    def run(self, trigger: str):
        if not self.lock.acquire(blocking=False):
            logger.warning(f"上一次分析尚未完成，跳过 trigger={trigger}")
            return

        try:
            start = time.time()

            # 动态监控：非 4H 时检查是否跳过
            if trigger != "4h" and not self._should_run(trigger):
                logger.info(f"⏭️  跳过 {trigger} 分析（LLM建议暂停监控）")
                return

            logger.info(f"========== 流水线启动 trigger={trigger} ==========")

            # Step 2: 数据采集
            logger.info("Step 2: 数据采集")
            if not self._collect_data(trigger):
                logger.error("数据采集失败")
                return

            # Step 3: 检查限价入场单是否已成交（成交后自动补挂止损止盈）
            self.order_executor.check_pending_fills()

            # Step 4: 状态组装
            logger.info("Step 4: 状态同步与组装")
            state = self.state_builder.build(trigger)
            if not state:
                logger.error("状态组装失败")
                return

            # Step 5: 调用LLM决策
            logger.info("Step 5: 调用LLM策略分析")
            state_dict = state if isinstance(state, dict) else state.dict()
            decision = self.strategy_engine.analyze(state_dict)

            # Step 5.5: 更新监控开关
            self._update_monitor(trigger, decision, state_dict)

            # Step 6: 执行操作（LLM决策直接执行，不做风控过滤）
            logger.info("Step 6: 执行操作")
            account_raw = state_dict.get("account", {})
            actions = decision.get("actions", [])
            results = self.order_executor.execute_actions(actions, account_raw)

            # Step 7: 记录 & 通知
            self._record_decision(trigger, state_dict, decision, decision, results)
            self._send_notifications(trigger, decision, results)
            self._post_execution_check(results)

            elapsed = time.time() - start
            logger.info(
                f"========== 流水线完成 trigger={trigger} "
                f"耗时={elapsed:.2f}s actions={len(results)} ==========\n"
            )

        except Exception as e:
            logger.error(f"流水线异常: {e}", exc_info=True)
            self.notifier.send_sync(f"🚨 流水线异常: {e}")
        finally:
            self.lock.release()

    # ============================================================
    # 数据采集
    # ============================================================

    def _collect_data(self, trigger: str) -> bool:
        try:
            for tf in self.config.strategy.timeframes:
                if tf == trigger or tf not in self.data_collector._cache:
                    df = self.data_collector.fetch_klines(tf)
                else:
                    df = self.data_collector._cache.get(tf)

                if df is None or len(df) < 60:
                    logger.warning(f"{tf} 数据不足，强制刷新")
                    df = self.data_collector.fetch_klines(tf)
                    if df is None or len(df) < 60:
                        logger.error(f"{tf} 数据采集失败")
                        return False
            return True
        except Exception as e:
            logger.error(f"数据采集异常: {e}")
            return False

    # ============================================================
    # 动态监控
    # ============================================================

    def _should_run(self, trigger: str) -> bool:
        if trigger == "4h":
            return True
        try:
            positions = self.data_collector.get_positions()
            if positions:
                return True
            orders = self.data_collector.get_open_orders()
            if any(o.get("type") == "LIMIT" for o in orders):
                return True
        except Exception:
            return True
        return self._monitor.get(trigger, True)

    def _update_monitor(self, trigger: str, decision: dict, state: dict):
        na = decision.get("next_analysis", {})
        if not na:
            return
        old_1h = self._monitor["1h"]
        if trigger == "4h":
            self._monitor["1h"] = na.get("monitor_1h", True)
        reason = na.get("reason", "")
        if old_1h != self._monitor["1h"]:
            logger.info(f"📡 监控调整: 1H={'🟢开' if self._monitor['1h'] else '🔴关'}  {reason}")

    # ============================================================
    # 记录 / 通知
    # ============================================================

    def _record_decision(self, trigger, state_dict, raw_decision, safe_decision, results):
        try:
            raw_actions = set(a.get("action", "") for a in raw_decision.get("actions", []))
            safe_actions = set(a.get("action", "") for a in safe_decision.get("actions", []))
            self.db.insert_decision(
                trigger=trigger,
                input_json=json.dumps(state_dict, ensure_ascii=False, default=str),
                output_json=json.dumps(raw_decision, ensure_ascii=False, default=str),
                actions_executed=json.dumps(
                    [r for r in results if r.get("status") == "success"],
                    ensure_ascii=False, default=str),
                risk_filtered=json.dumps(list(raw_actions - safe_actions), ensure_ascii=False),
            )
            for result in results:
                if result.get("status") == "success" and result.get("action") in (
                    "open_long", "open_short", "close_long", "close_short",
                    "add_position", "reduce_position",
                ):
                    self.db.insert_trade(
                        symbol=self.config.binance.symbol,
                        side=result.get("action", ""),
                        action=result.get("action", ""),
                        price=result.get("price", 0),
                        quantity=result.get("quantity", 0),
                        reason=next(
                            (a.get("reason", "") for a in safe_decision.get("actions", [])
                             if a.get("action") == result.get("action")), ""),
                        confidence=safe_decision.get("analysis", {}).get("confidence", 0),
                    )
        except Exception as e:
            logger.error(f"记录决策失败: {e}")

    def _send_notifications(self, trigger, decision, results):
        try:
            executed = [r for r in results
                        if r.get("status") == "success"
                        and r.get("action") not in ("hold", "no_action")]
            if not executed:
                return
            analysis = decision.get("analysis", {})
            msg = [
                f"📊 {trigger.upper()} 分析完成",
                f"方向: {analysis.get('direction')}  信心: {'⭐' * analysis.get('confidence', 0)}",
                f"逻辑: {analysis.get('reasoning', '')}",
            ]
            for r in executed:
                msg.append(f"✅ {r.get('action')}: qty={r.get('quantity', '')} price={r.get('price', '')}")
            for w in decision.get("risk_warnings", []):
                msg.append(f"⚠️ {w}")
            self.notifier.send_sync("\n".join(msg))
        except Exception as e:
            logger.error(f"通知失败: {e}")

    def _post_execution_check(self, results):
        for result in results:
            if result.get("action") in ("close_long", "close_short") and result.get("status") == "success":
                pnl = result.get("pnl")
                if pnl is not None:
                    if pnl < 0:
                        self.risk_manager.record_stop_loss(pnl)
                    else:
                        self.risk_manager.record_profit(pnl)

    # ============================================================
    # 对外接口
    # ============================================================

    def startup(self):
        logger.info("系统启动中...")
        logger.info("测试Binance连接...")
        try:
            self.data_collector.client.futures_ping()
            logger.info("Binance连接正常")
        except Exception as e:
            raise RuntimeError(f"Binance连接失败: {e}")

        logger.info("测试LLM API连接...")
        if not self.strategy_engine.ping():
            raise RuntimeError("LLM API连接失败")
        logger.info("LLM API连接正常")

        try:
            self.data_collector.client.futures_change_leverage(
                symbol=self.config.binance.symbol,
                leverage=self.config.binance.leverage)
            logger.info(f"杠杆: {self.config.binance.leverage}x")
            self.data_collector.client.futures_change_margin_type(
                symbol=self.config.binance.symbol,
                marginType=self.config.binance.margin_type)
        except Exception as e:
            logger.warning(f"设置杠杆/保证金: {e}")

        self.db.initialize()
        logger.info("执行初始4H分析...")
        self.run("4h")
        self.notifier.send_sync(
            f"✅ 启动成功 | {self.config.binance.symbol} "
            f"{self.config.binance.leverage}x "
            f"{'测试网' if self.config.binance.testnet else '主网'}")
        logger.info("系统启动完成")
    def startup_light(self):
        """轻量启动：只测连接和初始化数据库，不做初始分析（给单次运行模式用）"""
        logger.info("轻量启动...")
        try:
            self.data_collector.client.futures_ping()
            logger.info("Binance连接正常")
        except Exception as e:
            raise RuntimeError(f"Binance连接失败: {e}")
        if not self.strategy_engine.ping():
            raise RuntimeError("LLM API连接失败")
        logger.info("LLM API连接正常")
        self.db.initialize()
    def shutdown(self, reason="manual"):
        logger.info(f"系统关闭: {reason}")
        self.notifier.send_sync(f"⏸️ 关闭: {reason}")

    def emergency_stop(self, reason):
        logger.critical(f"紧急停止: {reason}")
        self.risk_manager.trigger_emergency(reason)
        self.order_executor.emergency_cancel_all()
        self.notifier.send_sync(f"🚨 紧急停止: {reason}")