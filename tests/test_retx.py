"""
Regression tests against frozen real-world Yahoo data (tests/fixtures/,
captured 2026-08-15) plus synthetic logic tests.

The three fixture episodes are the defects that motivated this repo:
  NTRS  2026-07-23..08-01  vendor share glitch 185.0M -> 122.8M -> 183.0M
  FITB  2026-02-03/04      share overshoot 661.2M -> 1087.0M -> 900.0M
  COF   2025-05-20         real M&A issuance 383.1M -> 640.1M (Discover)

Under the old bank-pd method (retx = market-cap pct-change) these produced
fake daily returns of -33.9%/+49.0%, +66.9%/-15.1%, and +65.8%. Under the
price-based method retx must stay at normal magnitudes throughout.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from lfinp import config
from lfinp.yf import (
    assemble,
    dedup_shares,
    repair_share_glitches,
    split_correct_shares,
)

FIX = Path(__file__).parent / "fixtures"


def load_case(name: str):
    h = pd.read_csv(FIX / f"{name}_history.csv", parse_dates=["date"], index_col="date")
    close = h["close"]
    splits = h["splits"]
    sd = pd.read_csv(FIX / f"{name}_shares.csv", parse_dates=["date"])
    shares = pd.Series(sd["shares"].values, index=sd["date"])  # duplicates preserved
    return close, splits, shares


def old_method_retx(df: pd.DataFrame) -> pd.Series:
    """The defective bank-pd derivation: market-cap pct-change with raw
    (uncorrected, unrepaired) shares — kept here so the tests document
    exactly what the new method fixes."""
    return df["mcap_raw"].pct_change()


def build(name: str, window_start: date):
    close, splits, shares = load_case(name)
    df = assemble(close, splits, shares, window_start)
    assert df is not None and len(df) > 10
    # Reconstruct the old method alongside: raw shares ffilled, no repair
    s = shares[~shares.index.duplicated(keep="last")].sort_index()
    raw = s.reindex(s.index.union(close.index)).sort_index().ffill().reindex(close.index)
    mcap_raw = (close * raw / 1000.0)
    old = pd.DataFrame({"mcap_raw": mcap_raw})
    old = old[old.index >= pd.Timestamp(window_start)]
    old.index = [ts.date() for ts in old.index]
    return df, old


# -- NTRS: pure vendor glitch ---------------------------------------------------


class TestNTRSGlitch:
    def test_price_retx_stays_sane(self):
        df, _ = build("ntrs_glitch", date(2026, 6, 1))
        assert df["retx"].abs().max() < 0.05, (
            "price-based retx must not see the share glitch"
        )

    def test_old_method_reproduces_the_defect(self):
        _, old = build("ntrs_glitch", date(2026, 6, 1))
        r = old_method_retx(old).dropna()
        assert r.min() < -0.25 and r.max() > 0.30, (
            "fixture no longer reproduces the -33.9%%/+49.0%% mcap artifact - "
            "if Yahoo fixed its data, refresh the fixture note, do not delete "
            "the test"
        )

    def test_glitch_shares_repaired(self):
        close, splits, shares = load_case("ntrs_glitch")
        df = assemble(close, splits, shares, date(2026, 6, 1))
        # The bogus 122.8M segment must be repaired back to the prior level
        seg = df.loc[date(2026, 7, 23):date(2026, 7, 31)]
        assert (seg["shares"] > 1.7e8).all(), (
            f"glitch segment not repaired: {seg['shares'].unique()}"
        )
        assert len(df.attrs["shares_repairs"]) >= 4

    def test_market_cap_level_repaired(self):
        df, _ = build("ntrs_glitch", date(2026, 6, 1))
        # 2026-07-23 true mcap ~ 177.95 * 185.0M ~ 32.9B (33.0e6 thousands)
        mc = df.loc[date(2026, 7, 23), "market_cap"]
        assert 3.0e7 < mc < 3.6e7, f"mcap {mc:,.0f} still on bogus share basis"


# -- FITB: overshoot-then-correct -------------------------------------------------


class TestFITBOvershoot:
    def test_price_retx_stays_sane(self):
        df, _ = build("fitb_overshoot", date(2025, 12, 1))
        assert df["retx"].abs().max() < 0.10

    def test_old_method_reproduces_the_defect(self):
        _, old = build("fitb_overshoot", date(2025, 12, 1))
        r = old_method_retx(old).dropna()
        assert r.max() > 0.30, "fixture lost the +66.9% overshoot artifact"


# -- COF: real M&A ---------------------------------------------------------------


class TestCOFMnA:
    def test_price_retx_unaffected_by_issuance(self):
        df, _ = build("cof_mna", date(2025, 4, 1))
        # 2025-05-20, deal close: shares +67%, price moved -0.74%.
        assert abs(df.loc[date(2025, 5, 20), "retx"]) < 0.02, (
            "the Discover share issuance is not a return"
        )
        # April 2025 tariff-crash days hit ~14.8% for real - only cap the
        # window at "no fabricated jump" scale:
        assert df["retx"].abs().max() < 0.20

    def test_share_step_kept(self):
        df, _ = build("cof_mna", date(2025, 4, 1))
        before = df.loc[date(2025, 5, 16), "shares"]
        after = df.loc[date(2025, 6, 16), "shares"]
        assert after / before > 1.5, "persistent M&A share step must be kept"

    def test_old_method_reproduces_the_defect(self):
        _, old = build("cof_mna", date(2025, 4, 1))
        r = old_method_retx(old).dropna()
        assert r.max() > 0.40, "fixture lost the +65.8% issuance artifact"


# -- synthetic logic tests ----------------------------------------------------------


def _bdays(start: str, n: int) -> pd.DatetimeIndex:
    return pd.bdate_range(start, periods=n)


class TestSplitCorrection:
    def test_two_for_one_split(self):
        idx = _bdays("2026-01-05", 40)
        split_day = idx[20]
        # price halves at the split (adjusted series: flat at 50 after
        # back-adjustment; simulate yahoo's adjusted close = flat 50)
        close = pd.Series(50.0, index=idx)
        splits = pd.Series(0.0, index=idx)
        splits.loc[split_day] = 2.0
        # raw shares: 100 before, 200 after
        shares = pd.Series([100.0] * 20 + [200.0] * 20, index=idx)
        adj = split_correct_shares(shares, splits)
        assert (adj[idx < split_day] == 200.0).all(), "pre-split shares not doubled"
        assert (adj[idx >= split_day] == 200.0).all()
        df = assemble(close, splits, shares, idx[5].date())
        assert df is not None
        # market cap must be continuous across the split
        mc = df["market_cap"]
        assert mc.max() / mc.min() - 1 < 1e-9
        assert df["retx"].abs().max() < 1e-12


class TestSyntheticFlag:
    def test_weekend_not_flagged_holiday_flagged(self):
        idx = pd.DatetimeIndex([
            "2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08", "2026-01-09",
            "2026-01-12",              # Monday: 3-day gap -> not synthetic
            "2026-01-20",              # 8-day gap -> synthetic
        ])
        close = pd.Series(100.0, index=idx)
        splits = pd.Series(0.0, index=idx)
        shares = pd.Series(1000.0, index=idx)
        df = assemble(close, splits, shares, date(2026, 1, 5))
        assert not df.loc[date(2026, 1, 12), "retx_synthetic"]
        assert bool(df.loc[date(2026, 1, 20), "retx_synthetic"])


class TestBufferDrop:
    def test_first_window_day_has_retx(self):
        idx = _bdays("2026-01-05", 30)
        close = pd.Series(np.linspace(100, 110, 30), index=idx)
        splits = pd.Series(0.0, index=idx)
        shares = pd.Series(1000.0, index=idx)
        window_start = idx[10].date()
        df = assemble(close, splits, shares, window_start)
        assert df.index[0] == window_start
        assert pd.notna(df["retx"].iloc[0]), (
            "buffer rows must feed the first in-window retx"
        )


class TestDedupShares:
    def test_conflicting_duplicates_resolved_by_neighbors(self):
        idx = pd.DatetimeIndex(
            ["2026-01-05", "2026-01-06", "2026-01-07", "2026-01-07",
             "2026-01-08", "2026-01-09"]
        )
        vals = [100.0, 100.0, 100.0, 400.0, 100.0, 100.0]
        s = pd.Series(vals, index=idx)
        out, warnings = dedup_shares(s)
        assert out.loc[pd.Timestamp("2026-01-07")] == 100.0, (
            "must pick the neighbor-consistent duplicate, not insertion order"
        )
        assert warnings


class TestRecentJumpAbort:
    def test_unexplained_recent_jump_flagged(self):
        idx = _bdays("2026-01-05", 30)
        shares = pd.Series([1000.0] * 28 + [1500.0] * 2, index=idx)
        splits = pd.Series(0.0, index=idx)
        repaired, repairs, warns, recent = repair_share_glitches(shares, splits)
        assert recent is not None, (
            "a +50% share jump 2 days ago cannot show persistence and must "
            "be surfaced as an abort signal"
        )

    def test_explained_by_split_not_flagged(self):
        idx = _bdays("2026-01-05", 30)
        shares = pd.Series([1000.0] * 28 + [2000.0] * 2, index=idx)
        splits = pd.Series(0.0, index=idx)
        splits.loc[idx[28]] = 2.0
        _, _, _, recent = repair_share_glitches(shares, splits)
        assert recent is None


class TestSharesGrounding:
    """BNY pattern: Yahoo prices are the right security but the share
    history before a ticker rename belongs to a previous holder of the
    symbol (24.1M fund shares vs the bank's ~713M)."""

    def _db(self, tmp_path, shrout_thousands=727_078.0):
        import duckdb
        from lfinp.db import init_schema
        conn = duckdb.connect(str(tmp_path / "g.duckdb"))
        init_schema(conn)
        days = pd.bdate_range("2025-12-01", "2025-12-31")
        for d in days:
            conn.execute(
                "INSERT INTO crsp_daily (permco, date, price, retx, shrout, market_cap)"
                " VALUES (1, ?, 77.0, 0.0, ?, ?)",
                [d.date(), shrout_thousands, 77.0 * shrout_thousands],
            )
        return conn

    def _frame(self, shares_pattern):
        idx = pd.bdate_range("2025-12-15", "2026-02-15")
        close = pd.Series(77.0, index=idx)
        splits = pd.Series(0.0, index=idx)
        shares = pd.Series(shares_pattern(idx), index=idx)
        from lfinp.yf import assemble
        from datetime import date as _d
        return assemble(close, splits, shares, _d(2025, 12, 15))

    def test_bny_pattern_grounded(self, tmp_path):
        from lfinp.yf import ground_shares_to_crsp
        # Yahoo: wrong entity's 24.1M until 2026-02-02, bank's 713M after
        def pattern(idx):
            return [24_117_100.0 if ts < pd.Timestamp("2026-02-02")
                    else 713_000_000.0 for ts in idx]
        df = self._frame(pattern)
        conn = self._db(tmp_path)
        out = ground_shares_to_crsp(conn, 1, df)
        from datetime import date as _d
        # CRSP-covered day: exact shrout
        assert out.loc[_d(2025, 12, 22), "shares"] == pytest.approx(727_078_000.0)
        # post-edge, pre-consistency: held at anchor, not 24.1M
        assert out.loc[_d(2026, 1, 15), "shares"] == pytest.approx(727_078_000.0)
        # once consistent: Yahoo trusted
        assert out.loc[_d(2026, 2, 6), "shares"] == pytest.approx(713_000_000.0)
        # market cap continuous at sane level throughout
        assert (out["market_cap"] > 5.0e7).all()
        conn.close()

    def test_never_consistent_aborts(self, tmp_path):
        from lfinp.yf import ground_shares_to_crsp
        df = self._frame(lambda idx: [24_117_100.0] * len(idx))  # CLBK case
        conn = self._db(tmp_path)
        with pytest.raises(RuntimeError, match="share-basis mismatch"):
            ground_shares_to_crsp(conn, 1, df)
        conn.close()


class TestBanksFile:
    def test_load_banks(self):
        banks = config.load_banks()
        assert len(banks) == 37
        dead = {b.rssd for b in banks if b.dead}
        assert 1031449 in dead, "SVB must be dead"
        assert len(dead) == 13, "SVB plus the 12 failed backtest banks"
        # every failed bank except SVB is nogroup (published history frozen)
        nogroup = {b.rssd for b in banks if b.nogroup}
        assert nogroup == dead - {1031449}
        assert not any(b.nogroup and not b.dead for b in banks), \
            "nogroup is only for dead banks"
        assert len({b.rssd for b in banks}) == 37
        assert 1073757 in {b.rssd for b in banks}  # BAC


class TestSchema:
    def test_init_schema_fresh_db(self, tmp_path):
        import duckdb
        from lfinp.db import init_schema
        conn = duckdb.connect(str(tmp_path / "t.duckdb"))
        init_schema(conn)
        tables = {r[0] for r in conn.execute(
            "SELECT table_name FROM information_schema.tables"
        ).fetchall()}
        assert {"crsp_daily", "yf_daily", "pd_input", "pd_panel",
                "pull_diff", "check_log", "link", "ticker_hist",
                "fred_dgs10", "fred_weekly"} <= tables
        # idempotent
        init_schema(conn)
        conn.close()
