"""Post-issuance equity against a pre-issuance balance sheet.

Y-9C is quarterly. Between a share issuance and the next filing the kernel
divides new equity by old liabilities, so E_scaled is overstated and the PD is
understated -- it fails in the direction that looks reassuring, which is why it
needs a flag. The real case: Capital One closed Discover on 2025-05-18, market
cap 70.9bn -> 121.1bn, total_liab stuck at Q1's 430.1bn until 2025-07-04.
Nothing else caught it (bs_jumps needs 30%, the quarter moved 27.4%;
pd_plausibility needs 3x, the PD moved 0.71x).
"""
from __future__ import annotations

from datetime import date, timedelta

import duckdb
import pytest

from lfinp import checks
from lfinp import db as db_mod

RSSD, PERMCO = 2277860, 20265


@pytest.fixture()
def conn(tmp_path):
    c = duckdb.connect(str(tmp_path / "t.duckdb"))
    db_mod.init_schema(c)
    yield c
    c.close()


def _daily(conn, permco, rows):
    """rows: (date, market_cap, retx)"""
    conn.executemany(
        "INSERT INTO yf_daily (permco, date, close, shares, market_cap, retx) "
        "VALUES (?, ?, 10.0, 1.0, ?, ?)", [(permco, *r) for r in rows])


def _week(conn, week_date, bs_quarter_end, *, permco=PERMCO, rssd=RSSD,
          market_cap=100.0):
    conn.execute(
        "INSERT INTO pd_input (permco, week_date, rssd, market_cap, total_liab, "
        "E_scaled, bs_quarter_end) VALUES (?, ?, ?, ?, 1000.0, 0.1, ?)",
        [permco, week_date, rssd, market_cap, bs_quarter_end])


class TestDetection:
    def test_flags_the_weeks_between_issuance_and_the_next_filing(self, conn):
        _daily(conn, PERMCO, [
            (date(2025, 5, 23), 70.9, 0.001),
            (date(2025, 5, 30), 121.1, 0.005),     # +71% cap on a +0.5% return
        ])
        for wk in [date(2025, 5, 23), date(2025, 5, 30), date(2025, 6, 27)]:
            _week(conn, wk, date(2025, 3, 31))
        _week(conn, date(2025, 7, 4), date(2025, 6, 30))   # Q2 lands

        r = checks.check_issuance_bs_lag(conn, [RSSD])
        assert not r.passed
        flagged = {d for d in r.details["ref_date"]}
        assert flagged == {date(2025, 5, 30), date(2025, 6, 27)}
        assert date(2025, 5, 23) not in flagged, "before the issuance"
        assert date(2025, 7, 4) not in flagged, "balance sheet has caught up"
        assert r.details["equity_ratio"].max() == pytest.approx(1.70, abs=0.02)

    def test_clears_itself_when_the_filing_arrives(self, conn):
        """No manual intervention: the flag is defined against bs_quarter_end,
        so it disappears on its own."""
        _daily(conn, PERMCO, [(date(2025, 5, 23), 70.9, 0.0),
                              (date(2025, 5, 30), 121.1, 0.0)])
        _week(conn, date(2025, 6, 6), date(2025, 6, 30))
        assert checks.check_issuance_bs_lag(conn, [RSSD]).passed

    def test_price_moves_alone_never_flag(self, conn):
        """A 20% crash moves market cap 20% with a matching return. The ratio
        stays at 1.0 -- otherwise every volatile week would flag."""
        _daily(conn, PERMCO, [(date(2025, 5, 23), 100.0, 0.0),
                              (date(2025, 5, 30), 80.0, -0.20)])
        _week(conn, date(2025, 5, 30), date(2025, 3, 31))
        assert checks.check_issuance_bs_lag(conn, [RSSD]).passed

    def test_a_split_is_not_an_issuance(self, conn):
        """Shares double, price halves. Market cap is split-invariant and retx
        is split-adjusted, so the detector must see nothing. Using raw share
        counts here would fire on every split."""
        _daily(conn, PERMCO, [(date(2025, 5, 23), 100.0, 0.0),
                              (date(2025, 5, 30), 100.0, 0.0)])
        _week(conn, date(2025, 5, 30), date(2025, 3, 31))
        assert checks.check_issuance_bs_lag(conn, [RSSD]).passed

    def test_issuance_before_the_balance_sheet_date_is_not_flagged(self, conn):
        """The filing already includes it. Flagging would never clear."""
        _daily(conn, PERMCO, [(date(2025, 1, 10), 70.0, 0.0),
                              (date(2025, 1, 17), 120.0, 0.0)])
        _week(conn, date(2025, 5, 30), date(2025, 3, 31))
        assert checks.check_issuance_bs_lag(conn, [RSSD]).passed


class TestGroupScope:
    def _setup(self, conn, other_cap):
        conn.execute(
            "INSERT INTO bank_group (rssd, grp, permco, dead, name) VALUES "
            "(?, 'lender', ?, FALSE, 'COF'), (1, 'lender', 999, FALSE, 'other')",
            [RSSD, PERMCO])
        _daily(conn, PERMCO, [(date(2025, 5, 23), 70.9, 0.0),
                              (date(2025, 5, 30), 121.1, 0.0)])
        wk = date(2025, 5, 30)
        _week(conn, wk, date(2025, 3, 31), market_cap=121.1)
        _week(conn, wk, date(2025, 3, 31), permco=999, rssd=1, market_cap=other_cap)
        conn.execute(
            "INSERT INTO group_input (grp, week_date, market_cap, total_liab, "
            "E_scaled, bs_quarter_end, n_members) VALUES "
            "('lender', ?, ?, 1000.0, 0.1, DATE '2025-03-31', 2)",
            [wk, 121.1 + other_cap])
        return wk

    def test_group_week_is_flagged_when_the_issuer_is_material(self, conn):
        self._setup(conn, other_cap=600.0)        # COF ~17% of the group
        r = checks.check_issuance_bs_lag(conn, [RSSD])
        grp = r.details[r.details["scope"] == "group"]
        assert len(grp) == 1
        assert grp.iloc[0]["rssd"] == -4         # lender's synthetic id
        assert grp.iloc[0]["cap_share"] == pytest.approx(121.1 / 721.1, rel=1e-6)

    def test_group_week_is_not_flagged_for_a_trivial_member(self, conn):
        """A 1% member issuing shares does not move a sector PD. Flagging it
        would train the operator to ignore this check."""
        self._setup(conn, other_cap=20_000.0)     # COF ~0.6% of the group
        r = checks.check_issuance_bs_lag(conn, [RSSD])
        assert not len(r.details[r.details["scope"] == "group"])
        assert len(r.details[r.details["scope"] == "bank"]), \
            "the bank itself is still distorted and must still flag"
