"""
DuckDB helpers and schema DDL for the lfi-np-pd local store.

Fresh schema, no legacy migrations. Two equity tables with a combining view:

- crsp_daily : WRDS CRSP, immutable once fetched.
- yf_daily   : Yahoo Finance; the last YF_REPULL_MONTHS window is replaced
               wholesale on every run (delete window + insert) so the entire
               vol window shares one split-adjustment basis and vendor
               revisions self-heal.
- equity_daily VIEW : CRSP wins per (permco, date); Yahoo fills after.

Audit tables:
- pull_diff  : what each re-pull changed vs the previous run; drives targeted
               pd_panel recompute and is the vendor-revision audit trail.
- check_log  : every accuracy-gate result, persisted.
"""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

import duckdb

from . import config


SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS fred_dgs10 (
  date DATE PRIMARY KEY,
  dgs10_pct DOUBLE,
  r_decimal DOUBLE
);

CREATE TABLE IF NOT EXISTS fred_weekly (
  week_date DATE PRIMARY KEY,
  r_decimal DOUBLE
);

CREATE TABLE IF NOT EXISTS crsp_daily (
  permco INTEGER NOT NULL,
  date   DATE    NOT NULL,
  price  DOUBLE,
  ret    DOUBLE,
  retx   DOUBLE,
  shrout DOUBLE,
  market_cap DOUBLE,            -- thousands of USD (CRSP convention)
  src_table TEXT,               -- 'dsf' | 'dsf_v2'
  PRIMARY KEY (permco, date)
);
CREATE INDEX IF NOT EXISTS ix_crsp_daily_date ON crsp_daily(date);

CREATE TABLE IF NOT EXISTS yf_daily (
  permco         INTEGER NOT NULL,
  date           DATE    NOT NULL,
  close          DOUBLE,             -- split-adjusted (yfinance auto_adjust=False)
  shares         DOUBLE,             -- split-corrected to the close's basis
  market_cap     DOUBLE,             -- close * shares / 1000 (thousands USD)
  retx           DOUBLE,             -- close-to-close pct change WITHIN one pull
  retx_synthetic BOOLEAN,            -- gap to prev close > SYNTHETIC_GAP_DAYS
  ticker         TEXT,
  pulled_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (permco, date)
);
CREATE INDEX IF NOT EXISTS ix_yf_daily_date ON yf_daily(date);

CREATE OR REPLACE VIEW equity_daily AS
  SELECT permco, date, price, retx, market_cap,
         'crsp' AS source
    FROM crsp_daily
  UNION ALL
  SELECT y.permco, y.date, y.close AS price, y.retx, y.market_cap,
         'yfinance' AS source
    FROM yf_daily y
   WHERE NOT EXISTS (
     SELECT 1 FROM crsp_daily c
      WHERE c.permco = y.permco AND c.date = y.date);

CREATE TABLE IF NOT EXISTS link (
  permco INTEGER NOT NULL,
  rssd   INTEGER NOT NULL,
  quarter_end DATE NOT NULL,
  name TEXT,
  confirmed BOOLEAN,
  PRIMARY KEY (permco, rssd, quarter_end)
);

CREATE TABLE IF NOT EXISTS ticker_hist (
  permco    INTEGER NOT NULL,
  permno    INTEGER NOT NULL,
  ticker    TEXT,
  comnam    TEXT,
  shrcd     INTEGER,
  namedt    DATE NOT NULL,
  nameenddt DATE NOT NULL,
  PRIMARY KEY (permco, permno, namedt)
);
CREATE INDEX IF NOT EXISTS ix_ticker_permco ON ticker_hist(permco);

CREATE TABLE IF NOT EXISTS pd_input (
  permco    INTEGER NOT NULL,
  week_date DATE    NOT NULL,
  date_eff  DATE,
  rssd      INTEGER,
  ticker    TEXT,
  market_cap DOUBLE,
  price     DOUBLE,
  sE        DOUBLE,
  n_obs_252 INTEGER,
  r         DOUBLE,
  bs_quarter_end DATE,
  total_liab DOUBLE,
  assets    DOUBLE,
  equity    DOUBLE,
  E_scaled  DOUBLE,
  bs_age_days   INTEGER,
  bs_stale      BOOLEAN,
  equity_lag_days INTEGER,
  equity_stale    BOOLEAN,
  data_source   TEXT,                       -- 'crsp' | 'yfinance' at date_eff
  bs_source     TEXT,                       -- 'y9c' | 'call_report' | NULL
  built_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (permco, week_date)
);
CREATE INDEX IF NOT EXISTS ix_pd_input_rssd ON pd_input(rssd, week_date);
CREATE INDEX IF NOT EXISTS ix_pd_input_week ON pd_input(week_date);

CREATE TABLE IF NOT EXISTS pd_panel (
  week_date DATE NOT NULL,
  permco    INTEGER NOT NULL,
  rssd      INTEGER,
  total_liab DOUBLE,
  market_cap_raw DOUBLE,
  E_scaled DOUBLE,
  sE DOUBLE,
  r  DOUBLE,
  L DOUBLE, B DOUBLE, mdef DOUBLE, fs DOUBLE, bookF DOUBLE,
  merton_PD DOUBLE,
  np_PD DOUBLE,
  L_fallback_used TINYINT,
  fs_fallback_used TINYINT,
  B_fallback_used TINYINT,
  bookF_fallback_used TINYINT,
  mdef_fallback_used TINYINT,
  computed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (week_date, permco)
);
CREATE INDEX IF NOT EXISTS ix_pd_panel_rssd ON pd_panel(rssd, week_date);

CREATE TABLE IF NOT EXISTS pull_diff (
  run_at  TIMESTAMP NOT NULL,
  permco  INTEGER NOT NULL,
  date    DATE NOT NULL,
  field   TEXT NOT NULL,        -- 'market_cap' | 'retx' | 'shares_suspect' | ...
  old_value DOUBLE,
  new_value DOUBLE
);
CREATE INDEX IF NOT EXISTS ix_pull_diff_run ON pull_diff(run_at);

CREATE TABLE IF NOT EXISTS check_log (
  run_at  TIMESTAMP NOT NULL,
  check_name TEXT NOT NULL,
  passed  BOOLEAN NOT NULL,
  details TEXT
);

-- Reviewed flags. Without this every real crisis (1987, 2008, COVID, SVB)
-- re-fires forever and the operator learns to ignore the checks. Once a flag
-- is acked it stays suppressed, so anything still showing is unexplained.
CREATE TABLE IF NOT EXISTS flag_ack (
  check_name TEXT    NOT NULL,
  rssd       INTEGER NOT NULL,
  ref_date   DATE    NOT NULL,
  verdict    TEXT,                 -- 'real' | 'artifact' | 'accepted'
  note       TEXT,
  acked_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (check_name, rssd, ref_date)
);

-- Snapshot of pd_panel taken before each compute, so a run can report which
-- HISTORICAL values it silently changed (pull_diff covers inputs only).
CREATE TABLE IF NOT EXISTS pd_panel_prev (
  week_date DATE NOT NULL,
  permco    INTEGER NOT NULL,
  rssd      INTEGER,
  sE DOUBLE, np_PD DOUBLE, merton_PD DOUBLE,
  snapshot_at TIMESTAMP,
  PRIMARY KEY (week_date, permco)
);
"""


def _apply_pragmas(conn: duckdb.DuckDBPyConnection) -> None:
    conn.execute(f"PRAGMA threads={config.DUCKDB_THREADS}")
    conn.execute(f"PRAGMA memory_limit='{config.DUCKDB_MEMORY_LIMIT}'")


def get_connection(
    db_path: Optional[Path] = None,
    *,
    read_only: bool = False,
) -> duckdb.DuckDBPyConnection:
    p = Path(db_path) if db_path else config.data_db_path()
    conn = duckdb.connect(str(p), read_only=read_only)
    _apply_pragmas(conn)
    return conn


@contextmanager
def transactional_connection(
    db_path: Optional[Path] = None,
) -> Iterator[duckdb.DuckDBPyConnection]:
    conn = get_connection(db_path, read_only=False)
    try:
        conn.execute("BEGIN TRANSACTION")
        yield conn
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def attach_external(
    conn: duckdb.DuckDBPyConnection,
    alias: str,
    db_path: Path,
) -> None:
    if not Path(db_path).exists():
        raise FileNotFoundError(f"External DuckDB not found: {db_path}")
    conn.execute(f"ATTACH '{db_path}' AS {alias} (READ_ONLY)")


def detach(conn: duckdb.DuckDBPyConnection, alias: str) -> None:
    try:
        conn.execute(f"DETACH {alias}")
    except duckdb.Error:
        pass


def init_schema(conn: duckdb.DuckDBPyConnection) -> None:
    conn.execute(SCHEMA_DDL)


def max_value(
    conn: duckdb.DuckDBPyConnection,
    table: str,
    col: str,
    where: Optional[str] = None,
):
    sql = f"SELECT MAX({col}) FROM {table}"
    if where:
        sql += f" WHERE {where}"
    row = conn.execute(sql).fetchone()
    return row[0] if row else None


def log_check(
    conn: duckdb.DuckDBPyConnection,
    check_name: str,
    passed: bool,
    details: str = "",
) -> None:
    conn.execute(
        "INSERT INTO check_log (run_at, check_name, passed, details) "
        "VALUES (CURRENT_TIMESTAMP, ?, ?, ?)",
        [check_name, passed, details[:4000]],
    )


def snapshot_panel(conn: duckdb.DuckDBPyConnection) -> int:
    """Freeze the current pd_panel so the next compute can report which
    historical values it changed. Call immediately before computing."""
    conn.execute("DELETE FROM pd_panel_prev")
    conn.execute(
        """
        INSERT INTO pd_panel_prev
              (week_date, permco, rssd, sE, np_PD, merton_PD, snapshot_at)
        SELECT week_date, permco, rssd, sE, np_PD, merton_PD, CURRENT_TIMESTAMP
        FROM pd_panel
        """
    )
    return int(conn.execute("SELECT COUNT(*) FROM pd_panel_prev").fetchone()[0])


def ack_flag(
    conn: duckdb.DuckDBPyConnection,
    check_name: str,
    rssd: int,
    ref_date,
    verdict: str = "real",
    note: str = "",
) -> None:
    """Mark a reviewed flag as explained; it stays suppressed thereafter."""
    conn.execute(
        """
        INSERT INTO flag_ack (check_name, rssd, ref_date, verdict, note, acked_at)
        VALUES (?, ?, ?, ?, ?, now())
        ON CONFLICT (check_name, rssd, ref_date)
        DO UPDATE SET verdict = EXCLUDED.verdict, note = EXCLUDED.note,
                      acked_at = EXCLUDED.acked_at
        """,
        [check_name, int(rssd), ref_date, verdict, note],
    )
