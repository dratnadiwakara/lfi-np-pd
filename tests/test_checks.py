"""
Output-side anomaly checks.

The scenario driving these: one fake daily return enters the 252-day vol
window, sE steps, PD stays wrong for a year, then cliffs when the day
rolls out. Real risk ramps and decays; artifacts step and cliff.
"""
from __future__ import annotations

from datetime import date, timedelta

import duckdb
import pandas as pd
import pytest

from lfinp import checks, config
from lfinp.db import ack_flag, init_schema, snapshot_panel


@pytest.fixture
def conn(tmp_path):
    c = duckdb.connect(str(tmp_path / "c.duckdb"))
    init_schema(c)
    yield c
    c.close()


def seed_banks(conn, n_banks=10, start="2025-01-01", days=400):
    """n_banks peers with flat-ish daily returns, linked rssd = 1000+i."""
    dates = pd.bdate_range(start, periods=days)
    for i in range(n_banks):
        permco, rssd = 100 + i, 1000 + i
        conn.execute(
            "INSERT INTO link (permco, rssd, quarter_end, name, confirmed) "
            "VALUES (?, ?, DATE '2024-12-31', ?, TRUE)",
            [permco, rssd, f"BANK {i}"],
        )
        for d in dates:
            conn.execute(
                "INSERT INTO crsp_daily (permco, date, price, retx, shrout, market_cap)"
                " VALUES (?, ?, 100.0, 0.001, 1000.0, 100000.0)",
                [permco, d.date()],
            )
    return dates


class TestPeerDivergence:
    def test_idiosyncratic_return_flagged(self, conn):
        dates = seed_banks(conn)
        bad_day = dates[200].date()
        # bank 0 alone jumps 66% (the COF/Discover signature)
        conn.execute(
            "UPDATE crsp_daily SET retx = 0.658 WHERE permco = 100 AND date = ?",
            [bad_day],
        )
        r = checks.check_peer_divergence(conn, [1000 + i for i in range(10)])
        assert not r.passed
        hits = r.details[r.details["ref_date"] == bad_day]
        assert len(hits) == 1
        assert int(hits.iloc[0]["rssd"]) == 1000
        assert hits.iloc[0]["peer_gap"] > 0.6

    def test_market_wide_crash_not_flagged(self, conn):
        dates = seed_banks(conn)
        crash = dates[200].date()
        # every bank down 12% together: real, must NOT flag
        conn.execute(
            "UPDATE crsp_daily SET retx = -0.12 WHERE date = ?", [crash])
        r = checks.check_peer_divergence(conn, [1000 + i for i in range(10)])
        if not r.passed:
            assert crash not in set(r.details["ref_date"]), (
                "a market-wide move must not be flagged as idiosyncratic"
            )

    def test_ack_suppresses(self, conn):
        dates = seed_banks(conn)
        bad_day = dates[200].date()
        conn.execute(
            "UPDATE crsp_daily SET retx = 0.658 WHERE permco = 100 AND date = ?",
            [bad_day],
        )
        rssds = [1000 + i for i in range(10)]
        assert not checks.check_peer_divergence(conn, rssds).passed
        ack_flag(conn, "peer_divergence", 1000, bad_day, "real", "tested")
        r2 = checks.check_peer_divergence(conn, rssds)
        assert bad_day not in set(r2.details.get("ref_date", []))


class TestSEStepAttribution:
    def _panel(self, conn, rssd=1000, permco=100):
        """Two consecutive Fridays with an sE step, plus the pd_input rows
        carrying date_eff that the attribution walks back through."""
        dates = seed_banks(conn, n_banks=10)
        # place a huge return in the week between the two Fridays
        w_prev, w_now = dates[300], dates[305]
        culprit = dates[303].date()
        conn.execute(
            "UPDATE crsp_daily SET retx = 0.658 WHERE permco = ? AND date = ?",
            [permco, culprit],
        )
        for wk, eff, se in [(w_prev, w_prev, 0.40), (w_now, w_now, 0.77)]:
            conn.execute(
                "INSERT INTO pd_input (permco, week_date, date_eff, rssd, sE,"
                " market_cap, total_liab, r, E_scaled)"
                " VALUES (?, ?, ?, ?, ?, 1e6, 1e7, 0.04, 0.1)",
                [permco, wk.date(), eff.date(), rssd, se],
            )
            conn.execute(
                "INSERT INTO pd_panel (week_date, permco, rssd, sE, np_PD, merton_PD)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                [wk.date(), permco, rssd, se, 0.22 if se < 0.5 else 0.55, 0.1],
            )
        return culprit

    def test_step_detected_and_attributed(self, conn):
        culprit = self._panel(conn)
        r = checks.check_se_steps(conn, [1000 + i for i in range(10)])
        assert not r.passed, "an sE jump 0.40 -> 0.77 must flag"
        row = r.details.iloc[0]
        assert row["culprit_date"] == culprit, "must name the day that did it"
        assert row["culprit_retx"] == pytest.approx(0.658)
        assert row["direction"] == "entered"
        # peers were flat that day -> large gap -> 'data, not market'
        assert row["peer_gap"] > 0.6
        assert "diverged from peers" in r.summary

    def test_gradual_vol_rise_not_flagged(self, conn):
        seed_banks(conn, n_banks=10)
        dates = pd.bdate_range("2025-01-01", periods=400)
        for i, se in enumerate([0.40, 0.44, 0.48, 0.52]):
            wk = dates[300 + i * 5]
            conn.execute(
                "INSERT INTO pd_input (permco, week_date, date_eff, rssd, sE,"
                " market_cap, total_liab, r, E_scaled)"
                " VALUES (100, ?, ?, 1000, ?, 1e6, 1e7, 0.04, 0.1)",
                [wk.date(), wk.date(), se],
            )
            conn.execute(
                "INSERT INTO pd_panel (week_date, permco, rssd, sE, np_PD, merton_PD)"
                " VALUES (?, 100, 1000, ?, 0.3, 0.1)",
                [wk.date(), se],
            )
        r = checks.check_se_steps(conn, [1000])
        assert r.passed, "a 10%/week ramp is a regime change, not a step"


class TestPanelRevisions:
    def test_detects_changed_history(self, conn):
        conn.execute(
            "INSERT INTO pd_panel (week_date, permco, rssd, sE, np_PD, merton_PD)"
            " VALUES (DATE '2026-01-02', 100, 1000, 0.3, 0.20, 0.05)")
        snapshot_panel(conn)
        conn.execute(
            "UPDATE pd_panel SET np_PD = 0.55 WHERE week_date = DATE '2026-01-02'")
        r = checks.check_panel_revisions(conn)
        assert not r.passed
        assert r.details.iloc[0]["np_delta"] == pytest.approx(0.35)

    def test_clean_when_unchanged(self, conn):
        conn.execute(
            "INSERT INTO pd_panel (week_date, permco, rssd, sE, np_PD, merton_PD)"
            " VALUES (DATE '2026-01-02', 100, 1000, 0.3, 0.20, 0.05)")
        snapshot_panel(conn)
        assert checks.check_panel_revisions(conn).passed

    def test_first_run_no_snapshot(self, conn):
        assert checks.check_panel_revisions(conn).passed


class TestBSJumps:
    def test_liab_jump_flagged(self, conn):
        for qe, liab in [("2025-03-31", 1.0e8), ("2025-06-30", 1.7e8)]:
            conn.execute(
                "INSERT INTO pd_input (permco, week_date, rssd, bs_quarter_end,"
                " total_liab, bs_source) VALUES (100, ?, 1000, ?, ?, 'y9c')",
                [date.fromisoformat(qe), date.fromisoformat(qe), liab],
            )
        r = checks.check_bs_jumps(conn, [1000])
        assert not r.passed
        assert r.details.iloc[0]["qoq_change"] == pytest.approx(0.7)

    def test_normal_growth_not_flagged(self, conn):
        for qe, liab in [("2025-03-31", 1.0e8), ("2025-06-30", 1.03e8)]:
            conn.execute(
                "INSERT INTO pd_input (permco, week_date, rssd, bs_quarter_end,"
                " total_liab, bs_source) VALUES (100, ?, 1000, ?, ?, 'y9c')",
                [date.fromisoformat(qe), date.fromisoformat(qe), liab],
            )
        assert checks.check_bs_jumps(conn, [1000]).passed


class TestFallbackRate:
    def test_high_extrapolation_flagged(self, conn):
        base = date(2026, 1, 2)
        for i in range(20):
            conn.execute(
                "INSERT INTO pd_panel (week_date, permco, rssd, sE, np_PD,"
                " merton_PD, mdef_fallback_used) VALUES (?, 100, 1000, 0.7, 0.6, 0.5, ?)",
                [base + timedelta(days=7 * i), 1 if i % 2 == 0 else 0],
            )
        r = checks.check_fallback_rate(conn, [1000])
        assert not r.passed
        assert r.details.iloc[0]["fallback_rate"] == pytest.approx(0.5)

    def test_supported_surface_clean(self, conn):
        base = date(2026, 1, 2)
        for i in range(20):
            conn.execute(
                "INSERT INTO pd_panel (week_date, permco, rssd, sE, np_PD,"
                " merton_PD, mdef_fallback_used) VALUES (?, 100, 1000, 0.3, 0.2, 0.05, 0)",
                [base + timedelta(days=7 * i)],
            )
        assert checks.check_fallback_rate(conn, [1000]).passed


class TestPDDecoupling:
    def test_ratio_break_with_stable_se_flagged(self, conn):
        rows = [("2026-01-02", 0.30, 0.20, 0.05), ("2026-01-09", 0.31, 0.60, 0.05)]
        for wk, se, np_pd, m_pd in rows:
            conn.execute(
                "INSERT INTO pd_panel (week_date, permco, rssd, sE, np_PD, merton_PD)"
                " VALUES (?, 100, 1000, ?, ?, ?)",
                [date.fromisoformat(wk), se, np_pd, m_pd],
            )
        r = checks.check_pd_decoupling(conn, [1000])
        assert not r.passed

    def test_moves_together_clean(self, conn):
        rows = [("2026-01-02", 0.30, 0.20, 0.05), ("2026-01-09", 0.31, 0.22, 0.055)]
        for wk, se, np_pd, m_pd in rows:
            conn.execute(
                "INSERT INTO pd_panel (week_date, permco, rssd, sE, np_PD, merton_PD)"
                " VALUES (?, 100, 1000, ?, ?, ?)",
                [date.fromisoformat(wk), se, np_pd, m_pd],
            )
        assert checks.check_pd_decoupling(conn, [1000]).passed
