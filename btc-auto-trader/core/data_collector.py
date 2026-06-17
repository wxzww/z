"""
数据采集模块 - 从Binance拉取K线数据
"""
import asyncio
import time
import hmac
import hashlib
from urllib.parse import urlencode
import requests as http_requests
import pandas as pd
from datetime import datetime, timezone
from binance.client import Client
from binance.exceptions import BinanceAPIException
from utils.logger import get_logger
from utils.indicators import calculate_all_indicators, extract_timeframe_indicators
from utils.config import AppConfig

logger = get_logger("btc_trader.data")

# Binance K线周期映射
INTERVAL_MAP = {
    "15m": Client.KLINE_INTERVAL_15MINUTE,
    "1h": Client.KLINE_INTERVAL_1HOUR,
    "4h": Client.KLINE_INTERVAL_4HOUR,
}


class DataCollector:
    """
    数据采集+指标计算+缓存
    """

    def __init__(self, config: AppConfig):
        self.config = config
        self.symbol = config.binance.symbol

        # Binance客户端
        if config.binance.testnet:
            self.client = Client(
                config.binance.api_key,
                config.binance.api_secret,
                testnet=True
            )
        else:
            self.client = Client(
                config.binance.api_key,
                config.binance.api_secret
            )

        # 数据缓存：各周期的DataFrame（含指标）
        self._cache: dict[str, pd.DataFrame] = {}
        self._cache_time: dict[str, datetime] = {}

    def fetch_klines(self, timeframe: str, limit: int = None) -> pd.DataFrame:
        """
        从Binance拉取K线数据并计算指标
        
        Returns:
            包含OHLCV + EMA/MACD/RSI指标的DataFrame
        """
        if limit is None:
            limit = self.config.strategy.kline_limit

        interval = INTERVAL_MAP.get(timeframe)
        if not interval:
            raise ValueError(f"Unsupported timeframe: {timeframe}")

        try:
            raw = self.client.futures_klines(
                symbol=self.symbol,
                interval=interval,
                limit=limit
            )
        except BinanceAPIException as e:
            logger.error(f"Binance K线拉取失败 [{timeframe}]: {e}")
            raise
        except Exception as e:
            logger.error(f"网络错误 [{timeframe}]: {e}")
            raise

        if not raw:
            logger.warning(f"K线数据为空 [{timeframe}]")
            return None

        # 解析为DataFrame
        df = pd.DataFrame(raw, columns=[
            "open_time", "open", "high", "low", "close", "volume",
            "close_time", "quote_volume", "trades", "taker_buy_base",
            "taker_buy_quote", "ignore"
        ])

        # 类型转换
        for col in ["open", "high", "low", "close", "volume"]:
            df[col] = df[col].astype(float)
        df["open_time"] = df["open_time"].astype(int)
        df["close_time"] = df["close_time"].astype(int)

        # 数据校验
        if not self._validate_klines(df, timeframe):
            logger.warning(f"K线数据校验不通过 [{timeframe}]，使用缓存")
            return self._cache.get(timeframe)

        # 计算技术指标
        df = calculate_all_indicators(
            df,
            ema_fast=self.config.strategy.ema_fast,
            ema_slow=self.config.strategy.ema_slow,
            macd_fast=self.config.strategy.macd_fast,
            macd_slow=self.config.strategy.macd_slow,
            macd_signal=self.config.strategy.macd_signal,
            rsi_period=self.config.strategy.rsi_period,
        )

        # 更新缓存
        self._cache[timeframe] = df
        self._cache_time[timeframe] = datetime.now(timezone.utc)

        logger.info(f"K线数据更新 [{timeframe}]: {len(df)}根, 最新价={df.iloc[-1]['close']:.2f}")
        return df

    def fetch_and_extract(self, timeframe: str, n_candles: int = 10) -> dict:
        """拉取K线 + 计算指标 + 提取为标准格式dict"""
        df = self.fetch_klines(timeframe)
        if df is None:
            return None
        return extract_timeframe_indicators(df, timeframe, n_candles)

    def get_cached_indicators(self, timeframe: str, n_candles: int = 10) -> dict:
        """获取缓存的指标数据（不重新拉取）"""
        df = self._cache.get(timeframe)
        if df is None:
            logger.warning(f"缓存中无 [{timeframe}] 数据，执行拉取")
            return self.fetch_and_extract(timeframe, n_candles)
        return extract_timeframe_indicators(df, timeframe, n_candles)

    def get_current_price(self) -> float:
        """获取当前价格"""
        try:
            ticker = self.client.futures_symbol_ticker(symbol=self.symbol)
            return float(ticker["price"])
        except Exception as e:
            logger.error(f"获取当前价格失败: {e}")
            # 从缓存中取最新收盘价
            for tf in ["15m", "1h", "4h"]:
                df = self._cache.get(tf)
                if df is not None and len(df) > 0:
                    return float(df.iloc[-1]["close"])
            raise

    def get_funding_rate(self) -> float:
        """获取当前资金费率"""
        try:
            info = self.client.futures_funding_rate(symbol=self.symbol, limit=1)
            if info:
                return float(info[-1]["fundingRate"])
        except Exception as e:
            logger.warning(f"获取资金费率失败: {e}")
        return 0.0

    def _validate_klines(self, df: pd.DataFrame, timeframe: str) -> bool:
        """校验K线数据质量"""
        issues = []

        # 数量
        if len(df) < 60:
            issues.append(f"K线数量不足: {len(df)} < 60")

        # 价格合理性
        if (df["close"] <= 0).any():
            issues.append("存在非正价格")

        if (df["high"] < df["low"]).any():
            issues.append("存在high < low")

        # 成交量（最后一根可能未收盘，跳过）
        if len(df) > 1 and (df["volume"].iloc[:-1] <= 0).any():
            issues.append("存在零成交量K线")

        if issues:
            for issue in issues:
                logger.warning(f"K线校验 [{timeframe}]: {issue}")
            # 只有致命问题才返回False
            if len(df) < 60 or (df["close"] <= 0).any():
                return False

        return True

    # ============================================================
    # 账户和持仓相关（也放在这里，统一Binance API调用）
    # ============================================================

    def get_account_info(self) -> dict:
        """获取账户信息"""
        try:
            account = self.client.futures_account()
            return {
                "total_balance": float(account["totalWalletBalance"]),
                "available_balance": float(account["availableBalance"]),
                "used_margin": float(account["totalPositionInitialMargin"]),
                "margin_ratio": float(account["totalMaintMargin"]) / max(float(account["totalMarginBalance"]), 0.01),
            }
        except Exception as e:
            logger.error(f"获取账户信息失败: {e}")
            raise

    def get_positions(self) -> list[dict]:
        """获取当前持仓"""
        try:
            positions = self.client.futures_position_information(symbol=self.symbol)
            result = []
            for pos in positions:
                qty = float(pos["positionAmt"])
                if qty == 0:
                    continue
                entry = float(pos["entryPrice"])
                unrealized = float(pos["unRealizedProfit"])
                margin = abs(qty) * entry / int(pos["leverage"])
                result.append({
                    "symbol": pos["symbol"],
                    "side": "long" if qty > 0 else "short",
                    "entry_price": entry,
                    "quantity": abs(qty),
                    "unrealized_pnl": unrealized,
                    "unrealized_pnl_pct": (unrealized / margin * 100) if margin > 0 else 0,
                    "leverage": int(pos["leverage"]),
                    "margin": margin,
                })
            return result
        except Exception as e:
            logger.error(f"获取持仓失败: {e}")
            raise

    def get_open_orders(self) -> list[dict]:
        """获取所有未成交挂单 (包括Algo条件单)"""
        result = []

        # 1. 获取普通挂单
        try:
            orders = self.client.futures_get_open_orders(symbol=self.symbol)
            for o in orders:
                purpose = self._classify_order_purpose(o)
                result.append({
                    "order_id": str(o["orderId"]),
                    "symbol": o["symbol"],
                    "side": o["side"].lower(),
                    "order_type": o["type"],
                    "price": float(o["price"]) if float(o["price"]) > 0 else None,
                    "stop_price": float(o["stopPrice"]) if float(o["stopPrice"]) > 0 else None,
                    "quantity": float(o["origQty"]),
                    "status": o["status"],
                    "purpose": purpose,
                    "is_algo": False,
                    "created_at": datetime.fromtimestamp(o["time"] / 1000, tz=timezone.utc).isoformat(),
                })
        except Exception as e:
            logger.error(f"获取普通挂单失败: {e}")

        # 2. 获取Algo条件单 (止损/止盈)
        try:
            algo_orders = self._get_open_algo_orders()
            for o in algo_orders:
                order_type = o.get("type", "")
                purpose = "stop_loss" if order_type == "STOP_MARKET" else (
                    "take_profit" if order_type == "TAKE_PROFIT_MARKET" else "unknown"
                )
                trigger_price = float(o.get("triggerPrice", 0) or o.get("stopPrice", 0))
                result.append({
                    "order_id": str(o.get("algoId", "")),
                    "symbol": o.get("symbol", self.symbol),
                    "side": o.get("side", "").lower(),
                    "order_type": order_type,
                    "price": None,
                    "stop_price": trigger_price if trigger_price > 0 else None,
                    "quantity": float(o.get("origQty", 0) or o.get("quantity", 0)),
                    "status": o.get("algoStatus", "NEW"),
                    "purpose": purpose,
                    "is_algo": True,
                    "created_at": datetime.fromtimestamp(
                        o.get("bookTime", o.get("updateTime", 0)) / 1000, tz=timezone.utc
                    ).isoformat() if o.get("bookTime") or o.get("updateTime") else None,
                })
        except Exception as e:
            logger.warning(f"获取Algo条件单失败: {e}")

        return result

    def _get_open_algo_orders(self) -> list:
        """获取所有活跃的Algo条件单"""
        try:
            params = {"symbol": self.symbol, "timestamp": int(time.time() * 1000)}
            query_string = urlencode(params)
            signature = hmac.new(
                self.client.API_SECRET.encode("utf-8"),
                query_string.encode("utf-8"),
                hashlib.sha256,
            ).hexdigest()
            params["signature"] = signature

            url = "https://fapi.binance.com/fapi/v1/openAlgoOrders"
            headers = {"X-MBX-APIKEY": self.client.API_KEY}
            resp = http_requests.get(url, params=params, headers=headers, timeout=10)

            if resp.status_code == 200:
                data = resp.json()
                return data.get("orders", []) if isinstance(data, dict) else data
            else:
                logger.warning(f"查询Algo挂单HTTP {resp.status_code}: {resp.text}")
                return []
        except Exception as e:
            logger.warning(f"查询Algo挂单异常: {e}")
            return []

    def get_recent_trades(self, limit: int = 10) -> list[dict]:
        """获取最近的成交记录"""
        try:
            trades = self.client.futures_account_trades(symbol=self.symbol, limit=limit)
            result = []
            for t in trades:
                result.append({
                    "time": datetime.fromtimestamp(t["time"] / 1000, tz=timezone.utc).isoformat(),
                    "side": t["side"].lower(),
                    "price": float(t["price"]),
                    "quantity": float(t["qty"]),
                    "pnl": float(t["realizedPnl"]),
                    "commission": float(t["commission"]),
                })
            return result
        except Exception as e:
            logger.warning(f"获取成交记录失败: {e}")
            return []

    def _classify_order_purpose(self, order: dict) -> str:
        """根据订单类型推断用途"""
        otype = order["type"]
        if otype == "STOP_MARKET":
            return "stop_loss"
        elif otype == "TAKE_PROFIT_MARKET":
            return "take_profit"
        elif otype == "LIMIT":
            # 限价单可能是入场单也可能是止盈单
            # 简单判断：如果有持仓且方向相反，则是止盈/平仓
            return "limit_entry_or_tp"
        return "unknown"

    # ============================================================
    # 交易所设置
    # ============================================================

    def setup_exchange(self):
        """初始化交易所设置（杠杆、保证金模式）"""
        try:
            # 设置杠杆
            self.client.futures_change_leverage(
                symbol=self.symbol,
                leverage=self.config.binance.leverage
            )
            logger.info(f"杠杆设置为 {self.config.binance.leverage}x")

            # 设置保证金模式
            try:
                self.client.futures_change_margin_type(
                    symbol=self.symbol,
                    marginType=self.config.binance.margin_type
                )
                logger.info(f"保证金模式设置为 {self.config.binance.margin_type}")
            except BinanceAPIException as e:
                if "No need to change margin type" in str(e):
                    logger.info(f"保证金模式已是 {self.config.binance.margin_type}")
                else:
                    raise

        except Exception as e:
            logger.error(f"交易所设置失败: {e}")
            raise

    def ping(self) -> bool:
        """测试连接"""
        try:
            self.client.futures_ping()
            return True
        except Exception:
            return False
