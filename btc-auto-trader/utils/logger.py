"""
日志配置
"""
import logging
import os
from logging.handlers import RotatingFileHandler


def setup_logger(level: str = "INFO", log_file: str = "logs/trading.log") -> logging.Logger:
    """配置全局日志"""
    os.makedirs(os.path.dirname(log_file), exist_ok=True)

    logger = logging.getLogger("btc_trader")
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))

    # 防止重复添加handler
    if logger.handlers:
        return logger

    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)s.%(funcName)s:%(lineno)d | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )

    # 控制台
    ch = logging.StreamHandler()
    ch.setFormatter(fmt)
    logger.addHandler(ch)

    # 文件（100MB轮转，保留10个）
    fh = RotatingFileHandler(log_file, maxBytes=100*1024*1024, backupCount=10, encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    return logger


def get_logger(name: str = "btc_trader") -> logging.Logger:
    return logging.getLogger(name)
