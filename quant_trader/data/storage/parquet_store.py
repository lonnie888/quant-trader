"""Parquet-based local storage for OHLCV and funding data.

自 2026-08-28 起支持可选 postgres 数据源（绿联共享 Jesse 数据库）:
- 历史数据统一从 postgres 读取 (candle 表只存 1m, SQL 端聚合到目标周期)
- 本地 parquet 作为兜底 + 实时 K 线缓存 (daemon WS 收盘后写入)
- load() 合并两者: pg 为历史基准, 本地补实时, 保证数据完整
"""
from __future__ import annotations

import logging
import os
import time
from pathlib import Path

import pandas as pd

logger = logging.getLogger(__name__)

try:
    import psycopg2
except ImportError:  # pragma: no cover
    psycopg2 = None

# 周期 -> 毫秒
_MS = {"1m": 60_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000,
       "1h": 3_600_000, "2h": 7_200_000, "4h": 14_400_000, "1d": 86_400_000}

_CACHE_TTL = 600  # pg 聚合结果缓存 10 分钟, 避免 daemon/market_filter 重复全量查询


def _safe(symbol: str) -> str:
    return symbol.replace("/", "_").replace(":", "_")


class ParquetStore:
    def __init__(self, root: str = "./data_store", pg: dict | None = None):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        if pg is None:
            pg = self._pg_from_settings()
        self._pg = pg
        self._pg_conn = None
        self._pg_cache: dict[tuple, tuple[float, pd.DataFrame]] = {}
        self._pg_sym_set: set[str] | None = None

    # ------------------------------------------------------------------
    # postgres 配置
    # ------------------------------------------------------------------
    @staticmethod
    def _pg_from_settings() -> dict | None:
        """从 quant_trader/config/settings.yaml 读取 postgres 节(若存在)."""
        try:
            import yaml
            p = Path(__file__).resolve().parent.parent.parent.parent
            for cand in (
                p / "config" / "settings.yaml",
                Path.cwd() / "config" / "settings.yaml",
                Path.cwd() / "quant_trader" / "config" / "settings.yaml",
            ):
                if cand.exists():
                    with open(cand, "r", encoding="utf-8") as f:
                        cfg = yaml.safe_load(f) or {}
                    pg = (cfg.get("data") or {}).get("postgres")
                    if pg and pg.get("host"):
                        return pg
            # 环境变量兜底
            if os.environ.get("QT_PG_HOST"):
                return {
                    "host": os.environ["QT_PG_HOST"],
                    "port": int(os.environ.get("QT_PG_PORT", "5432")),
                    "dbname": os.environ.get("QT_PG_DB", "jesse_db"),
                    "user": os.environ.get("QT_PG_USER", "jesse_user"),
                    "password": os.environ.get("QT_PG_PASSWORD", ""),
                }
        except Exception as e:  # pragma: no cover
            logger.debug("pg config load failed: %s", e)
        return None

    # ------------------------------------------------------------------
    # symbol 归一化
    # ------------------------------------------------------------------
    @staticmethod
    def _normalize_base(symbol: str) -> str:
        """多种 symbol 格式 -> 基础币名.

        支持: BANK_USDT_USDT / BANK/USDT:USDT / BANKUSDT / BANK-USDT / bankusdt
        """
        s = symbol.upper()
        s = s.replace("_USDT_USDT", "").replace("_USDT", "")
        if "/USDT:USDT" in s:
            s = s.split("/")[0]
        if s.endswith("-USDT"):
            s = s[:-5]
        elif s.endswith("USDT"):
            s = s[:-4]
        return s

    def _pg_symbol(self, symbol: str) -> str:
        return self._normalize_base(symbol) + "-USDT"

    # ------------------------------------------------------------------
    # postgres 读取 (SQL 端聚合)
    # ------------------------------------------------------------------
    def _pg_connect(self):
        if not psycopg2:
            raise RuntimeError("psycopg2 not installed")
        if self._pg_conn is None or self._pg_conn.closed:
            self._pg_conn = psycopg2.connect(
                host=self._pg["host"], port=int(self._pg.get("port", 5432)),
                dbname=self._pg.get("dbname", "jesse_db"),
                user=self._pg.get("user", "jesse_user"),
                password=self._pg.get("password", ""),
                connect_timeout=8,
                # 防 pg 繁忙(如 Vision 批量导入 COPY 锁)时 daemon 永久阻塞:
                # 查询超时抛异常 -> 调用方降级本地缓存, daemon 事件循环不卡死
                options="-c statement_timeout=90000",
            )
        return self._pg_conn

    def _pg_rollback(self):
        """事务失败后 rollback 恢复连接, 否则后续语句报 'transaction aborted'."""
        try:
            if self._pg_conn is not None and not self._pg_conn.closed:
                self._pg_conn.rollback()
        except Exception:
            pass

    def _pg_symbols(self) -> set[str]:
        """pg candle 表存在的全部 symbol (懒加载, 用于快速跳过不存在的币)."""
        if self._pg_sym_set is None:
            if not self._pg:
                self._pg_sym_set = set()
                return self._pg_sym_set
            try:
                conn = self._pg_connect()
                cur = conn.cursor()
                cur.execute("SELECT DISTINCT symbol FROM candle")
                self._pg_sym_set = {r[0] for r in cur.fetchall()}
                cur.close()
            except Exception as e:
                logger.warning("pg symbol list failed: %s", e)
                self._pg_rollback()
                self._pg_sym_set = set()
        return self._pg_sym_set

    def _pg_load(self, symbol: str, timeframe: str) -> pd.DataFrame:
        """从 postgres 读 1m 并按目标周期 SQL 聚合, 返回 DatetimeIndex(UTC) df."""
        if not self._pg or timeframe not in _MS:
            return pd.DataFrame()
        span = _MS[timeframe]
        key = (self._pg_symbol(symbol), timeframe)
        now = time.time()
        cached = self._pg_cache.get(key)
        if cached and now - cached[0] < _CACHE_TTL:
            return cached[1]

        pg_sym = self._pg_symbol(symbol)
        # pg 里不存在的币直接跳过, 不发起聚合查询 (本地 parquet 兜底)
        if pg_sym not in self._pg_symbols():
            return pd.DataFrame()
        sql = (
            "SELECT (timestamp/%s)*%s AS ts, "
            "(array_agg(open ORDER BY timestamp))[1] AS open, "
            "max(high) AS high, min(low) AS low, "
            "(array_agg(close ORDER BY timestamp DESC))[1] AS close, "
            "sum(volume) AS volume "
            "FROM candle WHERE symbol = %s AND timeframe = '1m' "
            "GROUP BY 1 ORDER BY 1"
        )
        try:
            conn = self._pg_connect()
            cur = conn.cursor()
            cur.execute(sql, (span, span, pg_sym))
            rows = cur.fetchall()
            cur.close()
            if not rows:
                # 空结果也缓存, 避免重复全量查询
                self._pg_cache[key] = (now, pd.DataFrame())
                return pd.DataFrame()
            df = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "volume"])
            df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
            df = df.set_index("timestamp")
            df.index = df.index.astype("datetime64[ms, UTC]")
            df = df.astype({"open": float, "high": float, "low": float, "close": float, "volume": float})
            self._pg_cache[key] = (now, df)
            logger.debug("pg load %s %s: %d rows", pg_sym, timeframe, len(df))
            return df
        except Exception as e:
            logger.warning("pg load failed %s/%s: %s", pg_sym, timeframe, e)
            self._pg_rollback()  # 关键: 失败后恢复连接, 否则后续语句全报 transaction aborted
            return pd.DataFrame()

    def _load_local(self, symbol: str, timeframe: str) -> pd.DataFrame:
        path = self._path(symbol, timeframe)
        if not path.exists():
            return pd.DataFrame()
        try:
            return pd.read_parquet(path)
        except Exception as e:
            logger.warning("local load failed %s/%s: %s", symbol, timeframe, e)
            return pd.DataFrame()

    def load_local(self, symbol: str, timeframe: str) -> pd.DataFrame:
        """只读本地 parquet 缓存 (不含 pg). 供 WS 增量更新/新鲜度检查使用."""
        return self._load_local(symbol, timeframe)

    def has_pg(self, symbol: str) -> bool:
        """pg 中是否存在该币 (用于判断历史数据源是否可用)."""
        return self._pg_symbol(symbol) in self._pg_symbols()

    def pull_pg_to_local(self, symbol: str, timeframe: str) -> int:
        """从 pg 拉取全量聚合结果写入本地 parquet 缓存, 返回行数.

        与本地已有缓存合并 (保留本地比 pg 更新的数据, 如预热 REST 拉的最近 K 线),
        避免覆盖丢失实时增量.
        用途: daemon 启动时用 pg 历史初始化本地缓存, 之后 WS 收盘增量更新.
        """
        df = self._pg_load(symbol, timeframe)
        if df.empty:
            return 0
        df_local = self._load_local(symbol, timeframe)
        if not df_local.empty:
            try:
                df = pd.concat([df, df_local])
                df = df[~df.index.duplicated(keep="first")].sort_index()
            except Exception:
                pass
        self.save(symbol, timeframe, df)
        return len(df)

    # ------------------------------------------------------------------
    # 对外接口 (原签名不变, 调用方零改动)
    # ------------------------------------------------------------------
    def _path(self, symbol: str, timeframe: str) -> Path:
        return self.root / _safe(symbol) / f"{timeframe}.parquet"

    def save(self, symbol: str, timeframe: str, df: pd.DataFrame) -> None:
        if df is None or df.empty:
            return
        try:
            path = self._path(symbol, timeframe)
            path.parent.mkdir(parents=True, exist_ok=True)
            # 直接覆盖写入（不合并），提高稳定性
            df.to_parquet(path)
        except Exception as e:
            logger.warning("store save failed %s/%s: %s", symbol, timeframe, e)

    def load(self, symbol: str, timeframe: str) -> pd.DataFrame:
        """优先 pg(历史完整) + 本地(实时缓存) 合并, 按时间去重排序."""
        df_pg = self._pg_load(symbol, timeframe)
        df_local = self._load_local(symbol, timeframe)
        if df_pg.empty:
            return df_local
        if df_local.empty:
            return df_pg
        try:
            df = pd.concat([df_pg, df_local])
            df = df[~df.index.duplicated(keep="first")].sort_index()
            return df
        except Exception as e:
            logger.warning("merge failed %s/%s: %s", symbol, timeframe, e)
            return df_pg

    def list_symbols(self) -> list[str]:
        if not self.root.exists():
            return []
        return sorted([d.name for d in self.root.iterdir() if d.is_dir()])
