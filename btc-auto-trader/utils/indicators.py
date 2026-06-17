"""
技术指标计算模块
在本地计算EMA/MACD/RSI，不依赖第三方指标服务
"""
import pandas as pd
import numpy as np
from models.schemas import EmaRelationship, EmaDirection


def calculate_ema(series: pd.Series, period: int) -> pd.Series:
    """计算EMA"""
    return series.ewm(span=period, adjust=False).mean()


def calculate_macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9):
    """
    计算MACD
    返回：DIFF, DEA(信号线), MACD柱(DIFF-DEA)
    """
    ema_fast = calculate_ema(close, fast)
    ema_slow = calculate_ema(close, slow)
    diff = ema_fast - ema_slow
    dea = calculate_ema(diff, signal)
    hist = 2 * (diff - dea)  # 有些平台MACD柱=DIFF-DEA，有些×2，这里×2与国内软件一致
    return diff, dea, hist


def calculate_rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """计算RSI"""
    delta = close.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = (-delta).where(delta < 0, 0.0)
    avg_gain = gain.ewm(alpha=1/period, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1/period, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    return rsi.fillna(50)


def determine_direction(series: pd.Series, lookback: int = 3) -> EmaDirection:
    """根据最近N个值判断方向"""
    if len(series) < lookback + 1:
        return EmaDirection.FLAT
    recent = series.iloc[-lookback:]
    first = recent.iloc[0]
    last = recent.iloc[-1]
    diff_pct = (last - first) / abs(first) if first != 0 else 0
    if diff_pct > 0.0001:
        return EmaDirection.UP
    elif diff_pct < -0.0001:
        return EmaDirection.DOWN
    return EmaDirection.FLAT


def determine_ema_relationship(ema15: float, ema60: float, gap_threshold_pct: float = 0.001) -> EmaRelationship:
    """判断EMA关系：金叉/死叉/麻花"""
    if ema60 == 0:
        return EmaRelationship.TANGLED
    gap_pct = abs(ema15 - ema60) / ema60
    if gap_pct < gap_threshold_pct:
        return EmaRelationship.TANGLED
    if ema15 > ema60:
        return EmaRelationship.GOLDEN_CROSS
    return EmaRelationship.DEATH_CROSS


def calculate_all_indicators(
    df: pd.DataFrame,
    ema_fast: int = 15,
    ema_slow: int = 60,
    macd_fast: int = 12,
    macd_slow: int = 26,
    macd_signal: int = 9,
    rsi_period: int = 14
) -> pd.DataFrame:
    """
    计算所有技术指标
    
    输入：包含 open, high, low, close, volume 列的DataFrame
    输出：附加指标列的DataFrame
    """
    df = df.copy()

    # EMA
    df["ema15"] = calculate_ema(df["close"], ema_fast)
    df["ema60"] = calculate_ema(df["close"], ema_slow)

    # MACD
    diff, dea, hist = calculate_macd(df["close"], macd_fast, macd_slow, macd_signal)
    df["macd_diff"] = diff
    df["macd_dea"] = dea
    df["macd_hist"] = hist

    # RSI
    df["rsi"] = calculate_rsi(df["close"], rsi_period)

    return df


def extract_timeframe_indicators(df: pd.DataFrame, timeframe: str, n_candles: int = 10) -> dict:
    """
    从计算好的DataFrame中提取最新指标值，组装成发给Claude的格式
    """
    if df is None or len(df) < 60:
        return None

    latest = df.iloc[-1]
    prev = df.iloc[-2] if len(df) > 1 else latest

    ema15 = float(latest["ema15"])
    ema60 = float(latest["ema60"])
    ema_gap = ema15 - ema60

    # EMA方向
    ema15_dir = determine_direction(df["ema15"])
    ema60_dir = determine_direction(df["ema60"])

    # EMA关系
    ema_rel = determine_ema_relationship(ema15, ema60)

    # MACD柱方向
    hist = float(latest["macd_hist"])
    hist_prev = float(prev["macd_hist"])

    # 近30根K线的高低点
    recent_30 = df.tail(30)
    recent_high = float(recent_30["high"].max())
    recent_low = float(recent_30["low"].min())

    # 最近N根K线
    last_candles = []
    for _, row in df.tail(n_candles).iterrows():
        last_candles.append({
            "open": round(float(row["open"]), 2),
            "high": round(float(row["high"]), 2),
            "low": round(float(row["low"]), 2),
            "close": round(float(row["close"]), 2),
            "volume": round(float(row["volume"]), 2),
        })

    return {
        "timeframe": timeframe,
        "ema15": round(ema15, 2),
        "ema60": round(ema60, 2),
        "ema15_direction": ema15_dir.value,
        "ema60_direction": ema60_dir.value,
        "ema_relationship": ema_rel.value,
        "ema_gap": round(ema_gap, 2),
        "macd_diff": round(float(latest["macd_diff"]), 2),
        "macd_dea": round(float(latest["macd_dea"]), 2),
        "macd_hist": round(hist, 2),
        "macd_hist_prev": round(hist_prev, 2),
        "macd_hist_direction": "expanding" if abs(hist) > abs(hist_prev) else "contracting",
        "rsi": round(float(latest["rsi"]), 2),
        "current_price": round(float(latest["close"]), 2),
        "recent_high": round(recent_high, 2),
        "recent_low": round(recent_low, 2),
        "last_candles": last_candles,
    }
