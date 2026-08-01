"""
Sequoia-X 信号追溯脚本（待接入 weekly_report workflow）。

用途：查 N 个交易日前飞书知识库的选股结果，跟踪信号发出后的实际涨跌。
"""
from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from pathlib import Path

# TODO: 实际使用时接入飞书知识库读取，或从本地 signal_log 文件读取
# 当前为骨架，待 Phase 2 启用


def trace_signals(
    db_path: str,
    lookback_days: int = 5,
    hold_days: list[int] | None = None,
) -> None:
    """
    追溯信号发出后 N 日的表现。

    Args:
        db_path: SQLite 数据库路径
        lookback_days: 往回看几个交易日的信号
        hold_days: 统计哪些持有期的收益，默认 [5, 10, 20]
    """
    hold_days = hold_days or [5, 10, 20]

    if not Path(db_path).exists():
        print(f"DB 不存在: {db_path}")
        return

    conn = sqlite3.connect(db_path)

    # 获取交易日列表
    trading_dates = [
        r[0] for r in conn.execute(
            "SELECT DISTINCT date FROM stock_daily ORDER BY date DESC LIMIT 250"
        ).fetchall()
    ]

    latest_date = trading_dates[0]
    signal_date_str = trading_dates[min(lookback_days - 1, len(trading_dates) - 1)]

    print(f"最新数据日: {latest_date}")
    print(f"信号发出日: {signal_date_str}（{lookback_days} 个交易日前）")
    print()

    # TODO: 从飞书知识库或 signal_log.json 读取当天实际发出信号的股票列表
    # 现在用占位：假数据演示格式
    signals: list[dict] = []
    print("⚠️  信号追溯脚本已就绪，待接入飞书知识库读取 + 每日 signal_log 生成。")
    print("   当前为骨架，Phase 2 启用。")

    conn.close()


if __name__ == "__main__":
    trace_signals(
        db_path="data/sequoia_v2.db",
        lookback_days=5,
        hold_days=[5, 10, 20],
    )
