"""
配置加载器
"""
import yaml
import os
from dataclasses import dataclass, field
from typing import List


@dataclass
class BinanceConfig:
    api_key: str = ""
    api_secret: str = ""
    testnet: bool = True
    symbol: str = "BTCUSDT"
    leverage: int = 10
    margin_type: str = "CROSSED"
    position_mode: str = "hedge"   # "hedge"=双向持仓  "oneway"=单向持仓


@dataclass
class LLMConfig:
    """LLM配置 — 通过 OpenRouter 调用 Claude"""
    api_key: str = ""
    model: str = "anthropic/claude-sonnet-4-20250514"
    max_tokens: int = 2000
    temperature: float = 0
    max_retries: int = 3
    timeout: int = 60
    base_url: str = "https://openrouter.ai/api/v1"


@dataclass
class StrategyConfig:
    ema_fast: int = 15
    ema_slow: int = 60
    macd_fast: int = 12
    macd_slow: int = 26
    macd_signal: int = 9
    rsi_period: int = 14
    kline_limit: int = 100
    timeframes: List[str] = field(default_factory=lambda: ["4h", "1h", "15m"])


@dataclass
class RiskConfig:
    max_position_pct: float = 0.30
    max_single_loss_pct: float = 0.02
    max_daily_loss_pct: float = 0.05
    max_leverage: int = 20
    max_consecutive_losses: int = 5
    cooldown_seconds: int = 900
    min_stop_distance_pct: float = 0.003
    emergency_stop_pct: float = 0.05
    emergency_stop_loss_pct: float = 0.05
    emergency_stop_margin_ratio: float = 0.8
    extreme_move_pct: float = 0.05
    max_open_orders: int = 10


@dataclass
class TelegramConfig:
    bot_token: str = ""
    chat_id: str = ""
    enabled: bool = False


@dataclass
class AppConfig:
    binance: BinanceConfig = field(default_factory=BinanceConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    telegram: TelegramConfig = field(default_factory=TelegramConfig)
    log_level: str = "INFO"
    log_file: str = "logs/trading.log"
    db_path: str = "storage/trading.db"


def _pick_fields(datacls, raw_dict: dict) -> dict:
    """只保留 dataclass 定义过的字段，丢弃 yaml 里多余的 key，避免 TypeError"""
    import dataclasses
    valid = {f.name for f in dataclasses.fields(datacls)}
    return {k: v for k, v in raw_dict.items() if k in valid}


def load_config(path: str = "config.yaml") -> AppConfig:
    """加载配置文件"""
    if not os.path.exists(path):
        raise FileNotFoundError(f"Config file not found: {path}")

    with open(path, "r") as f:
        raw = yaml.safe_load(f)

    # ---- 兼容 yaml 中 key 名不一致的情况 ----
    risk_raw = dict(raw.get("risk", {}))
    # yaml: cooldown_after_stop_sec → dataclass: cooldown_seconds
    if "cooldown_after_stop_sec" in risk_raw and "cooldown_seconds" not in risk_raw:
        risk_raw["cooldown_seconds"] = risk_raw.pop("cooldown_after_stop_sec")

    # ---- 兼容旧 yaml 中 "claude" 段 → 新 LLMConfig ----
    llm_raw = dict(raw.get("claude", {}))
    # 如果用户还没改 yaml，把 api_key 映射过来
    # 如果 yaml 已有 "llm" 段，优先使用
    if "llm" in raw:
        llm_raw = dict(raw["llm"])

    config = AppConfig(
        binance=BinanceConfig(**_pick_fields(BinanceConfig, raw.get("binance", {}))),
        llm=LLMConfig(**_pick_fields(LLMConfig, llm_raw)),
        strategy=StrategyConfig(**_pick_fields(StrategyConfig, raw.get("strategy", {}))),
        risk=RiskConfig(**_pick_fields(RiskConfig, risk_raw)),
        telegram=TelegramConfig(**_pick_fields(TelegramConfig, raw.get("telegram", {}))),
        log_level=raw.get("logging", {}).get("level", "INFO"),
        log_file=raw.get("logging", {}).get("file", "logs/trading.log"),
        db_path=raw.get("database", {}).get("path", "storage/trading.db"),
    )

    # 环境变量覆盖（敏感信息优先从环境变量读取）
    config.binance.api_key = os.environ.get("BINANCE_API_KEY", config.binance.api_key)
    config.binance.api_secret = os.environ.get("BINANCE_API_SECRET", config.binance.api_secret)
    config.llm.api_key = os.environ.get("OPENROUTER_API_KEY",
                         os.environ.get("CLAUDE_API_KEY", config.llm.api_key))
    config.telegram.bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", config.telegram.bot_token)
    config.telegram.chat_id = os.environ.get("TELEGRAM_CHAT_ID", config.telegram.chat_id)

    return config