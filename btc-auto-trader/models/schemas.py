"""
数据模型定义 - 使用Pydantic进行类型校验
"""
from __future__ import annotations
from pydantic import BaseModel, Field, field_validator
from typing import Optional
from enum import Enum
from datetime import datetime


# ============================================================
# 枚举类型
# ============================================================

class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"

class Direction(str, Enum):
    LONG = "long"
    SHORT = "short"
    NEUTRAL = "neutral"

class MarketType(str, Enum):
    TRENDING_UP = "trending_up"
    TRENDING_DOWN = "trending_down"
    TRANSITIONING = "transitioning"
    RANGING = "ranging"

class OrderType(str, Enum):
    MARKET = "market"
    LIMIT = "limit"
    STOP_MARKET = "stop_market"

class ActionType(str, Enum):
    OPEN_LONG = "open_long"
    OPEN_SHORT = "open_short"
    CLOSE_LONG = "close_long"
    CLOSE_SHORT = "close_short"
    ADD_POSITION = "add_position"
    REDUCE_POSITION = "reduce_position"
    PLACE_STOP_LOSS = "place_stop_loss"
    PLACE_TAKE_PROFIT = "place_take_profit"
    MOVE_STOP_LOSS = "move_stop_loss"
    MOVE_TAKE_PROFIT = "move_take_profit"
    PLACE_LIMIT_ENTRY = "place_limit_entry"
    CANCEL_ORDER = "cancel_order"
    CANCEL_ALL = "cancel_all"
    REPLACE_ORDER = "replace_order"
    HOLD = "hold"
    NO_ACTION = "no_action"

class EmaRelationship(str, Enum):
    GOLDEN_CROSS = "golden_cross"
    DEATH_CROSS = "death_cross"
    TANGLED = "tangled"

class EmaDirection(str, Enum):
    UP = "up"
    DOWN = "down"
    FLAT = "flat"

class RiskState(str, Enum):
    NORMAL = "normal"
    CAUTION = "caution"
    COOLDOWN = "cooldown"
    DAILY_LIMIT = "daily_limit"
    CONSECUTIVE_LOSS = "consecutive_loss"
    EMERGENCY = "emergency"
    MAINTENANCE = "maintenance"


# ============================================================
# K线和指标数据
# ============================================================

class Candle(BaseModel):
    """单根K线"""
    open_time: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    close_time: int

class TimeframeIndicators(BaseModel):
    """单个周期的指标数据"""
    timeframe: str
    ema15: float
    ema60: float
    ema15_direction: EmaDirection
    ema60_direction: EmaDirection
    ema_relationship: EmaRelationship
    ema_gap: float  # EMA15 - EMA60
    macd_diff: float
    macd_dea: float
    macd_hist: float
    macd_hist_prev: float  # 上一根的MACD柱，用于判断方向
    rsi: float
    current_price: float
    recent_high: float  # 近30根K线最高
    recent_low: float   # 近30根K线最低
    last_candles: list[Candle] = Field(default_factory=list)


# ============================================================
# 持仓和订单
# ============================================================

class Position(BaseModel):
    """持仓信息"""
    symbol: str
    side: Direction  # long/short
    entry_price: float
    quantity: float  # BTC数量
    unrealized_pnl: float = 0.0
    unrealized_pnl_pct: float = 0.0
    leverage: int = 10
    margin: float = 0.0

class OpenOrder(BaseModel):
    """挂单信息"""
    order_id: str
    symbol: str
    side: Side
    order_type: str  # LIMIT / STOP_MARKET / TAKE_PROFIT_MARKET
    price: Optional[float] = None
    stop_price: Optional[float] = None
    quantity: float
    status: str = "NEW"
    purpose: str = ""  # stop_loss / take_profit / entry / unknown
    created_at: Optional[str] = None

class AccountInfo(BaseModel):
    """账户信息"""
    total_balance: float
    available_balance: float
    used_margin: float = 0.0
    margin_ratio: float = 0.0
    daily_pnl: float = 0.0

class TradeRecord(BaseModel):
    """交易记录"""
    timestamp: str
    symbol: str
    side: str
    action: str
    price: float
    quantity: float
    pnl: Optional[float] = None
    reason: str = ""
    confidence: int = 0


# ============================================================
# Claude决策相关
# ============================================================

class KeyLevels(BaseModel):
    """关键价位"""
    resistance_1: Optional[float] = None
    resistance_2: Optional[float] = None
    support_1: Optional[float] = None
    support_2: Optional[float] = None

class Analysis(BaseModel):
    """Claude分析结果"""
    h4_status: str = Field(alias="4h_status", default="")
    h1_status: str = Field(alias="1h_status", default="")
    m15_status: str = Field(alias="15m_status", default="")
    resonance: bool = False
    resonance_direction: str = "none"
    market_type: str = "transitioning"
    direction: str = "neutral"
    confidence: int = Field(ge=0, le=5, default=0)
    key_levels: KeyLevels = Field(default_factory=KeyLevels)
    reasoning: str = ""

    model_config = {"populate_by_name": True}

class Action(BaseModel):
    """单个操作指令"""
    action: str
    side: Optional[str] = None
    price: Optional[float] = None
    quantity_pct: Optional[float] = None
    order_type: Optional[str] = "market"
    stop_loss_price: Optional[float] = None
    take_profit_price: Optional[float] = None
    take_profit_2_price: Optional[float] = None
    cancel_order_id: Optional[str] = None
    new_stop_price: Optional[float] = None
    new_tp_price: Optional[float] = None
    reason: str = ""

    @field_validator("action")
    @classmethod
    def validate_action_type(cls, v):
        valid = [e.value for e in ActionType]
        if v not in valid:
            raise ValueError(f"Invalid action: {v}, must be one of {valid}")
        return v

class Decision(BaseModel):
    """Claude完整决策"""
    analysis: Analysis
    actions: list[Action] = Field(default_factory=list)
    risk_warnings: list[str] = Field(default_factory=list)


# ============================================================
# 发送给Claude的完整状态
# ============================================================

class MarketState(BaseModel):
    """发送给Claude的完整市场状态"""
    timestamp: str
    trigger: str  # 15m / 1h / 4h
    symbol: str
    current_price: float
    market_data: dict[str, dict]  # 各周期指标数据
    positions: list[dict] = Field(default_factory=list)
    open_orders: list[dict] = Field(default_factory=list)
    account: dict = Field(default_factory=dict)
    recent_trades: list[dict] = Field(default_factory=list)
    funding_rate: Optional[float] = None
    risk_state: str = "normal"
    state_issues: list[str] = Field(default_factory=list)
