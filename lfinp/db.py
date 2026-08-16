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

-- Business-model group per bank, mirrored from banks.txt on every run so SQL
-- can cut the panel without re-parsing the file. banks.txt stays the source of
-- truth; this table is derived and safe to drop.
CREATE TABLE IF NOT EXISTS bank_group (
  rssd    INTEGER NOT NULL PRIMARY KEY,
  grp     TEXT,
  permco  INTEGER,             -- resolved at sync time; NULL if link lookup failed
  dead    BOOLEAN,
  name    TEXT,
  synced_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Cap-weighted daily index per group: the equity of the group treated as one
-- merged bank. ret is NULL on the first day (no lagged weight), same as retx.
CREATE TABLE IF NOT EXISTS group_daily (
  grp        TEXT NOT NULL,
  date       DATE NOT NULL,
  ret        DOUBLE,
  market_cap DOUBLE,
  n_members  INTEGER,          -- members with a market cap that day
  n_ret_members INTEGER,       -- members contributing to the return that day
  PRIMARY KEY (grp, date)
);

CREATE TABLE IF NOT EXISTS group_input (
  grp        TEXT NOT NULL,
  week_date  DATE NOT NULL,
  date_eff   DATE,
  market_cap DOUBLE,
  sE         DOUBLE,
  n_obs_252  INTEGER,
  r          DOUBLE,
  bs_quarter_end DATE,
  total_liab DOUBLE,           -- NULL unless balance-sheet cap coverage clears the bar
  assets     DOUBLE,
  equity     DOUBLE,
  E_scaled   DOUBLE,
  n_members    INTEGER,
  n_members_bs INTEGER,
  bs_cap_coverage DOUBLE,
  n_ret_members INTEGER,
  built_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (grp, week_date)
);

CREATE TABLE IF NOT EXISTS group_panel (
  week_date DATE NOT NULL,
  grp       TEXT NOT NULL,
  n_members INTEGER,           -- carried through: a level shift may be composition
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
  PRIMARY KEY (week_date, grp)
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
    # CREATE TABLE IF NOT EXISTS will not widen a table that predates a column.
    # bank_group is derived, but dropping it here would empty it for read-only
    # commands until the next update, so add the column in place instead.
    conn.execute("ALTER TABLE bank_group ADD COLUMN IF NOT EXISTS permco INTEGER")


def sync_bank_groups(
    conn: duckdb.DuckDBPyConnection,
    permco_map: Optional[dict] = None,
) -> int:
    """Mirror banks.txt groups into bank_group. Full replace, not a merge: a
    bank dropped from banks.txt must disappear from the mapping too, otherwise
    a stale group silently keeps showing up in group-wise cuts.

    permco_map (rssd -> permco) is what lets the group index join equity_daily
    directly. Without it the column is NULL and the group build finds nothing,
    which is loud rather than wrong."""
    banks = config.load_banks()
    pm = permco_map or {}
    conn.execute("DELETE FROM bank_group")
    conn.executemany(
        "INSERT INTO bank_group (rssd, grp, permco, dead, name) VALUES (?, ?, ?, ?, ?)",
        [(b.rssd, b.group, pm.get(b.rssd), b.dead, b.comment) for b in banks],
    )
    return len(banks)


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


def export_panel_snapshot(
    conn: duckdb.DuckDBPyConnection,
    run_date=None,
    *,
    out_dir: Optional[Path] = None,
) -> Optional[Path]:
    """Write the whole pd_panel to data/snapshots/pd_panel_YYYY-MM-DD.parquet.

    pd_panel_prev is one run deep, so it answers "did the last run change this
    number?" and nothing more. Values inside the rolling window legitimately
    move between runs (vendor revisions are absorbed by design), which means a
    number cited in a report months ago is not otherwise retrievable: the
    inputs are reconstructible from yf_daily + pull_diff, the published output
    is not. These files close that gap.

    Same-day re-runs overwrite; each run is a complete panel, not a delta, so
    the set of files can be read as one table:

        SELECT * FROM read_parquet('data/snapshots/pd_panel_*.parquet')

    Returns the path written, or None when pd_panel is empty.
    """
    from datetime import date as _date

    d = run_date or _date.today()
    n = int(conn.execute("SELECT COUNT(*) FROM pd_panel").fetchone()[0])
    if not n:
        return None
    target = Path(out_dir) if out_dir else config.snapshot_dir()
    target.mkdir(parents=True, exist_ok=True)
    path = target / f"pd_panel_{d.isoformat()}.parquet"
    # snapshot_date is carried in the rows as well as the filename so a
    # multi-file read stays self-describing.
    conn.execute(
        "COPY (SELECT DATE '"
        + d.isoformat()
        + "' AS snapshot_date, * FROM pd_panel ORDER BY week_date, permco) "
        f"TO '{path.as_posix()}' (FORMAT PARQUET, COMPRESSION ZSTD)"
    )
    return path


def export_group_snapshot(
    conn: duckdb.DuckDBPyConnection,
    run_date=None,
    *,
    out_dir: Optional[Path] = None,
) -> Optional[Path]:
    """Archive group_panel joined to its inputs, same contract as the bank
    panel: complete table per run, snapshot_date in the rows, same-day re-runs
    overwrite.

    Group PDs get cited like bank PDs and revise for the same reason, so they
    need the same record. The input columns ride along in one file rather than
    a second: a group row is small, and n_members / bs_cap_coverage are the two
    things a reader needs to interpret the number at all."""
    from datetime import date as _date

    d = run_date or _date.today()
    if not int(conn.execute("SELECT COUNT(*) FROM group_panel").fetchone()[0]):
        return None
    target = Path(out_dir) if out_dir else config.snapshot_dir()
    target.mkdir(parents=True, exist_ok=True)
    path = target / f"group_panel_{d.isoformat()}.parquet"
    conn.execute(
        f"""COPY (
              SELECT DATE '{d.isoformat()}' AS snapshot_date, p.*,
                     i.date_eff, i.n_obs_252, i.n_members_bs,
                     i.bs_cap_coverage, i.n_ret_members
              FROM group_panel p
              LEFT JOIN group_input i USING (grp, week_date)
              ORDER BY p.week_date, p.grp
            ) TO '{path.as_posix()}' (FORMAT PARQUET, COMPRESSION ZSTD)""")
    return path


def export_input_snapshot(
    conn: duckdb.DuckDBPyConnection,
    run_date=None,
    *,
    out_dir: Optional[Path] = None,
) -> Optional[Path]:
    """Write pd_input plus the actual return series behind each sE to
    data/snapshots/pd_input_YYYY-MM-DD.parquet.

    The panel snapshot preserves the published PD; this preserves what it was
    computed from, in a form that needs nothing else to check. Each row is one
    (permco, week) with its balance-sheet and rate inputs, plus `retx_csv`:
    the VOL_WINDOW daily returns ending at date_eff, in date order, comma
    separated, exactly as panel.build_pd_input's window sees them.

    Two details make the string reproduce sE rather than merely resemble it:

    - The window is VOL_WINDOW *rows* of equity_daily, not 252 non-null
      returns, so a missing retx is emitted as an empty field and holds its
      slot. STDDEV_SAMP ignores it; dropping it would silently shift the
      window a day earlier.
    - Values are cast to VARCHAR, not printf-formatted. DuckDB's double
      rendering round-trips; %.8f does not, and a re-derived sE that differs
      in the 9th decimal is a false positive nobody can dismiss quickly.

    Recompute check: stddev_samp(the non-empty fields) * sqrt(252) == sE.
    """
    from datetime import date as _date

    d = run_date or _date.today()
    n = int(conn.execute("SELECT COUNT(*) FROM pd_input").fetchone()[0])
    if not n:
        return None
    target = Path(out_dir) if out_dir else config.snapshot_dir()
    target.mkdir(parents=True, exist_ok=True)
    path = target / f"pd_input_{d.isoformat()}.parquet"
    conn.execute(
        f"""
        COPY (
          WITH d AS (
            SELECT permco, date, retx,
                   ROW_NUMBER() OVER (PARTITION BY permco ORDER BY date) AS rn
            FROM equity_daily
            WHERE permco IN (SELECT DISTINCT permco FROM pd_input)
          ),
          anchored AS (
            SELECT i.permco, i.week_date, d.rn AS rn_eff
            FROM pd_input i
            JOIN d ON d.permco = i.permco AND d.date = i.date_eff
          ),
          series AS (
            SELECT a.permco, a.week_date,
                   string_agg(COALESCE(CAST(w.retx AS VARCHAR), ''), ','
                              ORDER BY w.date) AS retx_csv,
                   COUNT(w.retx)               AS retx_n_obs,
                   COUNT(*)                    AS retx_n_rows,
                   MIN(w.date)                 AS retx_from,
                   MAX(w.date)                 AS retx_to
            FROM anchored a
            JOIN d w
              ON w.permco = a.permco
             AND w.rn BETWEEN a.rn_eff - {config.VOL_WINDOW - 1} AND a.rn_eff
            GROUP BY a.permco, a.week_date
          )
          SELECT DATE '{d.isoformat()}' AS snapshot_date, i.*,
                 s.retx_from, s.retx_to, s.retx_n_rows, s.retx_n_obs, s.retx_csv
          FROM pd_input i
          LEFT JOIN series s USING (permco, week_date)
          ORDER BY i.week_date, i.permco
        ) TO '{path.as_posix()}' (FORMAT PARQUET, COMPRESSION ZSTD)
        """
    )
    return path


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
