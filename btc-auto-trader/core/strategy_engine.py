"""
LLM决策模块 - 通过 OpenRouter 调用 Claude API 进行策略分析
使用 prompts/system_prompt.py 作为 System Prompt
"""
import json
import time
import requests
from utils.logger import get_logger
from utils.config import AppConfig
from prompts.system_prompt import SYSTEM_PROMPT

logger = get_logger("btc_trader.strategy")


class StrategyEngine:
    """
    通过 OpenRouter 调用 Claude 获取交易决策。
    每次调用都使用完整的策略Skill作为System Prompt。
    """

    def __init__(self, config: AppConfig):
        self.config = config
        self.llm = config.llm
        self.api_url = f"{self.llm.base_url}/chat/completions"
        self.headers = {
            "Authorization": f"Bearer {self.llm.api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/btc-auto-trader",
            "X-Title": "BTC Auto Trader",
        }

    def analyze(self, state: dict, max_retries: int = None) -> dict:
        """
        调用 Claude（经 OpenRouter）进行策略分析

        Args:
            state: StateBuilder组装的完整状态JSON
            max_retries: 最大重试次数（默认取配置值）

        Returns:
            解析后的决策dict（包含analysis和actions）
        """
        if max_retries is None:
            max_retries = self.llm.max_retries

        user_message = json.dumps(state, ensure_ascii=False)
        raw_text = ""

        for attempt in range(max_retries):
            try:
                logger.info(
                    f"调用 LLM API [attempt={attempt+1}] "
                    f"model={self.llm.model}, "
                    f"trigger={state.get('trigger')}, "
                    f"price={state.get('current_price')}"
                )

                start_time = time.time()

                payload = {
                    "model": self.llm.model,
                    "max_tokens": self.llm.max_tokens,
                    "temperature": self.llm.temperature,
                    "messages": [
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": user_message},
                    ],
                }

                resp = requests.post(
                    self.api_url,
                    headers=self.headers,
                    json=payload,
                    timeout=self.llm.timeout,
                )

                elapsed = time.time() - start_time
                logger.info(f"LLM 响应耗时: {elapsed:.2f}s, status={resp.status_code}")

                if resp.status_code != 200:
                    error_body = resp.text[:500]
                    logger.error(f"LLM API HTTP {resp.status_code}: {error_body}")
                    # 429/5xx 可重试
                    if resp.status_code in (429, 500, 502, 503, 504):
                        time.sleep(2 ** attempt)
                        continue
                    raise RuntimeError(f"LLM API 错误 {resp.status_code}: {error_body}")

                data = resp.json()

                # OpenRouter 兼容 OpenAI 格式
                choices = data.get("choices", [])
                if not choices:
                    raise ValueError(f"LLM 返回空 choices: {data}")

                raw_text = choices[0].get("message", {}).get("content", "").strip()
                if not raw_text:
                    raise ValueError("LLM 返回空内容")

                # 解析JSON
                decision = self._parse_response(raw_text)

                # 校验决策格式
                validation_ok, errors = self._validate_decision(decision, state)
                if not validation_ok:
                    logger.warning(f"决策格式校验问题: {errors}")
                    fatal = [e for e in errors if "FATAL" in e]
                    if fatal:
                        raise ValueError(f"致命校验错误: {fatal}")

                # ---- 详细记录LLM决策 ----
                self._log_decision(decision)

                return decision

            except json.JSONDecodeError as e:
                logger.warning(f"LLM 返回非JSON [attempt={attempt+1}]: {e}")
                logger.debug(f"原始响应: {raw_text[:500]}")
            except requests.Timeout:
                logger.error(f"LLM API 超时 [attempt={attempt+1}]")
                time.sleep(5)
            except requests.ConnectionError as e:
                logger.error(f"LLM API 连接失败 [attempt={attempt+1}]: {e}")
                time.sleep(2 ** attempt)
            except Exception as e:
                logger.error(f"策略分析异常 [attempt={attempt+1}]: {e}")
                time.sleep(2)

        # 所有重试失败
        logger.critical("LLM API 连续失败，返回安全默认决策")
        return self._safe_default_decision("LLM API 连续失败")

    def _log_decision(self, decision: dict):
        """详细记录LLM的完整策略决策"""
        a = decision.get("analysis", {})
        actions = decision.get("actions", [])
        warnings = decision.get("risk_warnings", [])

        # ---- 分析摘要 ----
        stars = "⭐" * a.get("confidence", 0)
        logger.info("─" * 55)
        logger.info(f"📊 LLM 策略决策")
        logger.info(f"  方向: {a.get('direction', '?')}  |  "
                     f"信心: {a.get('confidence', '?')} {stars}  |  "
                     f"行情类型: {a.get('market_type', '?')}")
        logger.info(f"  共振: {'✅ ' + a.get('resonance_direction', '') if a.get('resonance') else '❌ 无共振'}")

        # ---- 各周期状态 ----
        logger.info(f"  4H: {a.get('4h_status', '-')}")
        logger.info(f"  1H: {a.get('1h_status', '-')}")
        logger.info(f" 15M: {a.get('15m_status', '-')}")

        # ---- 关键价位 ----
        kl = a.get("key_levels", {})
        if kl:
            r1 = kl.get("resistance_1", "-")
            r2 = kl.get("resistance_2", "-")
            s1 = kl.get("support_1", "-")
            s2 = kl.get("support_2", "-")
            logger.info(f"  压力位: {r1} / {r2}  |  支撑位: {s1} / {s2}")

        # ---- 核心逻辑 ----
        reasoning = a.get("reasoning", "")
        if reasoning:
            logger.info(f"  逻辑: {reasoning}")

        # ---- 具体操作 ----
        if actions:
            logger.info(f"  操作 ({len(actions)} 条):")
            for i, act in enumerate(actions, 1):
                act_type = act.get("action", "?")
                parts = [f"    [{i}] {act_type}"]
                if act.get("side"):
                    parts.append(f"side={act['side']}")
                if act.get("price"):
                    parts.append(f"price={act['price']}")
                if act.get("quantity_pct"):
                    parts.append(f"qty={act['quantity_pct']*100:.0f}%")
                if act.get("stop_loss_price"):
                    parts.append(f"SL={act['stop_loss_price']}")
                if act.get("take_profit_price"):
                    parts.append(f"TP={act['take_profit_price']}")
                if act.get("take_profit_2_price"):
                    parts.append(f"TP2={act['take_profit_2_price']}")
                if act.get("new_stop_price"):
                    parts.append(f"newSL={act['new_stop_price']}")
                if act.get("cancel_order_id"):
                    parts.append(f"cancelId={act['cancel_order_id']}")
                logger.info("  ".join(parts))
                if act.get("reason"):
                    logger.info(f"        原因: {act['reason']}")

        # ---- 风险提示 ----
        if warnings:
            for w in warnings:
                logger.warning(f"  ⚠️ {w}")

        # ---- 监控频率建议 ----
        na = decision.get("next_analysis", {})
        if na:
            m1h = na.get("monitor_1h")
            m15m = na.get("monitor_15m")
            parts = []
            if m1h is not None:
                parts.append(f"1H={'🟢开' if m1h else '🔴关'}")
            if m15m is not None:
                parts.append(f"15M={'🟢开' if m15m else '🔴关'}")
            if parts:
                reason = na.get("reason", "")
                logger.info(f"  📡 下周期监控: {' '.join(parts)}  {reason}")

        logger.info("─" * 55)

    def _parse_response(self, raw_text: str) -> dict:
        """解析LLM返回的JSON"""
        text = raw_text.strip()

        # 去除可能的markdown代码块包裹
        if text.startswith("```"):
            lines = text.split("\n")
            lines = [l for l in lines if not l.strip().startswith("```")]
            text = "\n".join(lines).strip()

        return json.loads(text)

    def _validate_decision(self, decision: dict, state: dict) -> tuple[bool, list[str]]:
        """校验LLM返回的决策是否合理"""
        errors = []

        # 1. 必要字段
        if "analysis" not in decision:
            errors.append("FATAL: 缺少analysis字段")
            return False, errors
        if "actions" not in decision:
            errors.append("FATAL: 缺少actions字段")
            return False, errors

        analysis = decision["analysis"]
        actions = decision["actions"]

        # 2. confidence范围
        confidence = analysis.get("confidence", 0)
        if not (0 <= confidence <= 5):
            errors.append(f"confidence超出范围: {confidence}")

        # 3. confidence=1时不允许开仓
        if confidence <= 1:
            opening_actions = [
                a for a in actions
                if a.get("action") in ("open_long", "open_short", "add_position", "place_limit_entry")
            ]
            if opening_actions:
                errors.append(f"confidence={confidence}但有开仓action，将被过滤")
                decision["actions"] = [
                    a for a in actions
                    if a.get("action") not in ("open_long", "open_short", "add_position", "place_limit_entry")
                ]

        # 4. 开仓必须有止损和止盈
        for action in actions:
            act_type = action.get("action", "")
            if act_type in ("open_long", "open_short", "add_position", "place_limit_entry"):
                if not action.get("stop_loss_price"):
                    errors.append(f"FATAL: {act_type} 缺少 stop_loss_price")
                if not action.get("take_profit_price"):
                    errors.append(f"FATAL: {act_type} 缺少 take_profit_price")

                # 止损方向校验
                price = action.get("price") or state.get("current_price", 0)
                sl = action.get("stop_loss_price", 0)
                tp = action.get("take_profit_price", 0)

                if act_type in ("open_long", "add_position", "place_limit_entry") and action.get("side", "") != "sell":
                    if sl and price and sl >= price:
                        errors.append(f"做多止损({sl})必须低于入场价({price})")
                    if tp and price and tp <= price:
                        errors.append(f"做多止盈({tp})必须高于入场价({price})")
                elif act_type in ("open_short",) or action.get("side") == "sell":
                    if sl and price and sl <= price:
                        errors.append(f"做空止损({sl})必须高于入场价({price})")
                    if tp and price and tp >= price:
                        errors.append(f"做空止盈({tp})必须低于入场价({price})")

        # 5. 矛盾检测
        action_types = [a["action"] for a in actions]
        if "open_long" in action_types and "open_short" in action_types:
            errors.append("FATAL: 同时开多和开空")

        has_fatal = any("FATAL" in e for e in errors)
        return not has_fatal, errors

    def _safe_default_decision(self, reason: str) -> dict:
        """安全默认决策（不操作）"""
        return {
            "analysis": {
                "4h_status": "无法分析",
                "1h_status": "无法分析",
                "15m_status": "无法分析",
                "resonance": False,
                "resonance_direction": "none",
                "market_type": "transitioning",
                "direction": "neutral",
                "confidence": 0,
                "key_levels": {},
                "reasoning": reason,
            },
            "actions": [{"action": "no_action", "reason": reason}],
            "risk_warnings": [f"系统异常: {reason}"],
        }

    def ping(self) -> bool:
        """测试 OpenRouter API 连通性"""
        try:
            resp = requests.post(
                self.api_url,
                headers=self.headers,
                json={
                    "model": self.llm.model,
                    "max_tokens": 10,
                    "messages": [{"role": "user", "content": "ping"}],
                },
                timeout=15,
            )
            return resp.status_code == 200
        except Exception as e:
            logger.error(f"LLM API ping 失败: {e}")
            return False