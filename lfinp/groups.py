"""
Group-level NP/Merton PD: each business-model group treated as one bank.

The synthetic bank is a *merged entity*, not an average of its members' PDs:
liabilities are summed, equity is summed, and the volatility is the volatility
of the combined equity — i.e. of the cap-weighted portfolio of the members, not
the mean of their individual sigmas. That distinction is the point. Averaging
member PDs answers "how risky is a typical dealer"; merging answers "how risky
is the dealer sector as one balance sheet", and the gap between the two is
diversification, which is exactly what a sector PD is supposed to show.

Three properties are load-bearing:

1. **Weights are lagged.** w_i(t) = mcap_i(t-1). Using same-day market cap
   would multiply each return by the cap it produced, which is a look-ahead and
   biases the index toward whoever rose that day.
2. **Membership is whoever traded that day.** SVB leaves the lender index in
   March 2023 because it stops having rows, not because of a rule. This is a
   cap-weighted index, so composition changes shift the level; n_members is
   stored on every row so a jump can be attributed.
3. **Partial balance-sheet coverage is refused, not summed.** If members
   holding 5%+ of the group's market cap have no Y-9C row that week, the group
   total_liab is NULL and the week does not compute. A partial sum understates
   liabilities, which understates the PD -- silently, in the safe-looking
   direction.
"""
from __future__ import annotations

from datetime import date as _date
from typing import Optional

import duckdb
import pandas as pd

from . import compute as compute_mod
from . import config

# Members holding at least this share of the group's market cap must have a
# balance sheet, or the week is refused. Partial sums understate liabilities.
BS_MIN_CAP_COVERAGE = 0.95

# A group needs this many members with usable equity data before its index is
# meaningful. 'dealer' has exactly 2, so this cannot be raised past 2 without
# silently dropping that group.
MIN_MEMBERS = 2


def group_id(grp: str) -> int:
    """Synthetic permco. Negative so it can never collide with a real CRSP
    permco if the two ever land in the same frame."""
    return -(config.BANK_GROUPS.index(grp) + 1)


def build_group_daily(conn: duckdb.DuckDBPyConnection) -> int:
    """Cap-weighted daily index per group, from equity_daily + bank_group.

    ret is NULL on a group's first day (no lagged weights yet), exactly as retx
    is NULL on a member's first day -- the vol window counts rows, so the slot
    has to exist."""
    conn.execute("DELETE FROM group_daily")
    conn.execute(f"""
    INSERT INTO group_daily (grp, date, ret, market_cap, n_members, n_ret_members)
    WITH mem AS (
      SELECT bg.grp, e.permco, e.date, e.retx, e.market_cap,
             LAG(e.market_cap) OVER (PARTITION BY e.permco ORDER BY e.date) AS w
      FROM equity_daily e
      JOIN bank_group bg ON bg.permco = e.permco
      WHERE bg.grp IS NOT NULL
    ),
    caps AS (
      SELECT grp, date, SUM(market_cap) AS market_cap, COUNT(*) AS n_members
      FROM mem WHERE market_cap IS NOT NULL GROUP BY grp, date
    ),
    rets AS (
      SELECT grp, date,
             SUM(retx * w) / SUM(w) AS ret,
             COUNT(*) AS n_ret_members
      FROM mem
      WHERE retx IS NOT NULL AND w IS NOT NULL AND w > 0
      GROUP BY grp, date
    )
    SELECT c.grp, c.date, r.ret, c.market_cap, c.n_members,
           COALESCE(r.n_ret_members, 0)
    FROM caps c
    LEFT JOIN rets r ON r.grp = c.grp AND r.date = c.date
    WHERE c.n_members >= {MIN_MEMBERS}
    """)
    return int(conn.execute("SELECT COUNT(*) FROM group_daily").fetchone()[0])


def build_group_input(
    conn: duckdb.DuckDBPyConnection,
    *,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> int:
    """Friday-anchored group panel.

    Volatility comes from group_daily on the same 252-row / 126-minimum basis as
    a member bank. Balance sheet and market cap are summed from pd_input, so the
    group is by construction the sum of the same numbers the member PDs used --
    no second as-of join, no chance of the two disagreeing.
    """
    start = start_date or config.START_DATE
    end = end_date or _date.today().strftime("%Y-%m-%d")
    conn.execute("DELETE FROM group_input")
    conn.execute(f"""
    INSERT INTO group_input (
      grp, week_date, date_eff, market_cap, sE, n_obs_252, r,
      bs_quarter_end, total_liab, assets, equity, E_scaled,
      n_members, n_members_bs, bs_cap_coverage, n_ret_members
    )
    WITH daily_with_vol AS (
      SELECT grp, date, market_cap, n_members, n_ret_members,
             STDDEV_SAMP(ret) OVER (
               PARTITION BY grp ORDER BY date
               ROWS BETWEEN {config.VOL_WINDOW - 1} PRECEDING AND CURRENT ROW
             ) * sqrt(252) AS sE_raw,
             COUNT(ret) OVER (
               PARTITION BY grp ORDER BY date
               ROWS BETWEEN {config.VOL_WINDOW - 1} PRECEDING AND CURRENT ROW
             ) AS n_obs_252
      FROM group_daily
    ),
    bounds AS (
      SELECT grp, MIN(date) AS first_date FROM group_daily GROUP BY grp
    ),
    fridays AS (
      SELECT range AS week_date
      FROM range(DATE '{start}', DATE '{end}' + INTERVAL 1 DAY, INTERVAL 1 DAY)
      WHERE EXTRACT(dow FROM range) = 5
    ),
    grp_fridays AS (
      SELECT b.grp, f.week_date FROM bounds b JOIN fridays f ON f.week_date >= b.first_date
    ),
    resampled AS (
      SELECT gf.grp, gf.week_date, d.date AS date_eff,
             CASE WHEN d.n_obs_252 >= {config.VOL_MIN_PERIODS} THEN d.sE_raw END AS sE,
             d.n_obs_252, d.n_members AS n_members_daily, d.n_ret_members
      FROM grp_fridays gf
      ASOF LEFT JOIN daily_with_vol d ON d.grp = gf.grp AND gf.week_date >= d.date
    ),
    -- summed from the member panel, so group inputs are the member inputs
    members AS (
      SELECT bg.grp, i.week_date,
             SUM(i.market_cap)                                        AS market_cap,
             SUM(i.total_liab) FILTER (WHERE i.market_cap IS NOT NULL) AS total_liab,
             SUM(i.assets)     FILTER (WHERE i.market_cap IS NOT NULL) AS assets,
             SUM(i.equity)     FILTER (WHERE i.market_cap IS NOT NULL) AS equity,
             MAX(i.bs_quarter_end)                                    AS bs_quarter_end,
             COUNT(*)          FILTER (WHERE i.market_cap IS NOT NULL) AS n_members,
             COUNT(*)          FILTER (WHERE i.market_cap IS NOT NULL
                                         AND i.total_liab IS NOT NULL) AS n_members_bs,
             SUM(i.market_cap) FILTER (WHERE i.total_liab IS NOT NULL)
               / NULLIF(SUM(i.market_cap), 0)                         AS bs_cap_coverage
      FROM pd_input i
      JOIN bank_group bg ON bg.rssd = i.rssd
      WHERE bg.grp IS NOT NULL
      GROUP BY bg.grp, i.week_date
    ),
    joined AS (
      SELECT r.grp, r.week_date, r.date_eff, r.sE, r.n_obs_252, r.n_ret_members,
             m.market_cap, m.assets, m.equity, m.bs_quarter_end,
             m.n_members, m.n_members_bs, m.bs_cap_coverage,
             CASE WHEN m.bs_cap_coverage >= {BS_MIN_CAP_COVERAGE}
                  THEN m.total_liab END AS total_liab,
             fw.r_decimal AS r
      FROM resampled r
      LEFT JOIN members m ON m.grp = r.grp AND m.week_date = r.week_date
      LEFT JOIN fred_weekly fw ON fw.week_date = r.week_date
    )
    SELECT grp, week_date, date_eff, market_cap, sE, n_obs_252, r,
           bs_quarter_end, total_liab, assets, equity,
           CASE WHEN total_liab IS NULL OR total_liab <= 0 OR market_cap IS NULL
                THEN NULL ELSE market_cap / total_liab END AS E_scaled,
           n_members, n_members_bs, bs_cap_coverage, n_ret_members
    FROM joined
    WHERE n_members >= {MIN_MEMBERS}
    """)
    return int(conn.execute("SELECT COUNT(*) FROM group_input").fetchone()[0])


def assemble_group_inputs(
    conn: duckdb.DuckDBPyConnection,
    *,
    week_date_min: Optional[str] = None,
    exclude_existing: bool = True,
) -> pd.DataFrame:
    """Compute-ready group rows, same strict kernel filter as the members."""
    wheres = ["sE IS NOT NULL", "market_cap IS NOT NULL", "market_cap > 0",
              "total_liab IS NOT NULL", "total_liab > 0", "r IS NOT NULL"]
    params: list = []
    if week_date_min:
        wheres.append("week_date >= ?")
        params.append(week_date_min)
    excl = ("AND (week_date, grp) NOT IN (SELECT week_date, grp FROM group_panel)"
            if exclude_existing else "")
    df = conn.execute(f"""
        SELECT grp, week_date, date_eff,
               EXTRACT(year  FROM week_date)::INTEGER AS year,
               EXTRACT(month FROM week_date)::INTEGER AS month,
               r, sE, market_cap AS market_cap_raw, total_liab,
               E_scaled AS E, n_members
        FROM group_input
        WHERE {' AND '.join(wheres)} {excl}
        ORDER BY grp, week_date
    """, params).fetchdf()
    if not df.empty:
        # the kernel is keyed on permco; give each group a stable synthetic one
        df["permco"] = df["grp"].map(group_id).astype("int64")
        df["rssd"] = df["permco"]
        df["ticker"] = df["grp"]
    return df


def upsert_group_panel(conn: duckdb.DuckDBPyConnection, results: pd.DataFrame) -> int:
    """Insert-or-replace group results keyed (week_date, grp).

    n_members does not survive the kernel (it only carries preserve_columns),
    so it is re-joined from group_input. It has to be on the panel: a step in a
    sector PD is either risk or a member arriving, and only this column tells
    the reader which."""
    if results.empty:
        return 0
    df = results.copy()
    df["np_PD"] = df["mdef"]
    df["E_scaled"] = df["E"]
    df["week_date"] = pd.to_datetime(df["week_date"]).dt.date
    df["grp"] = df["ticker"]
    if "n_members" not in df.columns:
        nm = conn.execute(
            "SELECT grp, week_date, n_members FROM group_input").fetchdf()
        nm["week_date"] = pd.to_datetime(nm["week_date"]).dt.date
        df = df.merge(nm, on=["grp", "week_date"], how="left")
    cols = ["week_date", "grp", "n_members", "total_liab", "market_cap_raw",
            "E_scaled", "sE", "r", "L", "B", "mdef", "fs", "bookF",
            "merton_PD", "np_PD", "L_fallback_used", "fs_fallback_used",
            "B_fallback_used", "bookF_fallback_used", "mdef_fallback_used"]
    for c in cols:
        if c not in df.columns:
            df[c] = pd.NA
    df = df[cols]
    conn.register("_grp_new", df)
    try:
        conn.execute("DELETE FROM group_panel WHERE (week_date, grp) IN "
                     "(SELECT week_date, grp FROM _grp_new)")
        conn.execute(f"INSERT INTO group_panel ({', '.join(cols)}) "
                     f"SELECT {', '.join(cols)} FROM _grp_new")
        return int(len(df))
    finally:
        conn.unregister("_grp_new")


def build_and_compute(
    conn: duckdb.DuckDBPyConnection,
    *,
    recompute_since: Optional[_date] = None,
    log=lambda m: None,
) -> int:
    """Standalone rebuild + compute of the group panel.

    The CLI does not use this - it folds the group rows into the same kernel
    call as the member banks, because the Delaunay build is a fixed cost per
    call. This exists for backfilling groups on a store whose member panel is
    already current, and pays that cost once."""
    n_d = build_group_daily(conn)
    log(f"group_daily: {n_d} row(s)")
    n_i = build_group_input(conn)
    log(f"group_input: {n_i} row(s)")
    df = assemble_group_inputs(
        conn,
        week_date_min=recompute_since.isoformat() if recompute_since else None,
        exclude_existing=recompute_since is None,
    )
    if df.empty:
        log("group compute: nothing to do")
        return 0
    log(f"group compute: {len(df)} row(s)")
    n = upsert_group_panel(conn, compute_mod.run_compute(df))
    log(f"group_panel: upserted {n} row(s)")
    return n
