"""数据引擎模块：负责 SQLite 行情数据存储与 baostock 增量同步。"""

import sqlite3
from pathlib import Path

import pandas as pd

from sequoia_x.core.config import Settings
from sequoia_x.core.logger import get_logger

logger = get_logger(__name__)


_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS stock_daily (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol   TEXT    NOT NULL,
    date     TEXT    NOT NULL,
    open     REAL,
    high     REAL,
    low      REAL,
    close    REAL,
    volume   REAL,
    turnover REAL,
    UNIQUE (symbol, date)
);
"""

_CREATE_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_symbol_date ON stock_daily (symbol, date);
"""


def _bs_login_with_retry(max_tries: int = 3) -> bool:
    """带退避重试的 baostock 登录，避免瞬时网络抖动导致整批失败。"""
    import time
    import baostock as bs

    for attempt in range(max_tries):
        lg = bs.login()
        if lg.error_code == "0":
            return True
        wait = 2 ** attempt
        logger.warning(
            f"baostock 登录失败({lg.error_code}): {lg.error_msg}，{wait}s 后重试"
        )
        time.sleep(wait)
    return False


def _bs_fetch_one(symbol: str, bs_code: str, start: str, end: str) -> list:
    """单进程：拉取单只股票的 baostock 后复权日线。调用前需已 login。"""
    import baostock as bs

    rs = bs.query_history_k_data_plus(
        bs_code,
        "date,open,high,low,close,volume,amount",
        start_date=start,
        end_date=end,
        frequency="d",
        adjustflag="1",  # 后复权
    )
    if rs.error_code != "0":
        return []
    results = []
    while rs.next():
        results.append([symbol] + rs.get_row_data())
    return results


class DataEngine:
    """行情数据引擎，负责 SQLite 存储和 baostock 数据同步。"""

    def __init__(self, settings: Settings) -> None:
        self.db_path: str = settings.db_path
        self.start_date: str = settings.start_date
        self._init_db()

    def _init_db(self) -> None:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(_CREATE_TABLE_SQL)
            conn.execute(_CREATE_INDEX_SQL)
            conn.commit()
        logger.info(f"数据库初始化完成：{self.db_path}")

    def _get_last_date(self, symbol: str) -> str | None:
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT MAX(date) FROM stock_daily WHERE symbol = ?",
                (symbol,),
            ).fetchone()
        return row[0] if row and row[0] else None

    def get_ohlcv(self, symbol: str) -> pd.DataFrame:
        with sqlite3.connect(self.db_path) as conn:
            df = pd.read_sql(
                "SELECT * FROM stock_daily WHERE symbol = ? ORDER BY date",
                conn,
                params=(symbol,),
            )
        return df

    @staticmethod
    def _to_baostock_code(symbol: str) -> str:
        """将纯数字代码转为 baostock 格式：6/9开头 -> sh，其余 -> sz。"""
        prefix = "sh" if symbol.startswith(("6", "9")) else "sz"
        return f"{prefix}.{symbol}"

    # ── 数据同步 ──

    def sync_today_bulk(self) -> int:
        """增量同步：优先 baostock，失败时由 akshare 兜底（均为后复权）。"""
        from datetime import date, timedelta

        today_str = date.today().strftime("%Y-%m-%d")

        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT symbol, MAX(date) FROM stock_daily GROUP BY symbol"
            ).fetchall()

        if not rows:
            logger.warning("本地无股票数据，请先执行 --backfill")
            return 0

        tasks = []
        for symbol, last_date in rows:
            if last_date and last_date >= today_str:
                continue
            start = today_str
            if last_date:
                start = (date.fromisoformat(last_date) + timedelta(days=1)).strftime("%Y-%m-%d")
            tasks.append((symbol, self._to_baostock_code(symbol), start, today_str))

        if not tasks:
            logger.info("所有股票已是最新，无需更新")
            return 0

        logger.info(f"需要更新 {len(tasks)} 只股票，优先 baostock ...")

        # 1) baostock（多进程 + 登录重试）
        bs_rows = self._sync_via_baostock(tasks)
        fetched = {r[0] for r in bs_rows}
        missing = [t for t in tasks if t[0] not in fetched]

        # 2) akshare 兜底（baostock 被限流/拉黑时）
        if missing:
            if len(missing) == len(tasks):
                logger.warning("baostock 全量不可用（可能被限流/拉黑），改用 akshare 兜底")
            else:
                logger.warning(f"baostock 漏抓 {len(missing)} 只，akshare 兜底")
            ak_rows = self._sync_via_akshare(missing)
            bs_rows.extend(ak_rows)

        if not bs_rows:
            logger.info("无新数据（可能非交易日或数据源均不可用）")
            return 0

        df = pd.DataFrame(
            bs_rows,
            columns=["symbol", "date", "open", "high", "low", "close", "volume", "turnover"],
        )
        for col in ["open", "high", "low", "close", "volume", "turnover"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        df = df.dropna(subset=["close"])
        df = df[df["volume"] > 0]

        count = len(df)
        if count == 0:
            logger.info("无新数据（可能非交易日）")
            return 0

        with sqlite3.connect(self.db_path) as conn:
            for d in df["date"].unique().tolist():
                conn.execute("DELETE FROM stock_daily WHERE date = ?", (d,))
            df.to_sql("stock_daily", conn, if_exists="append", index=False, method="multi", chunksize=500)
            conn.commit()

        logger.info(
            f"sync_today_bulk: 写入 {count} 条（baostock {len(fetched)} / akshare {count - len(fetched)}）"
        )
        return count

    def _sync_via_baostock(self, tasks: list) -> list:
        """单进程顺序通过 baostock 拉取增量数据。

        baostock 当前对本环境限流/拉黑，多数情况下返回空，由 akshare 兜底。
        """
        import baostock as bs

        if not _bs_login_with_retry():
            return []
        all_rows: list = []
        try:
            for symbol, bs_code, start, end in tasks:
                all_rows.extend(_bs_fetch_one(symbol, bs_code, start, end))
        finally:
            bs.logout()
        return all_rows

    def _sync_via_akshare(self, tasks: list) -> list:
        """兜底：单进程顺序用 akshare(sina 源) 拉取后复权日线。

        tasks: [(symbol, bs_code, start, end), ...]，start/end 为 YYYY-MM-DD。
        返回与 baostock 同构的行：[symbol, date, open, high, low, close, volume, turnover]。
        eastmoney 源(push2his)在此环境被代理拦截，故统一走 sina。
        """
        import akshare as ak

        results: list = []
        for symbol, _, start, end in tasks:
            sina_code = ("sh" if symbol.startswith(("6", "9")) else "sz") + symbol
            try:
                df = ak.stock_zh_a_daily(
                    symbol=sina_code,
                    start_date=start.replace("-", ""),
                    end_date=end.replace("-", ""),
                    adjust="hfq",  # 后复权，与 baostock adjustflag=1 同口径
                )
            except Exception as exc:
                logger.warning(f"[akshare][{symbol}] 拉取失败: {exc}")
                continue
            if df is None or len(df) == 0:
                continue
            for _, row in df.iterrows():
                results.append([
                    symbol,
                    str(row["date"]),
                    float(row["open"]),
                    float(row["high"]),
                    float(row["low"]),
                    float(row["close"]),
                    float(row["volume"]),
                    float(row["amount"]),  # 成交额 → DB 的 turnover 列
                ])
        return results

    def backfill(self, symbols: list[str]) -> None:
        """单进程顺序通过 akshare(sina 源, 后复权) 批量回填历史日 K 线。

        容错机制：
        - 单只失败自动重试 3 次，间隔递增（2s/4s/8s）
        - 单进程顺序执行（避免并发触发 V8/代理层崩溃），DB 写入串行
        - last_date 为空时拉全量（用于重新基准化到统一口径）
        说明：baostock 当前对本环境限流/拉黑，统一改用 sina 源，保证与增量同步口径一致。
        """
        import time
        from datetime import date, timedelta

        import akshare as ak

        today_str = date.today().strftime("%Y-%m-%d")
        max_retries = 3
        success = skipped = failed = 0

        for i, symbol in enumerate(symbols):
            last_date = self._get_last_date(symbol)
            if last_date and last_date >= today_str:
                skipped += 1
            else:
                start = last_date or self.start_date
                if last_date:
                    start = (date.fromisoformat(last_date) + timedelta(days=1)).strftime("%Y-%m-%d")

                sina_code = ("sh" if symbol.startswith(("6", "9")) else "sz") + symbol
                df = None
                for attempt in range(max_retries):
                    try:
                        df = ak.stock_zh_a_daily(
                            symbol=sina_code,
                            start_date=start.replace("-", ""),
                            end_date=today_str.replace("-", ""),
                            adjust="hfq",
                        )
                        if df is not None and len(df) > 0:
                            break
                        df = None
                    except Exception as exc:
                        if attempt < max_retries - 1:
                            time.sleep(2 ** (attempt + 1))
                        else:
                            logger.warning(f"[{symbol}] {max_retries}次重试均失败，跳过")

                if df is None:
                    failed += 1
                    continue
                if len(df) == 0:
                    skipped += 1
                    continue

                rows = [
                    [symbol, str(r["date"]), float(r["open"]), float(r["high"]),
                     float(r["low"]), float(r["close"]), float(r["volume"]), float(r["amount"])]
                    for _, r in df.iterrows()
                ]
                out = pd.DataFrame(
                    rows, columns=["symbol", "date", "open", "high", "low", "close", "volume", "turnover"]
                )
                out["volume"] = pd.to_numeric(out["volume"], errors="coerce")
                out["turnover"] = pd.to_numeric(out["turnover"], errors="coerce")
                out = out.dropna(subset=["close"])
                out = out[out["volume"] > 0]
                if out.empty:
                    skipped += 1
                    continue

                try:
                    with sqlite3.connect(self.db_path) as conn:
                        for d in out["date"].unique().tolist():
                            conn.execute(
                                "DELETE FROM stock_daily WHERE symbol=? AND date=?", (symbol, d)
                            )
                        out.to_sql(
                            "stock_daily", conn, if_exists="append",
                            index=False, method="multi", chunksize=500,
                        )
                except sqlite3.IntegrityError:
                    pass
                success += 1

            if (i + 1) % 200 == 0:
                logger.info(
                    f"已处理 {i + 1}/{len(symbols)}，"
                    f"成功 {success} 跳过 {skipped} 失败 {failed}"
                )

        logger.info(
            f"回填完成 — 成功: {success} | 跳过: {skipped} | 失败: {failed}"
        )

    # ── 股票列表 ──

    def get_all_symbols(self) -> list[str]:
        """获取全市场 A 股代码列表（优先 baostock，失败则 akshare/sina 兜底）。"""
        import baostock as bs

        try:
            lg = bs.login()
            if lg.error_code == "0":
                try:
                    rs = bs.query_stock_basic(code_name="", code="")
                    symbols: list[str] = []
                    while rs.next():
                        row = rs.get_row_data()
                        code = row[0]           # "sh.600000" or "sz.000001"
                        status = row[4]         # "1" = 上市
                        stock_type = row[5]     # "1" = 股票
                        if status == "1" and stock_type == "1":
                            symbols.append(code.split(".")[1])  # 提取纯数字代码
                    if symbols:
                        logger.info(f"baostock 获取股票列表完成，共 {len(symbols)} 只")
                        return symbols
                except Exception as e:
                    logger.warning(f"baostock 获取股票列表失败: {e}")
                finally:
                    bs.logout()
        except Exception:
            pass

        # 兜底：akshare(sina) 提供全市场列表
        try:
            import akshare as ak

            df = ak.stock_info_a_code_name()
            symbols = [str(c) for c in df["code"].tolist() if str(c).isdigit()]
            logger.info(f"akshare 获取股票列表完成，共 {len(symbols)} 只")
            return symbols
        except Exception as e:
            logger.error(f"akshare 获取股票列表失败: {e}")
            return []

    def get_local_symbols(self) -> list[str]:
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT DISTINCT symbol FROM stock_daily"
            ).fetchall()
        return [row[0] for row in rows]
