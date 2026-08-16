"""
External data fetchers: WRDS CRSP daily (legacy dsf + dsf_v2), CRSP ticker
history, FRED DGS10, and the permco<->RSSD link mirror.

CRSP rows are immutable once fetched: the fetcher only appends past each
permco's watermark. Legacy crsp.dsf is frozen at its last annual update
(2024-12-31 as of Aug 2026); crsp.dsf_v2 (CIZ format) may extend further.
dsf_v2 rows are accepted only after validate_v2_against_legacy() passes on
an overlap window.
"""
from __future__ import annotations

from datetime import date
from typing import Iterable, Optional

import duckdb
import pandas as pd
import requests

from . import config
from .db import attach_external, detach, max_value

FRED_URL = "https://api.stlouisfed.org/fred/series/observations"
HTTP_TIMEOUT = 60


# -- WRDS ---------------------------------------------------------------------


def connect_wrds(username: str, password: str):
    import wrds
    return wrds.Connection(wrds_username=username, wrds_password=password)


def _fetch_dsf_legacy(db, permcos: list[int], start: str, end: str) -> pd.DataFrame:
    permco_str = ",".join(str(int(p)) for p in permcos)
    sql = f"""
        SELECT permco, date,
               ABS(prc) AS price,
               ret, retx, shrout,
               shrout * ABS(prc) AS market_cap
        FROM crsp.dsf
        WHERE date >= '{start}' AND date <= '{end}'
          AND permco IN ({permco_str})
        ORDER BY permco, date
    """
    df = db.raw_sql(sql)
    if df is not None and len(df):
        df["src_table"] = "dsf"
    return df


def _fetch_dsf_v2(db, permcos: list[int], start: str, end: str) -> pd.DataFrame:
    """CIZ-format daily stock file. Column mapping verified against legacy
    dsf on an overlap window before any v2 row is trusted (see
    validate_v2_against_legacy)."""
    permco_str = ",".join(str(int(p)) for p in permcos)
    sql = f"""
        SELECT permco,
               dlycaldt AS date,
               ABS(dlyprc) AS price,
               dlyret  AS ret,
               dlyretx AS retx,
               shrout,
               shrout * ABS(dlyprc) AS market_cap
        FROM crsp.dsf_v2
        WHERE dlycaldt >= '{start}' AND dlycaldt <= '{end}'
          AND permco IN ({permco_str})
        ORDER BY permco, dlycaldt
    """
    df = db.raw_sql(sql)
    if df is not None and len(df):
        df["src_table"] = "dsf_v2"
    return df


def v2_max_date(db, probe_permno: int = 59408) -> Optional[date]:
    """Latest date available in crsp.dsf_v2, probed via one liquid permno
    (default BAC 59408). A bare MAX() over the table is a full scan that
    runs for tens of minutes; the permno-filtered ORDER BY ... LIMIT 1
    returns in seconds."""
    try:
        row = db.raw_sql(
            f"SELECT dlycaldt AS mx FROM crsp.dsf_v2 "
            f"WHERE permno = {int(probe_permno)} "
            f"ORDER BY dlycaldt DESC LIMIT 1"
        )
        v = row["mx"].iloc[0]
        return pd.Timestamp(v).date() if pd.notna(v) else None
    except Exception:
        return None


def validate_v2_against_legacy(
    db, permcos: list[int], start: str = "2024-07-01", end: str = "2024-12-31",
) -> pd.DataFrame:
    """Compare dsf_v2 vs legacy dsf on a shared window. Returns rows that
    disagree (retx beyond 1e-8 or market_cap beyond 0.1%). Empty = pass."""
    a = _fetch_dsf_legacy(db, permcos, start, end)
    b = _fetch_dsf_v2(db, permcos, start, end)
    if a is None or b is None or not len(a) or not len(b):
        raise RuntimeError("v2 validation: one side returned no rows")
    for df in (a, b):
        df["date"] = pd.to_datetime(df["date"]).dt.date
        df["permco"] = df["permco"].astype(int)
    m = a.merge(b, on=["permco", "date"], suffixes=("_dsf", "_v2"))
    if not len(m):
        raise RuntimeError("v2 validation: zero overlapping (permco,date) rows")
    # dsf_v2 stores returns rounded to 6 decimals; legacy dsf carries more
    # precision. 5e-6 is comfortably above that rounding and far below any
    # real data difference. retx is the vol-critical field: ANY retx
    # disagreement rejects v2. Market cap gets latitude for small shrout
    # revisions between the products (observed: 3 boundary-day rows up to
    # 2.1%): reject only when large or widespread.
    retx_bad = m[(m["retx_dsf"] - m["retx_v2"]).abs() > 5e-6]
    mcap_rel = (m["market_cap_v2"] / m["market_cap_dsf"] - 1).abs()
    mcap_bad = m[mcap_rel > 0.001]
    if len(retx_bad) or (mcap_rel > 0.05).any() or len(mcap_bad) > 5:
        bad = pd.concat([retx_bad, mcap_bad]).drop_duplicates(
            subset=["permco", "date"])
    else:
        if len(mcap_bad):
            print(f"  note: dsf_v2 mcap differs on {len(mcap_bad)} row(s) "
                  f"(max {mcap_rel.max():.2%}, shrout revision) - accepted; "
                  f"retx identical on all {len(m):,} rows")
        bad = m.iloc[0:0]
    return bad[["permco", "date", "retx_dsf", "retx_v2",
                "market_cap_dsf", "market_cap_v2"]]


def fetch_crsp_daily(
    conn: duckdb.DuckDBPyConnection,
    db,
    permcos: Iterable[int],
    *,
    use_v2_after: Optional[str] = None,
) -> tuple[int, Optional[date]]:
    """Append CRSP daily rows past each permco's watermark.

    Legacy dsf up to its frozen edge; if `use_v2_after` is set (an ISO date,
    normally the legacy edge), dsf_v2 rows strictly after that date are
    appended too. Returns (rows inserted, earliest inserted date) — the
    date drives pd_panel recompute, since new CRSP rows replace Yahoo rows
    in the view and shift every vol window that touches them."""
    permcos = sorted({int(p) for p in permcos})
    if not permcos:
        return 0, None
    wm = conn.execute(
        "SELECT permco, MAX(date) FROM crsp_daily GROUP BY permco"
    ).fetchall()
    watermarks = {int(r[0]): r[1] for r in wm}
    today = date.today().isoformat()

    frames = []
    # One batch query per source (25 permcos fit in a single query). Any
    # permco with no watermark (newly added bank) forces a full-history
    # query; the per-permco filter below trims the rest.
    if any(watermarks.get(p) is None for p in permcos):
        legacy_start = config.START_DATE
    else:
        start_all = min(watermarks[p] for p in permcos)
        legacy_start = (
            pd.Timestamp(start_all) + pd.Timedelta(days=1)
        ).strftime("%Y-%m-%d")
    legacy = _fetch_dsf_legacy(db, permcos, legacy_start, today)
    if legacy is not None and len(legacy):
        frames.append(legacy)
    if use_v2_after:
        v2 = _fetch_dsf_v2(
            db, permcos,
            (pd.Timestamp(use_v2_after) + pd.Timedelta(days=1)).strftime("%Y-%m-%d"),
            today,
        )
        if v2 is not None and len(v2):
            frames.append(v2)
    if not frames:
        return 0, None

    df = pd.concat(frames, ignore_index=True)
    df = df.dropna(subset=["permco", "date"])
    df["permco"] = df["permco"].astype(int)
    df["date"] = pd.to_datetime(df["date"]).dt.date
    # Per-permco watermark filter (batch query used the global min watermark)
    def _past_wm(row):
        w = watermarks.get(row.permco)
        return w is None or row.date > w
    df = df[[_past_wm(r) for r in df.itertuples()]]
    if not len(df):
        return 0, None

    conn.register("_crsp_new", df)
    try:
        conn.execute(
            """
            INSERT INTO crsp_daily
                  (permco, date, price, ret, retx, shrout, market_cap, src_table)
            SELECT permco, date, price, ret, retx, shrout, market_cap, src_table
            FROM _crsp_new
            ON CONFLICT (permco, date) DO NOTHING
            """
        )
        inserted = int(len(df))
        min_date = df["date"].min()
    finally:
        conn.unregister("_crsp_new")
    return inserted, min_date


def fetch_ticker_hist(
    conn: duckdb.DuckDBPyConnection,
    db,
    permcos: Iterable[int],
) -> int:
    """Full replace of ticker_hist for the given permcos. Common stock only
    (shrcd 10, 11), permno bridged to permco via crsp.dsf."""
    permcos = sorted({int(p) for p in permcos})
    if not permcos:
        return 0
    permco_str = ",".join(str(p) for p in permcos)
    sql = f"""
        SELECT DISTINCT
               b.permco, s.permno, s.ticker, s.comnam, s.shrcd,
               s.namedt, s.nameenddt
        FROM crsp.stocknames s
        JOIN (SELECT DISTINCT permco, permno
              FROM crsp.dsf
              WHERE permco IN ({permco_str})) b USING (permno)
        WHERE s.shrcd IN (10, 11)
    """
    df = db.raw_sql(sql)
    if df is None or not len(df):
        return 0
    df = df.dropna(subset=["permco", "permno", "namedt", "nameenddt"]).copy()
    df["permco"] = df["permco"].astype(int)
    df["permno"] = df["permno"].astype(int)
    df["namedt"] = pd.to_datetime(df["namedt"]).dt.date
    df["nameenddt"] = pd.to_datetime(df["nameenddt"]).dt.date
    df["shrcd"] = pd.to_numeric(df["shrcd"], errors="coerce").astype("Int64")
    conn.register("_ticker_new", df)
    try:
        conn.execute(
            "DELETE FROM ticker_hist WHERE permco IN "
            "(SELECT DISTINCT permco FROM _ticker_new)"
        )
        conn.execute(
            """
            INSERT INTO ticker_hist
                  (permco, permno, ticker, comnam, shrcd, namedt, nameenddt)
            SELECT permco, permno, ticker, comnam, shrcd, namedt, nameenddt
            FROM _ticker_new
            ON CONFLICT (permco, permno, namedt) DO NOTHING
            """
        )
    finally:
        conn.unregister("_ticker_new")
    return int(len(df))


# -- FRED ---------------------------------------------------------------------


def fetch_dgs10_incremental(conn: duckdb.DuckDBPyConnection, api_key: str) -> int:
    """Append-only DGS10 fetch. Returns rows appended."""
    last = max_value(conn, "fred_dgs10", "date")
    start = (
        (pd.Timestamp(last) + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
        if last is not None else config.START_DATE
    )
    today = date.today().strftime("%Y-%m-%d")
    if start > today:
        return 0
    resp = requests.get(FRED_URL, params={
        "series_id": "DGS10", "api_key": api_key,
        "file_type": "json", "observation_start": start,
    }, timeout=HTTP_TIMEOUT)
    resp.raise_for_status()
    obs = resp.json().get("observations", [])
    if not obs:
        return 0
    df = pd.DataFrame(obs)
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df["dgs10_pct"] = pd.to_numeric(df["value"], errors="coerce")
    df["r_decimal"] = df["dgs10_pct"] / 100.0
    df = df.dropna(subset=["date"])
    if last is not None:
        df = df[df["date"] > pd.Timestamp(last)]
    if df.empty:
        return 0
    conn.register("_fred_new", df)
    try:
        conn.execute(
            """
            INSERT INTO fred_dgs10 (date, dgs10_pct, r_decimal)
            SELECT CAST(date AS DATE), dgs10_pct, r_decimal FROM _fred_new
            ON CONFLICT (date) DO NOTHING
            """
        )
    finally:
        conn.unregister("_fred_new")
    return int(len(df))


# Friday resample of DGS10 lives in panel.build_fred_weekly (ASOF version).


# -- Link mirror ----------------------------------------------------------------


def refresh_link(conn: duckdb.DuckDBPyConnection) -> int:
    """Full refresh of local link table from the external view."""
    attach_external(conn, "ext_link", config.link_db_path())
    try:
        conn.execute("DELETE FROM link")
        conn.execute(
            """
            INSERT INTO link (permco, rssd, quarter_end, name, confirmed)
            SELECT CAST(permco AS INTEGER), CAST(bhc_rssd AS INTEGER),
                   CAST(quarter_end AS DATE), name, CAST(confirmed AS BOOLEAN)
            FROM ext_link.crsp_frb_link
            WHERE permco IS NOT NULL AND bhc_rssd IS NOT NULL
            """
        )
        return int(conn.execute("SELECT COUNT(*) FROM link").fetchone()[0])
    finally:
        detach(conn, "ext_link")


def permcos_for_rssds(
    conn: duckdb.DuckDBPyConnection, rssds: list[int],
) -> dict[int, int]:
    """rssd -> permco via the link table, taken at each RSSD's most recent
    linked quarter (historical re-mappings across M&A are expected; only
    same-quarter ambiguity is an error). Raises if any RSSD is missing or
    ambiguous at its latest quarter — accuracy over convenience, no silent
    drops."""
    if not rssds:
        return {}
    placeholders = ",".join(str(int(r)) for r in rssds)
    rows = conn.execute(
        f"""
        WITH latest AS (
          SELECT rssd, MAX(quarter_end) AS qe
          FROM link WHERE rssd IN ({placeholders}) AND confirmed
          GROUP BY rssd
        )
        SELECT l.rssd, ARRAY_AGG(DISTINCT k.permco) AS permcos
        FROM latest l
        JOIN link k ON k.rssd = l.rssd AND k.quarter_end = l.qe AND k.confirmed
        GROUP BY l.rssd
        """
    ).fetchall()
    out: dict[int, int] = {}
    ambiguous = []
    for rssd, permcos in rows:
        if len(permcos) > 1:
            ambiguous.append(int(rssd))
        out[int(rssd)] = int(permcos[0])
    missing = [r for r in rssds if r not in out]
    if missing:
        raise RuntimeError(f"RSSDs not found in link table: {missing}")
    if ambiguous:
        raise RuntimeError(
            f"RSSDs map to multiple permcos at their latest quarter "
            f"(resolve manually): {sorted(set(ambiguous))}"
        )
    return out
