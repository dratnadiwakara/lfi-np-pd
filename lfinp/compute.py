"""
Assemble PD compute inputs, run the kernels, upsert pd_panel.

Kernels reused verbatim from the NP paper code (via bank-pd):
  - compute_merton_dtd.compute_merton_dtd  (NP value-surface + classic Merton)
  - merton_pd_from_paper.merton_pd_from_paper (used internally by the above)
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Optional

import duckdb
import pandas as pd

from . import config
from .compute_merton_dtd import compute_merton_dtd


def assemble_inputs(
    conn: duckdb.DuckDBPyConnection,
    *,
    permco_filter: Optional[list[int]] = None,
    rssd_filter: Optional[list[int]] = None,
    week_date_min: Optional[str] = None,
    week_date_max: Optional[str] = None,
    exclude_existing: bool = True,
) -> pd.DataFrame:
    """Compute-ready rows from pd_input (strict kernel filter: sE,
    market_cap>0, total_liab>0, r all present). exclude_existing drops
    (week_date, permco) already in pd_panel — incremental compute."""
    wheres = [
        "sE IS NOT NULL",
        "market_cap IS NOT NULL",
        "market_cap > 0",
        "total_liab IS NOT NULL",
        "total_liab > 0",
        "r IS NOT NULL",
    ]
    params: list = []
    if permco_filter:
        wheres.append(f"permco IN ({','.join(str(int(p)) for p in permco_filter)})")
    if rssd_filter:
        wheres.append(f"rssd IN ({','.join(str(int(r)) for r in rssd_filter)})")
    if week_date_min:
        wheres.append("week_date >= ?")
        params.append(week_date_min)
    if week_date_max:
        wheres.append("week_date <= ?")
        params.append(week_date_max)

    where_sql = "WHERE " + " AND ".join(wheres)
    excl_sql = (
        "AND (week_date, permco) NOT IN (SELECT week_date, permco FROM pd_panel)"
        if exclude_existing else ""
    )
    sql = f"""
    SELECT
      rssd, permco, ticker, week_date, date_eff,
      EXTRACT(year  FROM week_date)::INTEGER AS year,
      EXTRACT(month FROM week_date)::INTEGER AS month,
      r, sE,
      market_cap AS market_cap_raw,
      total_liab,
      E_scaled AS E
    FROM pd_input
    {where_sql}
    {excl_sql}
    ORDER BY permco, week_date
    """
    return conn.execute(sql, params).fetchdf()


def run_compute(
    input_df: pd.DataFrame,
    *,
    value_surface_path: Optional[Path] = None,
    max_workers: Optional[int] = None,
) -> pd.DataFrame:
    """Write input to a temp CSV and run the value-surface + Merton kernels."""
    if input_df.empty:
        return input_df.copy()
    vs = Path(value_surface_path) if value_surface_path else config.value_surface_path()
    if not vs.exists():
        raise FileNotFoundError(f"ValueSurface.mat not found: {vs}")

    tmp = tempfile.NamedTemporaryFile(
        mode="w", suffix=".csv", delete=False, newline="", encoding="utf-8"
    )
    tmp.close()
    try:
        cols_for_csv = ["E", "permco", "year", "month", "r", "sE",
                        "rssd", "ticker", "week_date", "date_eff",
                        "market_cap_raw", "total_liab"]
        cols_for_csv = [c for c in cols_for_csv if c in input_df.columns]
        input_df[cols_for_csv].to_csv(tmp.name, index=False)
        return compute_merton_dtd(
            input_csv_path=tmp.name,
            value_surface_path=vs,
            vol_value=config.VOL_VALUE,
            T_pd=config.T_PD,
            gamma_pd=config.GAMMA_PD,
            max_workers=max_workers,
            preserve_columns=["rssd", "ticker", "week_date", "date_eff",
                              "market_cap_raw", "total_liab"],
        )
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass


def upsert_pd_panel(
    conn: duckdb.DuckDBPyConnection,
    results: pd.DataFrame,
) -> int:
    """Insert-or-replace compute results keyed (week_date, permco)."""
    if results.empty:
        return 0
    df = results.copy()
    df["np_PD"] = df["mdef"]
    df["E_scaled"] = df["E"]
    df["week_date"] = pd.to_datetime(df["week_date"]).dt.date
    df["permco"] = df["permco"].astype("Int64")
    df["rssd"] = df["rssd"].astype("Int64")

    keep_cols = [
        "week_date", "permco", "rssd",
        "total_liab", "market_cap_raw", "E_scaled", "sE", "r",
        "L", "B", "mdef", "fs", "bookF",
        "merton_PD", "np_PD",
        "L_fallback_used", "fs_fallback_used",
        "B_fallback_used", "bookF_fallback_used",
        "mdef_fallback_used",
    ]
    for c in keep_cols:
        if c not in df.columns:
            df[c] = pd.NA
    df = df[keep_cols]
    conn.register("_pd_new", df)
    try:
        conn.execute(
            "DELETE FROM pd_panel WHERE (week_date, permco) IN "
            "(SELECT week_date, permco FROM _pd_new)"
        )
        conn.execute(
            f"INSERT INTO pd_panel ({', '.join(keep_cols)}) "
            f"SELECT {', '.join(keep_cols)} FROM _pd_new"
        )
        return int(len(df))
    finally:
        conn.unregister("_pd_new")
