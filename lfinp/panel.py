"""
Build the weekly compute-ready input panel (pd_input).

Ported from bank-pd's weekly.build_pd_input with the same semantics —
Friday anchor, 252-trading-day rolling vol sampled at each Friday's
date_eff, ASOF joins for link / balance sheet / rate — narrowed to the
equity_daily view and the configured bank scope. The pre-Yahoo-era output
must be bit-identical to bank-pd's (verified by the parity gate in the
build order); do not "improve" the SQL casually.
"""
from __future__ import annotations

from datetime import date as _date
from typing import Optional

import duckdb

from . import config
from .db import attach_external, detach

EQUITY_STALE_DAYS = 7    # market data older than this vs the Friday -> NULL'd


def _friday_calendar_sql(start_date: str, end_date: str) -> str:
    return f"""
        SELECT week_date FROM (
          SELECT range AS week_date
          FROM range(
            DATE '{start_date}',
            DATE '{end_date}' + INTERVAL 1 DAY,
            INTERVAL 1 DAY
          )
        )
        WHERE EXTRACT(dow FROM week_date) = 5  -- Friday
    """


def build_fred_weekly(
    conn: duckdb.DuckDBPyConnection,
    *,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> int:
    """Friday-anchored spot DGS10 via ASOF (last reading <= Friday).
    End is today so the newest Friday still gets a forward-filled r."""
    start = start_date or config.START_DATE
    end = end_date or _date.today().strftime("%Y-%m-%d")
    row = conn.execute("SELECT COUNT(*) FROM fred_dgs10").fetchone()
    if not row or not row[0]:
        return 0
    sql = f"""
    WITH fridays AS (
      {_friday_calendar_sql(start, end)}
    )
    SELECT f.week_date, d.r_decimal
    FROM fridays f
    ASOF LEFT JOIN (
      SELECT date, r_decimal FROM fred_dgs10 WHERE r_decimal IS NOT NULL
    ) d ON f.week_date >= d.date
    WHERE d.date IS NOT NULL
    """
    conn.execute("DELETE FROM fred_weekly")
    conn.execute("INSERT INTO fred_weekly (week_date, r_decimal) " + sql)
    return int(conn.execute("SELECT COUNT(*) FROM fred_weekly").fetchone()[0])


def build_pd_input(
    conn: duckdb.DuckDBPyConnection,
    permcos: list[int],
    *,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> int:
    """Rebuild pd_input for the given permcos.

    Anchored to every Friday from each permco's first equity_daily date to
    end_date (default today). Market data older than EQUITY_STALE_DAYS vs
    the Friday is NULL'd (row kept, equity_stale=TRUE). Balance sheet:
    Y-9C primary, FFIEC Call Reports fallback, ASOF forward-filled,
    bs_stale flagged past Y9C_STALE_DAYS. Hard filter: rssd IS NOT NULL."""
    if not permcos:
        return 0
    start = start_date or config.START_DATE
    end = end_date or _date.today().strftime("%Y-%m-%d")
    permco_str = ",".join(str(int(p)) for p in permcos)

    attach_external(conn, "ext_y9c", config.y9c_db_path())
    cr_attached = False
    try:
        try:
            attach_external(conn, "ext_cr", config.call_reports_db_path())
            cr_attached = True
        except FileNotFoundError:
            pass

        sql = f"""
        WITH scoped_daily AS (
          SELECT * FROM equity_daily WHERE permco IN ({permco_str})
        ),
        daily_with_vol AS (
          SELECT
            permco, date, price, market_cap, source AS data_source,
            STDDEV_SAMP(retx) OVER (
              PARTITION BY permco ORDER BY date
              ROWS BETWEEN {config.VOL_WINDOW - 1} PRECEDING AND CURRENT ROW
            ) * sqrt(252) AS sE_raw,
            COUNT(retx) OVER (
              PARTITION BY permco ORDER BY date
              ROWS BETWEEN {config.VOL_WINDOW - 1} PRECEDING AND CURRENT ROW
            ) AS n_obs_252
          FROM scoped_daily
        ),
        permco_bounds AS (
          SELECT permco, MIN(date) AS first_date FROM scoped_daily GROUP BY permco
        ),
        fridays AS (
          {_friday_calendar_sql(start, end)}
        ),
        permco_fridays AS (
          SELECT pb.permco, f.week_date
          FROM permco_bounds pb
          JOIN fridays f ON f.week_date >= pb.first_date
        ),
        resampled AS (
          SELECT pf.permco, pf.week_date,
                 d.date AS date_eff, d.price, d.market_cap, d.data_source,
                 CASE WHEN d.n_obs_252 >= {config.VOL_MIN_PERIODS}
                      THEN d.sE_raw END AS sE,
                 d.n_obs_252
          FROM permco_fridays pf
          ASOF LEFT JOIN daily_with_vol d
            ON d.permco = pf.permco AND pf.week_date >= d.date
        ),
        with_link AS (
          SELECT r.*, l.rssd
          FROM resampled r
          ASOF LEFT JOIN link l
            ON l.permco = r.permco AND r.week_date >= l.quarter_end
        ),
        ticker_ranked AS (
          SELECT wl.permco, wl.week_date, t.ticker,
                 ROW_NUMBER() OVER (
                   PARTITION BY wl.permco, wl.week_date
                   ORDER BY t.namedt DESC, t.nameenddt DESC, t.ticker ASC
                 ) AS rn
          FROM with_link wl
          JOIN ticker_hist t
            ON t.permco = wl.permco AND t.namedt <= wl.week_date
        ),
        with_ticker AS (
          SELECT wl.*, tr.ticker
          FROM with_link wl
          LEFT JOIN ticker_ranked tr
            ON tr.permco = wl.permco AND tr.week_date = wl.week_date AND tr.rn = 1
        ),
        with_y9c AS (
          SELECT wt.*,
                 yp.date AS y9c_quarter_end,
                 CAST(yp.total_liab AS DOUBLE) AS y9c_total_liab,
                 CAST(yp.assets     AS DOUBLE) AS y9c_assets,
                 CAST(yp.equity     AS DOUBLE) AS y9c_equity
          FROM with_ticker wt
          ASOF LEFT JOIN ext_y9c.bs_panel_y9c yp
            ON yp.id_rssd = wt.rssd AND wt.week_date >= yp.date
        ),
        with_cr AS (
          SELECT wy.*,
            {"cr.date AS cr_quarter_end," if cr_attached else "CAST(NULL AS DATE) AS cr_quarter_end,"}
            {"CAST(cr.total_liab AS DOUBLE) AS cr_total_liab," if cr_attached else "CAST(NULL AS DOUBLE) AS cr_total_liab,"}
            {"CAST(cr.assets    AS DOUBLE) AS cr_assets," if cr_attached else "CAST(NULL AS DOUBLE) AS cr_assets,"}
            {"CAST(cr.equity    AS DOUBLE) AS cr_equity" if cr_attached else "CAST(NULL AS DOUBLE) AS cr_equity"}
          FROM with_y9c wy
          {"ASOF LEFT JOIN ext_cr.bs_panel cr ON cr.id_rssd = wy.rssd AND wy.week_date >= cr.date" if cr_attached else ""}
        ),
        with_bs AS (
          SELECT
            * EXCLUDE (y9c_quarter_end, y9c_total_liab, y9c_assets, y9c_equity,
                       cr_quarter_end,  cr_total_liab,  cr_assets,  cr_equity),
            CASE WHEN y9c_total_liab IS NOT NULL THEN 'y9c'
                 WHEN cr_total_liab  IS NOT NULL THEN 'call_report'
            END AS bs_source,
            COALESCE(y9c_quarter_end, cr_quarter_end) AS bs_quarter_end,
            COALESCE(y9c_total_liab,  cr_total_liab)  AS total_liab,
            COALESCE(y9c_assets,      cr_assets)      AS assets,
            COALESCE(y9c_equity,      cr_equity)      AS equity
          FROM with_cr
        ),
        joined AS (
          SELECT wb.*, fw.r_decimal AS r
          FROM with_bs wb
          LEFT JOIN fred_weekly fw USING (week_date)
        ),
        flagged AS (
          SELECT *,
            CASE WHEN date_eff IS NOT NULL
                 THEN date_diff('day', date_eff, week_date) END AS equity_lag_days,
            CASE WHEN bs_quarter_end IS NOT NULL
                 THEN date_diff('day', bs_quarter_end, week_date) END AS bs_age_days
          FROM joined
        )
        SELECT
          permco, week_date, date_eff, rssd, ticker,
          CASE WHEN equity_lag_days IS NULL OR equity_lag_days > {EQUITY_STALE_DAYS}
               THEN NULL ELSE market_cap END AS market_cap,
          CASE WHEN equity_lag_days IS NULL OR equity_lag_days > {EQUITY_STALE_DAYS}
               THEN NULL ELSE price END AS price,
          CASE WHEN equity_lag_days IS NULL OR equity_lag_days > {EQUITY_STALE_DAYS}
               THEN NULL ELSE sE END AS sE,
          CASE WHEN equity_lag_days IS NULL OR equity_lag_days > {EQUITY_STALE_DAYS}
               THEN NULL ELSE n_obs_252 END AS n_obs_252,
          r,
          bs_quarter_end, total_liab, assets, equity,
          CASE
            WHEN total_liab IS NULL OR total_liab <= 0 THEN NULL
            WHEN equity_lag_days IS NULL OR equity_lag_days > {EQUITY_STALE_DAYS} THEN NULL
            ELSE market_cap / total_liab
          END AS E_scaled,
          bs_age_days,
          (bs_age_days IS NOT NULL AND bs_age_days > {config.Y9C_STALE_DAYS}) AS bs_stale,
          equity_lag_days,
          (equity_lag_days IS NULL OR equity_lag_days > {EQUITY_STALE_DAYS}) AS equity_stale,
          data_source,
          bs_source
        FROM flagged
        WHERE rssd IS NOT NULL
        """

        conn.execute("DELETE FROM pd_input")
        conn.execute(
            """
            INSERT INTO pd_input (
              permco, week_date, date_eff, rssd, ticker,
              market_cap, price, sE, n_obs_252, r,
              bs_quarter_end, total_liab, assets, equity, E_scaled,
              bs_age_days, bs_stale, equity_lag_days, equity_stale,
              data_source, bs_source
            )
            """ + sql
        )
        return int(conn.execute("SELECT COUNT(*) FROM pd_input").fetchone()[0])
    finally:
        if cr_attached:
            detach(conn, "ext_cr")
        detach(conn, "ext_y9c")
