"""Group-level PD: each business-model group as one merged bank.

The group is a synthetic entity, so nothing upstream validates it. These tests
pin the four things that would silently produce a plausible wrong number:
lagged weights (look-ahead), members who left (composition), partial balance
sheets (understated liabilities), and a group index that is secretly just the
biggest member.
"""
from __future__ import annotations

import math
from datetime import date, timedelta

import duckdb
import pytest

from lfinp import config
from lfinp import db as db_mod
from lfinp import groups


@pytest.fixture()
def conn(tmp_path):
    c = duckdb.connect(str(tmp_path / "t.duckdb"))
    db_mod.init_schema(c)
    c.execute("INSERT INTO bank_group (rssd, grp, permco, dead, name) VALUES "
              "(1, 'dealer', 101, FALSE, 'A'), (2, 'dealer', 102, FALSE, 'B')")
    yield c
    c.close()


def _days(n, start=date(2020, 1, 1)):
    return [start + timedelta(days=i) for i in range(n)]


def _daily(conn, permco, rows):
    """rows: (date, close, shares, market_cap, retx)"""
    conn.executemany(
        "INSERT INTO yf_daily (permco, date, close, shares, market_cap, retx) "
        "VALUES (?, ?, ?, ?, ?, ?)", [(permco, *r) for r in rows])


class TestIndex:
    def test_weights_are_lagged_not_contemporaneous(self, conn):
        """A big return raises the same day's market cap. Weighting by that cap
        would let the winner grade its own weight - a look-ahead that biases the
        index up. Weights must come from t-1."""
        d = _days(2)
        # A: flat, cap 100. B: +50%, cap 100 -> 150.
        _daily(conn, 101, [(d[0], 1, 1, 100.0, None), (d[1], 1, 1, 100.0, 0.0)])
        _daily(conn, 102, [(d[0], 1, 1, 100.0, None), (d[1], 1, 1, 150.0, 0.5)])
        groups.build_group_daily(conn)
        ret = conn.execute(
            "SELECT ret FROM group_daily WHERE date = ?", [d[1]]).fetchone()[0]
        assert ret == pytest.approx(0.25)      # lagged 100/100 weights
        assert ret != pytest.approx(0.30)      # 100/150 same-day weights

    def test_index_is_not_just_the_largest_member(self, conn):
        """Cap weighting must actually weight. A 9:1 split should track the big
        member without ignoring the small one."""
        d = _days(2)
        _daily(conn, 101, [(d[0], 1, 1, 900.0, None), (d[1], 1, 1, 900.0, 0.10)])
        _daily(conn, 102, [(d[0], 1, 1, 100.0, None), (d[1], 1, 1, 100.0, -0.10)])
        groups.build_group_daily(conn)
        ret = conn.execute(
            "SELECT ret FROM group_daily WHERE date = ?", [d[1]]).fetchone()[0]
        assert ret == pytest.approx(0.08)      # 0.9*0.10 + 0.1*(-0.10)

    def test_departed_member_leaves_the_index(self, conn):
        """SVB stops trading; it must stop contributing. No rule does this - it
        falls out of the join - so the behaviour is worth pinning."""
        d = _days(3)
        _daily(conn, 101, [(x, 1, 1, 100.0, None if i == 0 else 0.0)
                           for i, x in enumerate(d)])
        _daily(conn, 102, [(d[0], 1, 1, 100.0, None), (d[1], 1, 1, 100.0, 0.20)])
        groups.build_group_daily(conn)
        rows = dict(conn.execute(
            "SELECT date, n_ret_members FROM group_daily").fetchall())
        assert rows[d[1]] == 2
        # last day: only the survivor, and the index still exists
        assert conn.execute("SELECT COUNT(*) FROM group_daily "
                            "WHERE date = ?", [d[2]]).fetchone()[0] == 0, \
            "a one-member day is below MIN_MEMBERS and must not form an index"

    def test_first_day_return_is_null_not_dropped(self, conn):
        """No lagged weight exists on day one. The row must survive with a NULL
        return: the vol window counts rows, so dropping it would silently pull
        an extra day into every window."""
        d = _days(2)
        for p in (101, 102):
            _daily(conn, p, [(d[0], 1, 1, 100.0, None), (d[1], 1, 1, 100.0, 0.01)])
        groups.build_group_daily(conn)
        rows = conn.execute(
            "SELECT date, ret FROM group_daily ORDER BY date").fetchall()
        assert len(rows) == 2
        assert rows[0][1] is None


class TestDiversification:
    def test_group_vol_is_below_member_vol_when_members_differ(self, conn):
        """The reason a merged entity is not an average of PDs. Two members with
        offsetting moves must produce an index quieter than either."""
        n = 300
        d = _days(n)
        a = [(-1) ** i * 0.02 for i in range(n)]      # zig
        b = [-x for x in a]                           # zag
        _daily(conn, 101, [(d[i], 1, 1, 100.0, None if i == 0 else a[i])
                           for i in range(n)])
        _daily(conn, 102, [(d[i], 1, 1, 100.0, None if i == 0 else b[i])
                           for i in range(n)])
        groups.build_group_daily(conn)
        var = conn.execute("SELECT stddev_samp(ret) FROM group_daily").fetchone()[0]
        assert var == pytest.approx(0.0, abs=1e-12), \
            "perfectly offsetting members must net to a flat index"


class TestBalanceSheet:
    def _week_setup(self, conn, liab_b_present=True):
        n = 200
        d = _days(n)
        for p in (101, 102):
            _daily(conn, p, [(d[i], 1, 1, 100.0, None if i == 0 else 0.01)
                             for i in range(n)])
        wk = date(2020, 7, 3)      # a Friday inside the range
        rows = [(101, wk, d[-1], 1, 100.0, 1000.0), ]
        rows.append((102, wk, d[-1], 2, 100.0, 2000.0 if liab_b_present else None))
        conn.executemany(
            "INSERT INTO pd_input (permco, week_date, date_eff, rssd, market_cap, "
            "total_liab) VALUES (?, ?, ?, ?, ?, ?)", rows)
        conn.execute("INSERT INTO fred_weekly (week_date, r_decimal) VALUES (?, 0.02)",
                     [wk])
        groups.build_group_daily(conn)
        groups.build_group_input(conn, start_date="2020-01-01", end_date="2020-07-10")
        return wk

    def test_liabilities_are_summed_not_averaged(self, conn):
        wk = self._week_setup(conn)
        liab, mcap, e = conn.execute(
            "SELECT total_liab, market_cap, E_scaled FROM group_input "
            "WHERE week_date = ?", [wk]).fetchone()
        assert liab == 3000.0 and mcap == 200.0
        assert e == pytest.approx(200.0 / 3000.0)

    def test_partial_coverage_refuses_the_week(self, conn):
        """Half the group has no Y-9C row. Summing what exists understates
        liabilities, which understates the PD - wrong in the reassuring
        direction. The week must produce no number at all."""
        wk = self._week_setup(conn, liab_b_present=False)
        liab, cov, e, n_bs = conn.execute(
            "SELECT total_liab, bs_cap_coverage, E_scaled, n_members_bs "
            "FROM group_input WHERE week_date = ?", [wk]).fetchone()
        assert cov == pytest.approx(0.5) and n_bs == 1
        assert liab is None and e is None
        # and the refused week must not reach the kernel
        assert groups.assemble_group_inputs(conn).empty


class TestIdentity:
    def test_group_ids_are_negative_and_distinct(self):
        ids = [groups.group_id(g) for g in config.BANK_GROUPS]
        assert all(i < 0 for i in ids), "must never collide with a real permco"
        assert len(set(ids)) == len(ids)

    def test_vol_uses_the_same_window_as_a_member_bank(self, conn):
        """sE for a group must be computed the same way as for a bank, or the
        two panels are not comparable."""
        n = config.VOL_WINDOW + 20
        d = _days(n)
        rx = [((i * 37 % 101) - 50) / 1000.0 for i in range(n)]
        for p in (101, 102):
            _daily(conn, p, [(d[i], 1, 1, 100.0, None if i == 0 else rx[i])
                             for i in range(n)])
        groups.build_group_daily(conn)
        window = rx[-config.VOL_WINDOW:]
        mean = sum(window) / len(window)
        var = sum((x - mean) ** 2 for x in window) / (len(window) - 1)
        expect = math.sqrt(var) * math.sqrt(252)
        got = conn.execute(f"""
            SELECT stddev_samp(ret) * sqrt(252) FROM (
              SELECT ret FROM group_daily ORDER BY date DESC
              LIMIT {config.VOL_WINDOW})
        """).fetchone()[0]
        assert got == pytest.approx(expect, rel=1e-12)
