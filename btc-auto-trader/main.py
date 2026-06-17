"""
BTC自动化交易系统 - 主入口

使用方法:
    python main.py                     # 正常启动（APScheduler 定时调度）
    python main.py --trigger 4h        # 单次运行指定周期后退出（给 GitHub Actions 用）
    python main.py --trigger 1h
    python main.py --test              # 等同于 --trigger 1h
"""
import sys
import signal
import argparse
from utils.config import load_config
from utils.logger import setup_logger, get_logger
from core.pipeline import TradingPipeline

logger = get_logger("btc_trader.main")


def create_scheduler(pipeline: TradingPipeline):
    from apscheduler.schedulers.blocking import BlockingScheduler
    from apscheduler.triggers.cron import CronTrigger

    scheduler = BlockingScheduler(timezone="UTC")
    scheduler.add_job(
        pipeline.run,
        CronTrigger(hour="0,4,8,12,16,20", minute=0, second=3),
        args=["4h"], id="analysis_4h", name="4H分析",
        misfire_grace_time=60, max_instances=1,
    )
    scheduler.add_job(
        pipeline.run,
        CronTrigger(minute=0, second=8),
        args=["1h"], id="analysis_1h", name="1H分析",
        misfire_grace_time=60, max_instances=1,
    )
    scheduler.add_job(
        _daily_report, CronTrigger(hour=0, minute=1),
        args=[pipeline], id="daily_report", name="每日报告",
    )
    scheduler.add_job(
        _health_check, "interval", minutes=5,
        args=[pipeline], id="health_check", name="健康检查",
    )
    return scheduler


def _daily_report(pipeline):
    try:
        stats = pipeline.db.get_daily_stats()
        account = pipeline.data_collector.client.futures_account_balance()
        balance = sum(float(a["balance"]) for a in account if a["asset"] == "USDT")
        pipeline.notifier.send_daily_report(stats, balance)
    except Exception as e:
        logger.error(f"生成日报失败: {e}")


def _health_check(pipeline):
    try:
        pipeline.data_collector.client.futures_ping()
    except Exception as e:
        logger.error(f"健康检查异常: {e}")


def main():
    parser = argparse.ArgumentParser(description="BTC自动化交易系统")
    parser.add_argument("--config", default="config.yaml", help="配置文件路径")
    parser.add_argument("--trigger", choices=["4h", "1h"], help="单次运行指定周期后退出")
    parser.add_argument("--test", action="store_true", help="等同于 --trigger 1h")
    args = parser.parse_args()

    config = load_config(args.config)
    setup_logger(config.log_level, config.log_file)
    logger.info("=" * 60)
    logger.info("BTC自动化交易系统启动")
    logger.info(f"模式: {'测试网' if config.binance.testnet else '⚠️ 主网'}")
    logger.info(f"品种: {config.binance.symbol}  杠杆: {config.binance.leverage}x")
    logger.info("=" * 60)

    pipeline = TradingPipeline(config)

    # ---- 单次运行模式（GitHub Actions 用这个）----
    trigger = args.trigger or ("1h" if args.test else None)
    if trigger:
        logger.info(f"单次运行模式: trigger={trigger}")
        pipeline.startup_light()  # 轻量启动：只测连接，不做初始分析
        pipeline.run(trigger)
        logger.info("单次运行完成")
        return

    # ---- 长驻调度模式 ----
    def handle_signal(signum, frame):
        pipeline.shutdown(f"signal_{signum}")
        sys.exit(0)

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    try:
        pipeline.startup()
        scheduler = create_scheduler(pipeline)
        logger.info("调度任务:")
        for job in scheduler.get_jobs():
            logger.info(f"  {job.name}: {job.trigger}")
        scheduler.start()
    except KeyboardInterrupt:
        pipeline.shutdown("keyboard_interrupt")
    except Exception as e:
        logger.critical(f"系统异常: {e}", exc_info=True)
        pipeline.emergency_stop(str(e))
        sys.exit(1)


if __name__ == "__main__":
    main()