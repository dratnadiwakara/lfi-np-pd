"""
Accuracy gates. Each returns a CheckResult; callers decide abort semantics
per the table in the README. Everything is logged to check_log.

  1 share_jumps        import, pre-insert   abort on recent unexplained jump
  2 crsp_xval          import (when overlap) abort permco on failure
  3 pull_diff          import               never blocks; drives recompute
  4 level_continuity   import               whole-import abort on FAIL
  5 coverage           post-import          abort naming missing permco
  6 pd_plausibility    post-compute         warn (exit 1 under --strict)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Optional

import duckdb
import numpy as np
import pandas as pd

from . import config
from .db import log_check
from .yf import PullResult


@dataclass
class CheckResult:
    name: str
    passed: bool
    summary: str
    details: pd.DataFrame = field(default_factory=pd.DataFrame)

    def log(self, conn: duckdb.DuckDBPyConnection) -> "CheckResult":
        log_check(conn, self.name, self.passed, self.summary)
        return self


def _table_exists(conn: duckdb.DuckDBPyConnection, name: str) -> bool:
    """Read-only connections cannot create tables, so checks must tolerate a
    store that predates them rather than crashing the diagnostic."""
    row = conn.execute(
        "SELECT COUNT(*) FROM information_schema.tables WHERE table_name = ?",
        [name],
    ).fetchone()
    return bool(row and row[0])


def _normalize_dates(df: pd.DataFrame, *cols: str) -> pd.DataFrame:
    """fetchdf returns datetime64; flags are compared, acked and printed as
    plain dates, so normalize once at the boundary."""
    if not len(df):
        return df
    out = df.copy()
    for c in cols:
        if c in out.columns:
            out[c] = [None if pd.isna(v) else pd.Timestamp(v).date()
                      for v in out[c]]
    return out


def suppress_acked(
    conn: duckdb.DuckDBPyConnection,
    check_name: str,
    details: pd.DataFrame,
    *,
    date_col: str = "ref_date",
) -> pd.DataFrame:
    """Drop rows an operator has already reviewed and explained.

    Without this, every real crisis re-fires on every run and the checks
    become noise the operator scrolls past - which is how a genuine
    artifact slips through. Anything still listed after suppression is by
    construction unexplained."""
    if not len(details) or "rssd" not in details.columns:
        return details
    if not _table_exists(conn, "flag_ack"):
        return details          # read-only DB predating the table: nothing acked
    acked = conn.execute(
        "SELECT rssd, ref_date FROM flag_ack WHERE check_name = ?",
        [check_name],
    ).fetchdf()
    if not len(acked):
        return details
    keys = {(int(r.rssd), pd.Timestamp(r.ref_date).date())
            for r in acked.itertuples()}
    mask = [
        (int(r[1]["rssd"]), pd.Timestamp(r[1][date_col]).date()) not in keys
        for r in details.iterrows()
    ]
    return details[mask]


# -- 1: share jumps ---------------------------------------------------------------


def check_share_jumps(pulls: dict[int, PullResult]) -> CheckResult:
    """Consumes the repair metadata produced by yf.repair_share_glitches.
    Fails when any permco has an unexplained share jump in the last 5
    trading days (too recent to distinguish M&A from vendor error)."""
    rows = []
    failed = []
    for permco, pr in pulls.items():
        for rep in pr.shares_repairs:
            rows.append({"permco": permco, "ticker": pr.ticker,
                         "date": rep["date"], "kind": "repaired",
                         "old": rep["old"], "new": rep["new"]})
        if pr.unexplained_recent_jump is not None:
            j = pr.unexplained_recent_jump
            rows.append({"permco": permco, "ticker": pr.ticker,
                         "date": j["date"], "kind": "RECENT_UNEXPLAINED",
                         "old": None, "new": j["ratio"]})
            failed.append(f"{pr.ticker}(permco {permco}) x{j['ratio']:.3f} at {j['date']}")
    details = pd.DataFrame(rows)
    if failed:
        return CheckResult(
            "share_jumps", False,
            f"unexplained share jump within last 5 trading days: {'; '.join(failed)}. "
            f"Too recent to verify persistence - refusing to guess. "
            f"Re-run in a few days or pass --allow-share-jump PERMCO=DATE.",
            details,
        )
    n_rep = sum(len(p.shares_repairs) for p in pulls.values())
    return CheckResult("share_jumps", True,
                       f"OK ({n_rep} vendor-glitch day(s) repaired)", details)


# -- 2: CRSP-overlap return cross-validation ------------------------------------------


def check_crsp_xval(
    conn: duckdb.DuckDBPyConnection,
    pulls: dict[int, PullResult],
) -> CheckResult:
    """Where the pull window overlaps CRSP coverage, the pull's price
    returns must reproduce CRSP retx (same quantity: split-adjusted
    ex-dividend price return). A mismatch means the Yahoo symbol is not the
    CRSP security (rename/reuse/wrong share class)."""
    rows = []
    failed = []
    skipped = []
    for permco, pr in pulls.items():
        new = pr.df.reset_index()[["date", "retx"]].rename(columns={"retx": "retx_yf"})
        conn.register("_xval_new", new)
        try:
            m = conn.execute(
                """
                SELECT c.date, c.retx AS retx_crsp, n.retx_yf
                FROM crsp_daily c
                JOIN _xval_new n ON n.date = c.date
                WHERE c.permco = ? AND c.retx IS NOT NULL AND n.retx_yf IS NOT NULL
                ORDER BY c.date
                """,
                [permco],
            ).fetchdf()
        finally:
            conn.unregister("_xval_new")
        if len(m) < config.XVAL_MIN_OVERLAP_DAYS:
            skipped.append(f"{pr.ticker}({len(m)}d)")
            continue
        corr = float(np.corrcoef(m["retx_crsp"], m["retx_yf"])[0, 1])
        big = float(
            ((m["retx_crsp"] - m["retx_yf"]).abs() > config.XVAL_BIG_DIFF_BP / 1e4).mean()
        )
        ok = corr >= config.XVAL_MIN_CORR and big <= config.XVAL_MAX_BIG_DIFF_SHARE
        rows.append({"permco": permco, "ticker": pr.ticker, "overlap_days": len(m),
                     "corr": corr, "big_diff_share": big, "passed": ok})
        if not ok:
            failed.append(f"{pr.ticker}(permco {permco}) corr={corr:.4f} big={big:.2%}")
    details = pd.DataFrame(rows)
    if failed:
        return CheckResult(
            "crsp_xval", False,
            f"pull returns do not reproduce CRSP retx: {'; '.join(failed)} "
            f"- wrong symbol / share class / reused ticker.",
            details,
        )
    note = f"; no overlap for {', '.join(skipped)}" if skipped else ""
    return CheckResult("crsp_xval", True,
                       f"OK ({len(rows)} permcos validated{note})", details)


# -- 3: re-pull diff (never blocks) ----------------------------------------------------


def record_pull_diff(
    conn: duckdb.DuckDBPyConnection,
    diff: pd.DataFrame,
) -> CheckResult:
    """Persist the diff and derive recompute spans: any changed day shifts
    sE for every Friday whose 252-day window includes it -> recompute
    [earliest change, earliest change + 372 calendar days] per permco."""
    if len(diff):
        conn.register("_pull_diff_new", diff)
        try:
            conn.execute(
                """
                INSERT INTO pull_diff (run_at, permco, date, field, old_value, new_value)
                SELECT CURRENT_TIMESTAMP, permco, date, field, old, new
                FROM _pull_diff_new
                """
            )
        finally:
            conn.unregister("_pull_diff_new")
    spans = (
        diff.groupby("permco")["date"].min().to_dict() if len(diff) else {}
    )
    summary = (
        f"{len(diff)} changed value(s) across {len(spans)} permco(s)"
        if len(diff) else "no changes vs previous pull"
    )
    return CheckResult("pull_diff", True, summary, diff)


def recompute_spans(diff: pd.DataFrame) -> dict[int, tuple[date, date]]:
    """permco -> (first_week, last_week) needing pd_panel recompute."""
    out: dict[int, tuple[date, date]] = {}
    if not len(diff):
        return out
    for permco, first in diff.groupby("permco")["date"].min().items():
        d0 = pd.Timestamp(first).date()
        out[int(permco)] = (d0, min(d0 + timedelta(days=372), date.today()))
    return out


# -- 4: level continuity -----------------------------------------------------------------


def check_level_continuity(
    conn: duckdb.DuckDBPyConnection,
    pulls: dict[int, PullResult],
) -> CheckResult:
    """Pull market_cap vs whatever equity_daily already holds over the full
    overlap (CRSP rows and prior Yahoo rows). Catches wrong-share-basis
    problems (observed: Yahoo halving an MHC's share count) that pure
    return checks cannot see."""
    rows = []
    failed = []
    for permco, pr in pulls.items():
        new = pr.df.reset_index()[["date", "market_cap"]].rename(
            columns={"market_cap": "mcap_new"})
        conn.register("_lvl_new", new)
        try:
            m = conn.execute(
                """
                SELECT e.date, e.market_cap AS mcap_old, n.mcap_new
                FROM equity_daily e
                JOIN _lvl_new n ON n.date = e.date
                WHERE e.permco = ? AND e.market_cap IS NOT NULL
                  AND n.mcap_new IS NOT NULL
                ORDER BY e.date
                """,
                [permco],
            ).fetchdf()
        finally:
            conn.unregister("_lvl_new")
        if not len(m):
            rows.append({"permco": permco, "ticker": pr.ticker, "overlap_days": 0,
                         "median_diff": None, "days_over_fail": 0, "verdict": "NO_OVERLAP"})
            continue
        rel = (m["mcap_new"] / m["mcap_old"] - 1).abs()
        med = float(rel.median())
        n_fail_days = int((rel > config.LEVEL_FAIL).sum())
        verdict = "OK"
        if n_fail_days > config.LEVEL_FAIL_MAX_DAYS:
            verdict = "FAIL"
            failed.append(
                f"{pr.ticker}(permco {permco}) median={med:.2%}, "
                f"{n_fail_days} day(s) beyond {config.LEVEL_FAIL:.0%}"
            )
        elif med > config.LEVEL_WARN:
            verdict = "WARN"
        rows.append({"permco": permco, "ticker": pr.ticker, "overlap_days": len(m),
                     "median_diff": med, "days_over_fail": n_fail_days,
                     "verdict": verdict})
    details = pd.DataFrame(rows)
    if failed:
        return CheckResult(
            "level_continuity", False,
            f"market-cap level break vs stored data: {'; '.join(failed)} "
            f"- share-basis / wrong-company problem. Nothing inserted.",
            details,
        )
    n_warn = int((details["verdict"] == "WARN").sum()) if len(details) else 0
    return CheckResult("level_continuity", True,
                       f"OK ({n_warn} soft warn(s))", details)


# -- 5: coverage -----------------------------------------------------------------------------


def check_coverage(
    conn: duckdb.DuckDBPyConnection,
    expected_permcos: list[int],
    *,
    as_of: Optional[date] = None,
) -> CheckResult:
    """Every live bank must have an equity_daily row within
    COVERAGE_MAX_LAG_BDAYS business days of today. Catches silent stalls
    (delisting, unnoticed ticker rename, guard misfire)."""
    today = as_of or date.today()
    cutoff = np.busday_offset(
        np.datetime64(today, "D"), -config.COVERAGE_MAX_LAG_BDAYS,
        roll="backward",
    ).astype("datetime64[D]").astype(object)
    if not expected_permcos:
        return CheckResult("coverage", True, "no live banks configured")
    placeholders = ",".join(str(int(p)) for p in expected_permcos)
    rows = conn.execute(
        f"""
        SELECT p.permco, MAX(e.date) AS last_date
        FROM (SELECT UNNEST(ARRAY[{placeholders}]) AS permco) p
        LEFT JOIN equity_daily e USING (permco)
        GROUP BY p.permco
        """
    ).fetchdf()
    rows["stale"] = rows["last_date"].isna() | (
        pd.to_datetime(rows["last_date"]) < pd.Timestamp(cutoff)
    )
    missing = rows[rows["stale"]]
    if len(missing):
        names = ", ".join(
            f"permco {int(r.permco)} (last {r.last_date})" for r in missing.itertuples()
        )
        return CheckResult(
            "coverage", False,
            f"{len(missing)} live bank(s) lack equity data within "
            f"{config.COVERAGE_MAX_LAG_BDAYS} business days: {names}",
            rows,
        )
    return CheckResult("coverage", True,
                       f"OK (all {len(expected_permcos)} live banks current)", rows)


# -- 6: PD plausibility ------------------------------------------------------------------------


def check_pd_plausibility(
    conn: duckdb.DuckDBPyConnection,
    rssds: list[int],
    *,
    since: Optional[date] = None,
) -> CheckResult:
    """Week-over-week np_PD ratio outside [1/PD_JUMP_FACTOR, PD_JUMP_FACTOR]
    or sE ratio above SE_JUMP_FACTOR on any in-scope bank. Real credit
    deterioration can trip this - it is a warn, not an abort."""
    if not rssds:
        return CheckResult("pd_plausibility", True, "no banks in scope")
    placeholders = ",".join(str(int(r)) for r in rssds)
    since_clause = f"AND week_date >= DATE '{since.isoformat()}'" if since else ""
    df = conn.execute(
        f"""
        WITH series AS (
          SELECT rssd, week_date, np_PD, sE,
                 LAG(np_PD) OVER (PARTITION BY rssd ORDER BY week_date) AS prev_pd,
                 LAG(sE)    OVER (PARTITION BY rssd ORDER BY week_date) AS prev_se
          FROM pd_panel
          WHERE rssd IN ({placeholders}) {since_clause}
        )
        SELECT rssd, week_date, prev_pd, np_PD, prev_se, sE,
               np_PD / NULLIF(prev_pd, 0) AS pd_ratio,
               sE / NULLIF(prev_se, 0)    AS se_ratio
        FROM series
        WHERE (prev_pd > 0.001 AND (np_PD / prev_pd > {config.PD_JUMP_FACTOR}
                                    OR np_PD / prev_pd < {1.0 / config.PD_JUMP_FACTOR}))
           OR (prev_se > 0 AND sE / prev_se > {config.SE_JUMP_FACTOR})
        ORDER BY week_date, rssd
        """
    ).fetchdf()
    df = _normalize_dates(df.rename(columns={"week_date": "ref_date"}), "ref_date")
    df = suppress_acked(conn, "pd_plausibility", df)
    if len(df):
        return CheckResult(
            "pd_plausibility", False,
            f"{len(df)} unexplained week-over-week move(s) - inspect before use",
            df,
        )
    return CheckResult("pd_plausibility", True, "OK (no unexplained moves)", df)


# -- 7: sE step detector with attribution ------------------------------------------


def check_se_steps(
    conn: duckdb.DuckDBPyConnection,
    rssds: list[int],
    *,
    since: Optional[date] = None,
) -> CheckResult:
    """The signature check for this failure class.

    Volatility over a 252-day window moves one observation at a time, so a
    genuine regime change ramps. A one-week sE step beyond
    SE_STEP_THRESHOLD means a single enormous return ENTERED the window
    (step up) or EXITED it (cliff down). Both are attributed here: the
    culprit day, its return, its provenance flags, and how far it sat from
    that day's peer median. That is the difference between "inspect this"
    and "COF's +65.8% on 2025-05-20 while peers were flat did it"."""
    if not rssds:
        return CheckResult("se_steps", True, "no banks in scope")
    placeholders = ",".join(str(int(r)) for r in rssds)
    since_clause = f"AND p.week_date >= DATE '{since.isoformat()}'" if since else ""
    steps = conn.execute(
        f"""
        WITH s AS (
          SELECT p.rssd, p.permco, p.week_date, p.sE, i.date_eff,
                 LAG(p.sE)       OVER w AS prev_se,
                 LAG(p.week_date) OVER w AS prev_week,
                 LAG(i.date_eff)  OVER w AS prev_eff
          FROM pd_panel p
          JOIN pd_input i ON i.permco = p.permco AND i.week_date = p.week_date
          WHERE p.rssd IN ({placeholders}) {since_clause}
          WINDOW w AS (PARTITION BY p.rssd ORDER BY p.week_date)
        )
        SELECT * FROM (
          SELECT *, sE / NULLIF(prev_se, 0) AS se_ratio FROM s
          WHERE prev_se > 0
        )
        WHERE se_ratio > {1 + config.SE_STEP_THRESHOLD}
           OR se_ratio < {1 / (1 + config.SE_STEP_THRESHOLD)}
        ORDER BY week_date
        """
    ).fetchdf()
    if not len(steps):
        return CheckResult("se_steps", True, "OK (no abrupt sE steps)")

    rows = []
    for st in steps.itertuples():
        culprit = _attribute_se_step(
            conn, int(st.permco), st.prev_eff, st.date_eff, rssds,
        )
        rows.append({
            "rssd": int(st.rssd),
            "ref_date": st.week_date,
            "prev_se": st.prev_se, "sE": st.sE, "se_ratio": st.se_ratio,
            **culprit,
        })
    details = _normalize_dates(pd.DataFrame(rows), "ref_date", "culprit_date")
    details = suppress_acked(conn, "se_steps", details)
    if not len(details):
        return CheckResult("se_steps", True, "OK (all sE steps reviewed)")
    idio = int((details["peer_gap"].abs() > config.PEER_DIVERGENCE_THRESHOLD).sum())
    return CheckResult(
        "se_steps", False,
        f"{len(details)} abrupt sE step(s), {idio} driven by a return that "
        f"diverged from peers (likely data, not market)",
        details,
    )


def _attribute_se_step(
    conn: duckdb.DuckDBPyConnection,
    permco: int,
    prev_eff,
    date_eff,
    peer_rssds: list[int],
) -> dict:
    """Which day caused it? Days between the two effective dates ENTERED the
    252-day window; the same number of days rolled off the far end and
    EXITED. Report whichever had the larger absolute return."""
    empty = {"culprit_date": None, "culprit_retx": None, "direction": None,
             "peer_median": None, "peer_gap": None, "synthetic": None,
             "source": None}
    if prev_eff is None or date_eff is None:
        return empty
    daily = conn.execute(
        "SELECT date, retx, source FROM equity_daily "
        "WHERE permco = ? AND retx IS NOT NULL ORDER BY date",
        [permco],
    ).fetchdf()
    if not len(daily):
        return empty
    daily = daily.reset_index(drop=True)
    pos = {pd.Timestamp(d).date(): i for i, d in enumerate(daily["date"])}
    i_new = pos.get(pd.Timestamp(date_eff).date())
    i_prev = pos.get(pd.Timestamp(prev_eff).date())
    if i_new is None or i_prev is None or i_new <= i_prev:
        return empty
    w = config.VOL_WINDOW
    entering = daily.iloc[i_prev + 1: i_new + 1]
    exiting = daily.iloc[max(0, i_prev + 1 - w): max(0, i_new + 1 - w)]
    cand = []
    if len(entering):
        e = entering.loc[entering["retx"].abs().idxmax()]
        cand.append(("entered", e))
    if len(exiting):
        x = exiting.loc[exiting["retx"].abs().idxmax()]
        cand.append(("exited", x))
    if not cand:
        return empty
    direction, row = max(cand, key=lambda t: abs(float(t[1]["retx"])))
    cdate = pd.Timestamp(row["date"]).date()
    peer_med = _peer_median_retx(conn, cdate, peer_rssds, exclude_permco=permco)
    retx = float(row["retx"])
    syn = conn.execute(
        "SELECT retx_synthetic FROM yf_daily WHERE permco = ? AND date = ?",
        [permco, cdate],
    ).fetchone()
    return {
        "culprit_date": cdate,
        "culprit_retx": retx,
        "direction": direction,
        "peer_median": peer_med,
        "peer_gap": None if peer_med is None else retx - peer_med,
        "synthetic": None if syn is None else bool(syn[0]),
        "source": str(row["source"]),
    }


def _peer_median_retx(
    conn: duckdb.DuckDBPyConnection,
    day: date,
    rssds: list[int],
    *,
    exclude_permco: Optional[int] = None,
) -> Optional[float]:
    if not rssds:
        return None
    placeholders = ",".join(str(int(r)) for r in rssds)
    excl = f"AND e.permco <> {int(exclude_permco)}" if exclude_permco else ""
    row = conn.execute(
        f"""
        SELECT MEDIAN(e.retx), COUNT(*) FROM equity_daily e
        WHERE e.date = ? AND e.retx IS NOT NULL {excl}
          AND e.permco IN (SELECT DISTINCT permco FROM link
                           WHERE rssd IN ({placeholders}))
        """,
        [day],
    ).fetchone()
    if row is None or row[1] is None or row[1] < config.PEER_MIN_BANKS:
        return None
    return None if row[0] is None else float(row[0])


# -- 8: peer divergence on daily returns ---------------------------------------------


def check_peer_divergence(
    conn: duckdb.DuckDBPyConnection,
    rssds: list[int],
    *,
    since: Optional[date] = None,
) -> CheckResult:
    """A market crash moves every bank; a data error moves one. Flags any
    daily return that sits far from the same-day median across the scoped
    banks. COVID-March passes (all banks down together); a share-issuance
    artifact fails (one bank +66%, peers flat)."""
    if not rssds:
        return CheckResult("peer_divergence", True, "no banks in scope")
    placeholders = ",".join(str(int(r)) for r in rssds)
    since_clause = f"AND e.date >= DATE '{since.isoformat()}'" if since else ""
    df = conn.execute(
        f"""
        WITH scoped AS (
          SELECT e.permco, e.date, e.retx, e.source
          FROM equity_daily e
          WHERE e.retx IS NOT NULL {since_clause}
            AND e.permco IN (SELECT DISTINCT permco FROM link
                             WHERE rssd IN ({placeholders}))
        ),
        peer AS (
          SELECT date, MEDIAN(retx) AS peer_median, COUNT(*) AS n_peers
          FROM scoped GROUP BY date
        )
        SELECT s.permco, s.date AS ref_date, s.retx, s.source,
               p.peer_median, p.n_peers,
               s.retx - p.peer_median AS peer_gap
        FROM scoped s JOIN peer p USING (date)
        WHERE p.n_peers >= {config.PEER_MIN_BANKS}
          AND ABS(s.retx - p.peer_median) > {config.PEER_DIVERGENCE_THRESHOLD}
        ORDER BY ABS(s.retx - p.peer_median) DESC
        """
    ).fetchdf()
    if len(df):
        rssd_map = conn.execute(
            f"""SELECT DISTINCT permco, rssd FROM link
                WHERE rssd IN ({placeholders})"""
        ).fetchdf()
        df = df.merge(rssd_map, on="permco", how="left")
        df = _normalize_dates(df, "ref_date")
        df = suppress_acked(conn, "peer_divergence", df)
    if len(df):
        return CheckResult(
            "peer_divergence", False,
            f"{len(df)} daily return(s) diverging >{config.PEER_DIVERGENCE_THRESHOLD:.0%} "
            f"from the peer median - idiosyncratic, verify against the tape",
            df,
        )
    return CheckResult("peer_divergence", True, "OK (no idiosyncratic returns)")


# -- 9: value-surface fallback rate ---------------------------------------------------


def check_fallback_rate(
    conn: duckdb.DuckDBPyConnection,
    rssds: list[int],
) -> CheckResult:
    """`mdef_fallback_used` means the NP value surface had no support at the
    query point and the kernel extrapolated. A high rate says the PD is
    interpolation, not measurement - it is the source of the PD 'flapping'
    seen at sE around 0.6-0.7."""
    if not rssds:
        return CheckResult("fallback_rate", True, "no banks in scope")
    placeholders = ",".join(str(int(r)) for r in rssds)
    df = conn.execute(
        f"""
        WITH recent AS (
          SELECT rssd, week_date, mdef_fallback_used,
                 ROW_NUMBER() OVER (PARTITION BY rssd ORDER BY week_date DESC) AS rn
          FROM pd_panel WHERE rssd IN ({placeholders})
        )
        SELECT rssd, MAX(week_date) AS ref_date, COUNT(*) AS weeks,
               AVG(CAST(COALESCE(mdef_fallback_used, 0) AS DOUBLE)) AS fallback_rate
        FROM recent WHERE rn <= {config.FALLBACK_WINDOW_WEEKS}
        GROUP BY rssd
        HAVING AVG(CAST(COALESCE(mdef_fallback_used, 0) AS DOUBLE))
               > {config.FALLBACK_RATE_THRESHOLD}
        ORDER BY fallback_rate DESC
        """
    ).fetchdf()
    df = _normalize_dates(df, "ref_date")
    df = suppress_acked(conn, "fallback_rate", df)
    if len(df):
        return CheckResult(
            "fallback_rate", False,
            f"{len(df)} bank(s) with >{config.FALLBACK_RATE_THRESHOLD:.0%} "
            f"value-surface extrapolation over the last "
            f"{config.FALLBACK_WINDOW_WEEKS} weeks",
            df,
        )
    return CheckResult("fallback_rate", True, "OK (surface support adequate)")


# -- 10: np_PD vs merton_PD decoupling -------------------------------------------------


def check_pd_decoupling(
    conn: duckdb.DuckDBPyConnection,
    rssds: list[int],
    *,
    since: Optional[date] = None,
) -> CheckResult:
    """The two PDs track loosely by construction. A sudden change in their
    RATIO while sE barely moved cannot come from the data - it points at
    the value-surface interpolation, so it is triaged differently from an
    input problem."""
    if not rssds:
        return CheckResult("pd_decoupling", True, "no banks in scope")
    placeholders = ",".join(str(int(r)) for r in rssds)
    since_clause = f"AND week_date >= DATE '{since.isoformat()}'" if since else ""
    df = conn.execute(
        f"""
        WITH s AS (
          SELECT rssd, week_date, sE, np_PD, merton_PD,
                 np_PD / NULLIF(merton_PD, 0) AS ratio,
                 LAG(np_PD / NULLIF(merton_PD, 0)) OVER w AS prev_ratio,
                 LAG(sE) OVER w AS prev_se
          FROM pd_panel
          WHERE rssd IN ({placeholders}) {since_clause}
            AND merton_PD > 0.001 AND np_PD > 0.001
          WINDOW w AS (PARTITION BY rssd ORDER BY week_date)
        )
        SELECT rssd, week_date AS ref_date, sE, prev_se, np_PD, merton_PD,
               prev_ratio, ratio
        FROM s
        WHERE prev_ratio > 0 AND prev_se > 0
          AND ABS(sE / prev_se - 1) < {config.DECOUPLE_SE_STABLE}
          AND (ratio / prev_ratio > {config.DECOUPLE_FACTOR}
               OR ratio / prev_ratio < {1 / config.DECOUPLE_FACTOR})
        ORDER BY week_date
        """
    ).fetchdf()
    df = _normalize_dates(df, "ref_date")
    df = suppress_acked(conn, "pd_decoupling", df)
    if len(df):
        return CheckResult(
            "pd_decoupling", False,
            f"{len(df)} week(s) where np_PD/merton_PD moved >{config.DECOUPLE_FACTOR}x "
            f"with sE stable - kernel/surface behaviour, not input data",
            df,
        )
    return CheckResult("pd_decoupling", True, "OK (PD measures track)")


# -- 11: balance-sheet jumps ------------------------------------------------------------


def check_bs_jumps(
    conn: duckdb.DuckDBPyConnection,
    rssds: list[int],
    *,
    since: Optional[date] = None,
) -> CheckResult:
    """total_liab enters E_scaled directly, so a bad Y-9C/Call Report row
    distorts PD exactly like a bad share count. Real mergers produce jumps
    too - those get acked once and stay suppressed."""
    if not rssds:
        return CheckResult("bs_jumps", True, "no banks in scope")
    placeholders = ",".join(str(int(r)) for r in rssds)
    since_clause = f"AND bs_quarter_end >= DATE '{since.isoformat()}'" if since else ""
    df = conn.execute(
        f"""
        WITH q AS (
          SELECT DISTINCT rssd, bs_quarter_end, total_liab, bs_source
          FROM pd_input
          WHERE rssd IN ({placeholders}) AND total_liab > 0
            AND bs_quarter_end IS NOT NULL {since_clause}
        ),
        seq AS (
          SELECT *, LAG(total_liab) OVER w AS prev_liab,
                    LAG(bs_quarter_end) OVER w AS prev_qe
          FROM q WINDOW w AS (PARTITION BY rssd ORDER BY bs_quarter_end)
        )
        SELECT rssd, bs_quarter_end AS ref_date, prev_qe, prev_liab,
               total_liab, bs_source,
               total_liab / NULLIF(prev_liab, 0) - 1 AS qoq_change
        FROM seq
        WHERE prev_liab > 0
          AND ABS(total_liab / prev_liab - 1) > {config.BS_JUMP_THRESHOLD}
        ORDER BY bs_quarter_end
        """
    ).fetchdf()
    df = _normalize_dates(df, "ref_date", "prev_qe")
    df = suppress_acked(conn, "bs_jumps", df)
    if len(df):
        return CheckResult(
            "bs_jumps", False,
            f"{len(df)} quarter-over-quarter balance-sheet jump(s) "
            f">{config.BS_JUMP_THRESHOLD:.0%} - merger or bad filing",
            df,
        )
    return CheckResult("bs_jumps", True, "OK (no balance-sheet jumps)")


# -- 12: historical panel revisions -------------------------------------------------------


def check_panel_revisions(
    conn: duckdb.DuckDBPyConnection,
) -> CheckResult:
    """What did this run silently change in already-published history?
    pull_diff covers inputs; this covers the outputs people actually cite.
    Expected to be non-empty whenever a vendor revision propagates - the
    point is that it is visible and quantified, never silent."""
    if not _table_exists(conn, "pd_panel_prev"):
        return CheckResult("panel_revisions", True, "no prior snapshot (first run)")
    n_prev = conn.execute("SELECT COUNT(*) FROM pd_panel_prev").fetchone()[0]
    if not n_prev:
        return CheckResult("panel_revisions", True, "no prior snapshot (first run)")
    df = conn.execute(
        f"""
        SELECT n.rssd, n.week_date AS ref_date,
               o.np_PD AS np_PD_before, n.np_PD AS np_PD_after,
               n.np_PD - o.np_PD AS np_delta,
               o.sE AS sE_before, n.sE AS sE_after
        FROM pd_panel n JOIN pd_panel_prev o USING (week_date, permco)
        WHERE ABS(COALESCE(n.np_PD, 0) - COALESCE(o.np_PD, 0))
              > {config.PANEL_REVISION_TOL}
        ORDER BY ABS(n.np_PD - o.np_PD) DESC
        """
    ).fetchdf()
    df = _normalize_dates(df, "ref_date")
    dropped = conn.execute(
        "SELECT COUNT(*) FROM pd_panel_prev o WHERE NOT EXISTS "
        "(SELECT 1 FROM pd_panel n WHERE n.week_date = o.week_date "
        " AND n.permco = o.permco)"
    ).fetchone()[0]
    if len(df) or dropped:
        worst = df["np_delta"].abs().max() if len(df) else 0.0
        return CheckResult(
            "panel_revisions", False,
            f"{len(df)} historical np_PD value(s) revised (worst {worst:+.4f}), "
            f"{dropped} row(s) dropped - expected after a vendor revision, "
            f"re-snapshot any cited figures",
            df,
        )
    return CheckResult("panel_revisions", True, "OK (history unchanged)")


# -- 13: group composition changes -----------------------------------------------------


def check_group_composition(
    conn: duckdb.DuckDBPyConnection,
    *,
    since: Optional[date] = None,
) -> CheckResult:
    """A group PD is a cap-weighted index, so its level moves when a member
    arrives or leaves - not only when risk changes. Synchrony joining `lender`
    in 2014 shifts that group's sE and PD with no risk event behind it.

    This is the one check a group panel genuinely needs: every other anomaly it
    could show is already caught on the member banks it is built from. Flags
    are keyed on the group's synthetic negative id, so `ack --rssd -4` works
    the same way it does for a bank."""
    if not _table_exists(conn, "group_panel"):
        return CheckResult("group_composition", True, "no group panel yet")
    from .groups import group_id

    where = f"WHERE week_date >= DATE '{since.isoformat()}'" if since else ""
    df = conn.execute(
        f"""
        WITH s AS (
          SELECT grp, week_date, n_members,
                 LAG(n_members) OVER (PARTITION BY grp ORDER BY week_date) AS prev
          FROM group_panel {where}
        )
        SELECT grp, week_date AS ref_date, prev AS members_before,
               n_members AS members_after
        FROM s WHERE prev IS NOT NULL AND n_members <> prev
        ORDER BY week_date DESC
        """
    ).fetchdf()
    df = _normalize_dates(df, "ref_date")
    if len(df):
        df["rssd"] = [group_id(g) for g in df["grp"]]
        df = suppress_acked(conn, "group_composition", df)
    if len(df):
        return CheckResult(
            "group_composition", False,
            f"{len(df)} membership change(s) in the group indices - a level "
            f"shift at these dates is composition, not risk",
            df,
        )
    return CheckResult("group_composition", True, "OK (stable membership)")


# -- 14: equity issued, balance sheet not yet caught up --------------------------------


def _issuance_days_sql(since: Optional[date]) -> str:
    """Days where market cap moved without a matching return.

    (mcap_t / mcap_t-1) / (1 + retx_t) is ~1.0 whenever the change in market
    cap is the price change. It deviates only when the share count changed, and
    it is immune to splits: market cap is split-invariant and retx is
    split-adjusted, so a split leaves both terms at 1.0. That makes it a
    share-issuance detector that does not need share counts, which matters
    because the CRSP era has no equivalent of the Yahoo-side share_jumps gate.
    """
    lo = f"AND date >= DATE '{(since - timedelta(days=400)).isoformat()}'" if since else ""
    return f"""
      SELECT permco, date AS issue_date, ratio FROM (
        SELECT permco, date,
               (market_cap / NULLIF(LAG(market_cap) OVER w, 0))
               / NULLIF(1 + retx, 0) AS ratio
        FROM equity_daily
        WHERE market_cap IS NOT NULL AND retx IS NOT NULL {lo}
        WINDOW w AS (PARTITION BY permco ORDER BY date)
      )
      WHERE ratio IS NOT NULL
        AND ABS(ratio - 1) > {config.ISSUANCE_RATIO_TOL}
    """


def check_issuance_bs_lag(
    conn: duckdb.DuckDBPyConnection,
    rssds: list[int],
    *,
    since: Optional[date] = None,
) -> CheckResult:
    """Equity base changed after the balance-sheet date the PD is using.

    Y-9C is quarterly, so between a share issuance and the next filing the
    kernel holds post-issuance equity against pre-issuance liabilities. E_scaled
    is overstated, so the PD is *understated* - the reassuring direction, which
    is why this needs a flag rather than a footnote.

    Observed: Capital One closed Discover on 2025-05-18. Market cap 70.9bn ->
    121.1bn while total_liab stayed at the Q1 430.1bn until Q2's 548.0bn landed
    on 2025-07-04. np_PD read 0.164 for five weeks against 0.231 the week
    before. Nothing else caught it: the Q1->Q2 balance-sheet jump was +27.4%,
    under the 30% bs_jumps bar, and the PD ratio was 0.71, inside the
    pd_plausibility band. Emergency capital raises produce the same distortion
    and arrive exactly when the PD is being read.

    The flag clears itself: a week stops being listed once its bs_quarter_end
    moves past the issuance date. No correction is attempted - the true interim
    liability figure does not exist in any source here.
    """
    if not rssds:
        return CheckResult("issuance_bs_lag", True, "no banks in scope")
    scope = ",".join(str(int(r)) for r in rssds)
    wk = f"AND i.week_date >= DATE '{since.isoformat()}'" if since else ""
    df = conn.execute(
        f"""
        WITH issues AS ({_issuance_days_sql(since)}),
        bank AS (
          SELECT i.rssd, i.week_date AS ref_date, 'bank' AS scope,
                 MAX(s.issue_date) AS issue_date,
                 MAX(s.ratio)      AS equity_ratio,
                 ANY_VALUE(i.bs_quarter_end) AS bs_quarter_end,
                 1.0 AS cap_share
          FROM pd_input i
          JOIN issues s ON s.permco = i.permco
                       AND s.issue_date >  i.bs_quarter_end
                       AND s.issue_date <= i.week_date
          WHERE i.rssd IN ({scope}) AND i.E_scaled IS NOT NULL {wk}
          GROUP BY i.rssd, i.week_date
        )
        SELECT * FROM bank ORDER BY ref_date DESC, rssd
        """
    ).fetchdf()
    df = _normalize_dates(df, "ref_date", "issue_date", "bs_quarter_end")

    gdf = pd.DataFrame()
    if _table_exists(conn, "group_input"):
        from .groups import group_id

        gdf = conn.execute(
            f"""
            WITH issues AS ({_issuance_days_sql(since)}),
            affected AS (
              SELECT bg.grp, i.week_date, i.market_cap,
                     MAX(s.issue_date) AS issue_date, MAX(s.ratio) AS equity_ratio
              FROM pd_input i
              JOIN bank_group bg ON bg.rssd = i.rssd
              JOIN issues s ON s.permco = i.permco
                           AND s.issue_date >  i.bs_quarter_end
                           AND s.issue_date <= i.week_date
              WHERE i.market_cap IS NOT NULL
              GROUP BY bg.grp, i.week_date, i.market_cap
            )
            SELECT a.grp, a.week_date AS ref_date, 'group' AS scope,
                   MAX(a.issue_date)   AS issue_date,
                   MAX(a.equity_ratio) AS equity_ratio,
                   ANY_VALUE(g.bs_quarter_end) AS bs_quarter_end,
                   SUM(a.market_cap) / ANY_VALUE(g.market_cap) AS cap_share
            FROM affected a
            JOIN group_input g ON g.grp = a.grp AND g.week_date = a.week_date
            WHERE g.E_scaled IS NOT NULL
              {"AND a.week_date >= DATE '" + since.isoformat() + "'" if since else ""}
            GROUP BY a.grp, a.week_date
            HAVING SUM(a.market_cap) / ANY_VALUE(g.market_cap)
                   > {config.ISSUANCE_GROUP_CAP_SHARE}
            ORDER BY ref_date DESC, grp
            """
        ).fetchdf()
        gdf = _normalize_dates(gdf, "ref_date", "issue_date", "bs_quarter_end")
        if len(gdf):
            gdf["rssd"] = [group_id(g) for g in gdf["grp"]]

    both = pd.concat([d for d in (df, gdf) if len(d)], ignore_index=True) \
        if (len(df) or len(gdf)) else pd.DataFrame()
    both = suppress_acked(conn, "issuance_bs_lag", both)
    if len(both):
        worst = both["equity_ratio"].max()
        n_bank = int((both["scope"] == "bank").sum())
        n_grp = int((both["scope"] == "group").sum())
        return CheckResult(
            "issuance_bs_lag", False,
            f"{n_bank} bank week(s) + {n_grp} group week(s) price post-issuance "
            f"equity against a pre-issuance balance sheet (largest equity change "
            f"{worst:.2f}x) - PD understated until the next filing",
            both,
        )
    return CheckResult("issuance_bs_lag", True, "OK (balance sheets contemporaneous)")
