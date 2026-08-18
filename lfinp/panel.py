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


# How far around the stated closing date to look for the day the share count
# actually re-bases. The legal close and the vendor's share-count update are
# not the same day - COF closed Discover on 2025-05-18 (a Sunday) and CRSP
# carried the combined count from 2025-05-30. Bridging from the stated date
# instead would add the target's liabilities to a week that still has only the
# acquirer's equity, which is the same error with the sign flipped.
MERGER_EFFECTIVE_LOOKBACK_DAYS = 10
MERGER_EFFECTIVE_LOOKAHEAD_DAYS = 120


def _merger_liab_sql() -> str:
    """Per merger: the liabilities to add, and the week they start applying.

    target_liab - liab_override wins, since it exists for targets that file no
    Y-9C and for deals where the filed entity is not the entity acquired.
    Otherwise the target's own last filing on or before the closing, the most
    recent statement of what the acquirer took on.

    effective_date - the first day the acquirer's market cap re-bases, found
    the same way check_issuance_bs_lag finds it: (mcap_t / mcap_t-1) / (1+retx)
    departs from 1 only when the share count changed, and is immune to splits.
    Timing the bridge off the data rather than off the stated closing date
    keeps numerator and denominator changing on the same day, whatever the
    vendor does - COF closed Discover on 2025-05-18 but CRSP carried the
    combined count from 2025-05-30. The bar is MERGER_REBASE_TOL, well below
    the blind-scan threshold, because the merger row has already asserted that
    a deal happened. Earliest match wins: where a window holds more than one
    candidate the later ones are subsequent events, not this one.
    """
    return f"""
        SELECT ml.acquirer_rssd, ml.target_rssd, ml.close_date, ml.name,
               ml.target_liab, ml.target_filing,
               MIN(iss.date) AS effective_date
        FROM (
          SELECT me.acquirer_rssd, me.target_rssd, me.close_date, me.name,
                 COALESCE(
                   me.liab_override,
                   (SELECT t.total_liab
                    FROM ext_y9c.bs_panel_y9c t
                    WHERE t.id_rssd = me.target_rssd
                      AND t.date <= me.close_date
                      AND t.total_liab IS NOT NULL
                    ORDER BY t.date DESC
                    LIMIT 1)
                 ) AS target_liab,
                 CASE WHEN me.liab_override IS NULL THEN
                   (SELECT t.date
                    FROM ext_y9c.bs_panel_y9c t
                    WHERE t.id_rssd = me.target_rssd
                      AND t.date <= me.close_date
                      AND t.total_liab IS NOT NULL
                    ORDER BY t.date DESC
                    LIMIT 1)
                 END AS target_filing
          FROM merger_event me
        ) ml
        LEFT JOIN link l ON l.rssd = ml.acquirer_rssd
        LEFT JOIN (
          SELECT permco, date FROM (
            SELECT permco, date,
                   (market_cap / NULLIF(LAG(market_cap) OVER w, 0))
                   / NULLIF(1 + retx, 0) AS ratio
            FROM equity_daily
            WHERE market_cap IS NOT NULL AND retx IS NOT NULL
            WINDOW w AS (PARTITION BY permco ORDER BY date)
          )
          WHERE ratio IS NOT NULL AND ratio - 1 > {config.MERGER_REBASE_TOL}
        ) iss
          ON iss.permco = l.permco
         AND iss.date >= ml.close_date - INTERVAL {MERGER_EFFECTIVE_LOOKBACK_DAYS} DAY
         AND iss.date <= ml.close_date + INTERVAL {MERGER_EFFECTIVE_LOOKAHEAD_DAYS} DAY
        GROUP BY ml.acquirer_rssd, ml.target_rssd, ml.close_date, ml.name,
                 ml.target_liab, ml.target_filing
    """


def check_mergers_resolvable(
    conn: duckdb.DuckDBPyConnection,
    permcos: list[int],
) -> list[tuple]:
    """Mergers in scope that cannot be turned into a bridge.

    Three ways to fail, all config errors rather than data conditions:
    no target liabilities (target files nothing and no liab= was given); no
    effective date (the acquirer's share count never re-bases near the stated
    close, so either the date is wrong or the deal was cash-only and does not
    belong here); or a target whose last filing is far older than the close,
    which means the RSSD names an entity that had stopped filing under its own
    charter. Each bridges nothing, or bridges the wrong number, while looking
    like it worked - so the caller aborts. Must run with ext_y9c attached."""
    if not permcos:
        return []
    permco_str = ",".join(str(int(p)) for p in permcos)
    return conn.execute(
        f"""
        WITH ml AS ({_merger_liab_sql()})
        SELECT acquirer_rssd, close_date, target_rssd, name,
               CASE WHEN target_liab IS NULL THEN 'no target liabilities'
                    WHEN target_filing IS NOT NULL
                     AND date_diff('day', target_filing, close_date)
                         > {config.MERGER_TARGET_STALE_DAYS}
                      THEN 'target last filed ' || target_filing
                           || ', stale at close - wrong RSSD?'
                    ELSE 'no share-count change near close' END AS reason
        FROM ml
        WHERE (target_liab IS NULL
               OR effective_date IS NULL
               OR (target_filing IS NOT NULL
                   AND date_diff('day', target_filing, close_date)
                       > {config.MERGER_TARGET_STALE_DAYS}))
          AND acquirer_rssd IN (
            SELECT DISTINCT rssd FROM link WHERE permco IN ({permco_str})
          )
        ORDER BY close_date
        """
    ).fetchall()


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

        unresolved = check_mergers_resolvable(conn, permcos)
        if unresolved:
            rows = "; ".join(
                f"acquirer {a} close {c} target {t} ({n}): {why}"
                for a, c, t, n, why in unresolved
            )
            raise ValueError(
                f"{len(unresolved)} merger(s) in mergers.txt cannot be bridged: "
                f"{rows}. Add an explicit liab= for a target that files nothing, "
                "correct the close= date, or remove the line. Bridging nothing "
                "silently would leave the PD understated while looking corrected.")

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
        -- Pro-forma merger bridge. Bridge exactly the weeks where the target
        -- is in the numerator but not yet the denominator. The two sides
        -- arrive on different clocks and must be tested separately:
        --   equity in       -> week_date >= effective_date (the day the share
        --                      count actually re-based, from the data)
        --   liabilities out -> bs_quarter_end < close_date (a filing dated on
        --                      or after the closing already consolidates it)
        -- Testing only the effective date would double-count whenever the
        -- vendor's share count lags past the next filing: Huntington closed
        -- Veritex on 2025-10-20, CRSP never picked it up, and the combined
        -- count first appears on 2026-01-02 - by which time the 2025-12-31
        -- Y-9C already contains Veritex's liabilities.
        -- SUM because a bank can close two deals inside one quarter.
        merger_liab AS ({_merger_liab_sql()}),
        bridge AS (
          SELECT wb.permco, wb.week_date, SUM(ml.target_liab) AS bridge_liab
          FROM with_bs wb
          JOIN merger_liab ml
            ON ml.acquirer_rssd = wb.rssd
           AND ml.effective_date <= wb.week_date
           AND ml.close_date     >  wb.bs_quarter_end
          WHERE ml.target_liab IS NOT NULL AND ml.effective_date IS NOT NULL
            AND wb.total_liab IS NOT NULL
          GROUP BY wb.permco, wb.week_date
        ),
        with_bridge AS (
          SELECT wb.* EXCLUDE (total_liab, bs_source),
                 wb.total_liab AS total_liab_reported,
                 wb.total_liab + COALESCE(br.bridge_liab, 0) AS total_liab,
                 (br.bridge_liab IS NOT NULL) AS bs_bridged,
                 CASE WHEN br.bridge_liab IS NOT NULL AND wb.bs_source IS NOT NULL
                      THEN wb.bs_source || '+proforma'
                      ELSE wb.bs_source END AS bs_source
          FROM with_bs wb
          LEFT JOIN bridge br USING (permco, week_date)
        ),
        joined AS (
          SELECT wb.*, fw.r_decimal AS r
          FROM with_bridge wb
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
          bs_quarter_end, total_liab, total_liab_reported, bs_bridged,
          assets, equity,
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
              bs_quarter_end, total_liab, total_liab_reported, bs_bridged,
              assets, equity, E_scaled,
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
