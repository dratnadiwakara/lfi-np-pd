"""Pro-forma liability bridge across a merger close.

The defect: on the day an acquisition closes, the acquirer's share count jumps
to the combined company so market_cap jumps, but total_liab stays on the
acquirer's last solo Y-9C until the next quarter lands. E_scaled =
market_cap / total_liab then divides a combined numerator by a solo denominator
and the PD reads too low for up to a quarter.

The real case, frozen here: Capital One closed Discover on 2025-05-18. Market
cap 70.9bn -> 121.1bn on 2025-05-30 (the day CRSP re-based the share count, not
the legal close), total_liab stuck at Q1's 430,062,067 until Q2's 548,012,277
arrived on 2025-07-04. np_PD printed 0.1644 for five weeks against a
contemporaneous 0.1873. Discover's own last filing (2025-03-31) was
128,950,917, so the bridge gives 559,012,984 -- 2.0% above what COF actually
filed for Q2, against 27.4% below it if nothing is done.

Two things these tests pin that are easy to get wrong:

1. The bridge is timed off the day the share count actually re-bases, not the
   stated close date. Bridging from 2025-05-18 would put Discover's liabilities
   into the 2025-05-23 week, which still carries only Capital One's equity --
   the same error with the sign flipped (E_scaled 0.165 -> 0.127).
2. A merger that cannot be resolved aborts the build. Silently bridging nothing
   leaves the distortion in place while the operator believes it is corrected.
"""
from __future__ import annotations

from datetime import date

import duckdb
import pytest

from lfinp import checks, config, panel
from lfinp import compute as compute_mod
from lfinp import db as db_mod

RSSD, PERMCO = 2277860, 20265           # Capital One
TARGET_RSSD = 3846375                   # Discover Financial Services

COF_Q1_LIAB = 430_062_067.0
COF_Q2_LIAB = 548_012_277.0
DFS_Q1_LIAB = 128_950_917.0
BRIDGED_LIAB = COF_Q1_LIAB + DFS_Q1_LIAB       # 559,012,984


@pytest.fixture()
def conn(tmp_path):
    c = duckdb.connect(str(tmp_path / "t.duckdb"))
    db_mod.init_schema(c)
    yield c
    c.close()


def write_mergers(tmp_path, body: str):
    p = tmp_path / "mergers.txt"
    p.write_text(body, encoding="utf-8")
    return p


# -- 1: mergers.txt parsing ------------------------------------------------------
#
# Same contract as banks.txt: anything present must parse exactly. A silently
# dropped line restores the distortion the file exists to remove.


class TestMergersFile:
    def test_parses_a_deal(self, tmp_path):
        p = write_mergers(tmp_path, "# a comment\n"
                          "2277860  3846375  close=2025-05-18   # COF / Discover\n")
        (m,) = config.load_mergers(p)
        assert (m.acquirer_rssd, m.target_rssd) == (2277860, 3846375)
        assert m.close_date == date(2025, 5, 18)
        assert m.liab_override is None
        assert m.comment == "COF / Discover"

    def test_missing_file_is_not_an_error(self, tmp_path):
        """No mergers.txt means no bridges, which is how the repo behaved
        before the feature existed."""
        assert config.load_mergers(tmp_path / "nope.txt") == []

    def test_liab_override(self, tmp_path):
        p = write_mergers(tmp_path, "1 0 close=2020-10-06 liab=1234.5  # no filer\n")
        (m,) = config.load_mergers(p)
        assert m.liab_override == 1234.5
        assert m.target_rssd == 0

    @pytest.mark.parametrize("line, why", [
        ("2277860 3846375 close=18-05-2025", "date not ISO"),
        ("2277860 3846375 close=2025-05-18 junk=1", "unknown flag"),
        ("2277860 3846375 close=2025-05-18 dead", "flag without a value"),
        ("2277860 3846375", "no close date"),
        ("2277860", "no target"),
        ("2277860 2277860 close=2025-05-18", "acquirer is the target"),
        ("2277860 0 close=2025-05-18", "no filer and no liab="),
        ("2277860 3846375 close=2025-05-18 liab=0", "non-positive liabilities"),
    ])
    def test_bad_lines_raise(self, tmp_path, line, why):
        with pytest.raises(ValueError):
            config.load_mergers(write_mergers(tmp_path, line + "\n")), why

    def test_duplicate_deal_raises(self, tmp_path):
        p = write_mergers(tmp_path, "2277860 3846375 close=2025-05-18\n"
                                    "2277860 3846375 close=2025-05-18\n")
        with pytest.raises(ValueError, match="Duplicate"):
            config.load_mergers(p)

    def test_sync_is_a_full_replace(self, conn, tmp_path, monkeypatch):
        """A deal removed from the file must stop bridging. A stale bridge is
        worse than none: it moves a PD with no visible cause."""
        p = write_mergers(tmp_path, "2277860 3846375 close=2025-05-18\n")
        monkeypatch.setattr(config, "MERGERS_PATH", p)
        assert db_mod.sync_mergers(conn) == 1
        p.write_text("", encoding="utf-8")
        assert db_mod.sync_mergers(conn) == 0
        assert conn.execute("SELECT COUNT(*) FROM merger_event").fetchone()[0] == 0


# -- 2: the bridge inside build_pd_input -----------------------------------------
#
# build_pd_input attaches the external Y-9C store, so these tests stand up a
# miniature one and point config at it. That keeps the real SQL under test
# rather than a re-implementation of it.


def _fake_y9c(tmp_path, rows):
    """rows: (id_rssd, quarter_end, total_liab)"""
    path = tmp_path / "y9c.duckdb"
    y = duckdb.connect(str(path))
    y.execute("CREATE TABLE bs_panel_y9c (id_rssd BIGINT, date DATE, "
              "total_liab DOUBLE, assets DOUBLE, equity DOUBLE)")
    y.executemany(
        "INSERT INTO bs_panel_y9c VALUES (?, ?, ?, NULL, NULL)", rows)
    y.close()
    return path


def _seed_cof(conn, *, cap_rebase_date=date(2025, 5, 30)):
    """Capital One's real weeks around the Discover close.

    Daily market cap is flat either side of the re-base so any bridge that
    starts on the wrong week shows up as a jump in E_scaled rather than
    hiding in ordinary price movement.
    """
    conn.execute("INSERT INTO link VALUES (?, ?, DATE '2000-03-31', 'COF', TRUE)",
                 [PERMCO, RSSD])
    conn.execute("INSERT INTO ticker_hist VALUES (?, 1, 'COF', 'CAPITAL ONE', 11, "
                 "DATE '2000-01-01', DATE '2030-01-01')", [PERMCO])
    rows = []
    d = date(2025, 1, 3)
    while d <= date(2025, 7, 18):
        cap = 70_878_050.0 if d < cap_rebase_date else 121_082_100.0
        rows.append((PERMCO, d, cap))
        d = date.fromordinal(d.toordinal() + 1)
    conn.executemany(
        "INSERT INTO crsp_daily (permco, date, price, ret, retx, shrout, "
        "market_cap, src_table) VALUES (?, ?, 100.0, 0.0, 0.0, 1.0, ?, 'dsf')",
        rows)
    conn.executemany(
        "INSERT INTO fred_weekly VALUES (?, 0.044)",
        [(r[1],) for r in rows if r[1].weekday() == 4])


def _build(conn, tmp_path, monkeypatch, mergers_body, y9c_rows):
    monkeypatch.setattr(config, "MERGERS_PATH",
                        write_mergers(tmp_path, mergers_body))
    monkeypatch.setattr(config, "y9c_db_path",
                        lambda: _fake_y9c(tmp_path, y9c_rows))
    monkeypatch.setattr(config, "call_reports_db_path",
                        lambda: tmp_path / "absent.duckdb")
    db_mod.sync_mergers(conn)
    panel.build_pd_input(conn, [PERMCO], start_date="2025-01-01",
                         end_date="2025-07-18")
    return conn.execute(
        "SELECT week_date, total_liab, total_liab_reported, bs_bridged, "
        "bs_source, E_scaled FROM pd_input WHERE rssd = ? "
        "AND week_date >= DATE '2025-05-16' ORDER BY week_date", [RSSD]
    ).fetchdf()


COF_Y9C = [(RSSD, date(2025, 3, 31), COF_Q1_LIAB),
           (RSSD, date(2025, 6, 30), COF_Q2_LIAB),
           (TARGET_RSSD, date(2024, 12, 31), 129_713_801.0),
           (TARGET_RSSD, date(2025, 3, 31), DFS_Q1_LIAB)]
COF_LINE = f"{RSSD} {TARGET_RSSD} close=2025-05-18\n"


class TestBridge:
    def test_adds_the_targets_liabilities_for_the_gap_weeks(
            self, conn, tmp_path, monkeypatch):
        _seed_cof(conn)
        df = _build(conn, tmp_path, monkeypatch, COF_LINE, COF_Y9C)
        by_week = {r.week_date.date(): r for r in df.itertuples()}

        bridged = {d for d, r in by_week.items() if r.bs_bridged}
        assert bridged == {date(2025, 5, 30), date(2025, 6, 6), date(2025, 6, 13),
                           date(2025, 6, 20), date(2025, 6, 27)}, \
            "five weeks between the share re-base and Q2 landing"

        row = by_week[date(2025, 5, 30)]
        assert row.total_liab == pytest.approx(BRIDGED_LIAB)
        assert row.total_liab_reported == pytest.approx(COF_Q1_LIAB), \
            "the figure as filed must survive untouched"
        assert row.bs_source == "y9c+proforma"
        assert row.E_scaled == pytest.approx(121_082_100.0 / BRIDGED_LIAB, rel=1e-9)

    def test_stops_when_the_filing_catches_up(self, conn, tmp_path, monkeypatch):
        """Q2 already contains Discover. Bridging past it would double-count."""
        _seed_cof(conn)
        df = _build(conn, tmp_path, monkeypatch, COF_LINE, COF_Y9C)
        july = df[df["week_date"] >= "2025-07-04"]
        assert not july["bs_bridged"].any()
        assert july["total_liab"].iloc[0] == pytest.approx(COF_Q2_LIAB)

    def test_does_not_start_before_the_share_count_re_bases(
            self, conn, tmp_path, monkeypatch):
        """The bug this exists to prevent. COF closed on 2025-05-18 but CRSP
        carried the combined share count from 2025-05-30. Timing the bridge off
        the stated close would put Discover's liabilities into the 2025-05-23
        week, which still has only Capital One's equity, and drive E_scaled from
        0.165 down to 0.127 -- overstating the PD instead of understating it."""
        _seed_cof(conn)
        df = _build(conn, tmp_path, monkeypatch, COF_LINE, COF_Y9C)
        wk = df[df["week_date"] == "2025-05-23"].iloc[0]
        assert not wk.bs_bridged
        assert wk.total_liab == pytest.approx(COF_Q1_LIAB)
        assert wk.E_scaled == pytest.approx(70_878_050.0 / COF_Q1_LIAB, rel=1e-9)

    def test_follows_the_data_when_the_close_date_is_off(
            self, conn, tmp_path, monkeypatch):
        """Same deal, share count re-basing a fortnight later than stated. The
        bridge must track the data, not the file, so a slightly wrong close=
        cannot silently mis-time it."""
        _seed_cof(conn, cap_rebase_date=date(2025, 6, 11))
        df = _build(conn, tmp_path, monkeypatch, COF_LINE, COF_Y9C)
        bridged = {r.week_date.date() for r in df.itertuples() if r.bs_bridged}
        assert min(bridged) == date(2025, 6, 13)
        assert date(2025, 5, 30) not in bridged

    def test_never_double_counts_when_the_share_count_lags_the_filing(
            self, conn, tmp_path, monkeypatch):
        """The other way this goes wrong. Huntington closed Veritex on
        2025-10-20, CRSP never updated the share count, and the combined count
        first appears on 2026-01-02 -- by which time the 2025-12-31 Y-9C already
        consolidates Veritex. Bridging on the effective date alone would add
        liabilities the filing already contains.

        Equity and liabilities arrive on different clocks: equity on the
        effective date, liabilities on the close date. Only weeks with equity
        in and liabilities out may be bridged.
        """
        _seed_cof(conn, cap_rebase_date=date(2025, 7, 4))    # re-base after Q2
        df = _build(conn, tmp_path, monkeypatch, COF_LINE, COF_Y9C)
        assert not df["bs_bridged"].any(), \
            "Q2 already contains the target - nothing left to bridge"
        july = df[df["week_date"] >= "2025-07-04"].iloc[0]
        assert july.total_liab == pytest.approx(COF_Q2_LIAB)

    def test_no_merger_row_means_no_bridge(self, conn, tmp_path, monkeypatch):
        """The unbridged path must still reproduce the defect, so this file
        cannot quietly stop testing anything."""
        _seed_cof(conn)
        df = _build(conn, tmp_path, monkeypatch, "", COF_Y9C)
        assert not df["bs_bridged"].any()
        wk = df[df["week_date"] == "2025-05-30"].iloc[0]
        assert wk.E_scaled == pytest.approx(121_082_100.0 / COF_Q1_LIAB, rel=1e-9)

    def test_liab_override_wins_over_the_lookup(self, conn, tmp_path, monkeypatch):
        _seed_cof(conn)
        df = _build(conn, tmp_path, monkeypatch,
                    f"{RSSD} {TARGET_RSSD} close=2025-05-18 liab=1000.0\n", COF_Y9C)
        wk = df[df["week_date"] == "2025-05-30"].iloc[0]
        assert wk.total_liab == pytest.approx(COF_Q1_LIAB + 1000.0)


class TestUnresolvableAborts:
    def test_target_with_no_filing_aborts(self, conn, tmp_path, monkeypatch):
        """Bridging nothing while reporting success is the one outcome worse
        than not bridging at all."""
        _seed_cof(conn)
        with pytest.raises(ValueError, match="no target liabilities"):
            _build(conn, tmp_path, monkeypatch,
                   f"{RSSD} 999999 close=2025-05-18\n", COF_Y9C)

    def test_target_that_stopped_filing_long_before_the_close_aborts(
            self, conn, tmp_path, monkeypatch):
        """The trap that nearly shipped. Huntington bought TCF Financial in
        2021, but TCF's original RSSD 2389941 stops filing at 2019-06-30 --
        the 2019 TCF/Chemical Financial merger of equals put the TCF name on
        Chemical's charter (RSSD 1201934). Looking up 2389941 succeeds and
        returns a two-year-stale balance sheet, so this cannot be caught by
        checking for a missing value.
        """
        _seed_cof(conn)
        stale = [(RSSD, date(2025, 3, 31), COF_Q1_LIAB),
                 (RSSD, date(2025, 6, 30), COF_Q2_LIAB),
                 (TARGET_RSSD, date(2023, 3, 31), DFS_Q1_LIAB)]   # 2 years old
        with pytest.raises(ValueError, match="stale at close"):
            _build(conn, tmp_path, monkeypatch, COF_LINE, stale)

    def test_an_explicit_liab_is_never_called_stale(
            self, conn, tmp_path, monkeypatch):
        """liab= carries its own provenance in the comment, so the filing-age
        test does not apply -- it is exactly the escape hatch for targets whose
        figures do not come from the Y-9C panel."""
        _seed_cof(conn)
        df = _build(conn, tmp_path, monkeypatch,
                    f"{RSSD} 0 close=2025-05-18 liab=128950917\n", COF_Y9C)
        assert df["bs_bridged"].any()

    def test_close_date_with_no_share_change_aborts(
            self, conn, tmp_path, monkeypatch):
        """Either the date is wrong or the deal was cash-only and does not
        belong in the file. Both need a human, not a guess. 2025-01-10 has a
        resolvable target but no share-count change within the search window."""
        _seed_cof(conn)
        with pytest.raises(ValueError, match="no share-count change"):
            _build(conn, tmp_path, monkeypatch,
                   f"{RSSD} {TARGET_RSSD} close=2025-01-10\n", COF_Y9C)


# -- 3: the checks must not fight the correction ---------------------------------


class TestChecksAgreeWithTheBridge:
    def _week(self, conn, week_date, bs_quarter_end, *, market_cap,
              total_liab, reported, bridged):
        conn.execute(
            "INSERT INTO pd_input (permco, week_date, rssd, market_cap, "
            "total_liab, total_liab_reported, bs_bridged, E_scaled, "
            "bs_quarter_end, bs_source) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [PERMCO, week_date, RSSD, market_cap, total_liab, reported, bridged,
             market_cap / total_liab, bs_quarter_end,
             "y9c+proforma" if bridged else "y9c"])

    def test_issuance_bs_lag_stops_flagging_a_corrected_week(self, conn):
        """The flag is the to-do list for mergers.txt. A week whose denominator
        now contains the target has nothing left to report."""
        conn.executemany(
            "INSERT INTO crsp_daily (permco, date, price, ret, retx, shrout, "
            "market_cap, src_table) VALUES (?, ?, 10.0, 0.0, 0.0, 1.0, ?, 'dsf')",
            [(PERMCO, date(2025, 5, 23), 70.9), (PERMCO, date(2025, 5, 30), 121.1)])
        self._week(conn, date(2025, 5, 30), date(2025, 3, 31), market_cap=121.1,
                   total_liab=559.0, reported=430.0, bridged=True)
        assert checks.check_issuance_bs_lag(conn, [RSSD]).passed

        conn.execute("DELETE FROM pd_input")
        self._week(conn, date(2025, 5, 30), date(2025, 3, 31), market_cap=121.1,
                   total_liab=430.0, reported=430.0, bridged=False)
        assert not checks.check_issuance_bs_lag(conn, [RSSD]).passed, \
            "an uncorrected week must still flag"

    def test_bs_jumps_ignores_the_mid_quarter_bridge(self, conn):
        """bs_jumps compares one filed quarter to the next. The bridge raises
        total_liab inside a quarter on purpose, so reading it here would print
        a step that was never filed."""
        self._week(conn, date(2025, 5, 16), date(2025, 3, 31), market_cap=70.9,
                   total_liab=COF_Q1_LIAB, reported=COF_Q1_LIAB, bridged=False)
        self._week(conn, date(2025, 5, 30), date(2025, 3, 31), market_cap=121.1,
                   total_liab=BRIDGED_LIAB, reported=COF_Q1_LIAB, bridged=True)
        self._week(conn, date(2025, 7, 4), date(2025, 6, 30), market_cap=141.4,
                   total_liab=COF_Q2_LIAB, reported=COF_Q2_LIAB, bridged=False)
        r = checks.check_bs_jumps(conn, [RSSD])
        assert r.passed, \
            f"real Q1->Q2 move is 27.4%, under the 30% bar: {r.details.to_dict()}"


# -- 4: a rewritten input must force a recompute ---------------------------------


class TestStaleRowsRecompute:
    """The incremental compute skips weeks already in pd_panel. That is only
    safe while "already computed" means "computed from the inputs present now".

    Editing mergers.txt rewrites total_liab for historical weeks that sit far
    outside the rolling recompute window, so the plain not-in-pd_panel test
    silently kept the pre-bridge PDs: the first live backfill left 75 of 96
    bridged weeks stale. Nothing flagged it -- pd_panel and pd_input simply
    disagreed. The rule is general, not merger-specific: any input revision
    must pull its week back into the compute set.
    """

    def _row(self, conn, *, total_liab, in_panel_liab=None):
        conn.execute(
            "INSERT INTO pd_input (permco, week_date, rssd, market_cap, sE, r, "
            "total_liab, E_scaled) VALUES (?, DATE '2020-01-03', ?, 100.0, 0.3, "
            "0.02, ?, ?)",
            [PERMCO, RSSD, total_liab, 100.0 / total_liab])
        if in_panel_liab is not None:
            conn.execute(
                "INSERT INTO pd_panel (week_date, permco, rssd, total_liab, "
                "market_cap_raw, E_scaled, sE, r, np_PD) VALUES "
                "(DATE '2020-01-03', ?, ?, ?, 100.0, ?, 0.3, 0.02, 0.1)",
                [PERMCO, RSSD, in_panel_liab, 100.0 / in_panel_liab])

    def test_unchanged_week_is_skipped(self, conn):
        """The whole point of incremental compute -- 40 years of history must
        not re-run every week."""
        self._row(conn, total_liab=1000.0, in_panel_liab=1000.0)
        df = compute_mod.assemble_inputs(conn, permco_filter=[PERMCO])
        assert df.empty

    def test_rewritten_total_liab_is_recomputed(self, conn):
        """The bridge case: pd_input now says 1559, pd_panel was computed from
        1000."""
        self._row(conn, total_liab=1559.0, in_panel_liab=1000.0)
        df = compute_mod.assemble_inputs(conn, permco_filter=[PERMCO])
        assert len(df) == 1
        assert df["total_liab"].iloc[0] == pytest.approx(1559.0)

    def test_a_week_never_computed_is_still_picked_up(self, conn):
        self._row(conn, total_liab=1000.0)
        assert len(compute_mod.assemble_inputs(conn, permco_filter=[PERMCO])) == 1

    def test_rounding_alone_does_not_force_a_recompute(self, conn):
        """Inputs round-trip through the CSV the kernel reads. If that noise
        counted as a change, every week would recompute forever and the
        incremental path would be dead code."""
        self._row(conn, total_liab=1000.0, in_panel_liab=1000.0 + 1e-9)
        assert compute_mod.assemble_inputs(conn, permco_filter=[PERMCO]).empty


# -- 5: every line in the real mergers.txt, against what was actually filed -------


@pytest.mark.skipif(not config.y9c_db_path().exists(),
                    reason="Y-9C store not available")
class TestMergersFileBacktest:
    """The bridge is (acquirer's last solo filing + target's last filing). The
    quarter after the close, the acquirer files the real combined figure. If
    the two disagree badly the line is wrong -- bad RSSD, bad date, or a deal
    too complex for a simple sum -- and that must fail here rather than quietly
    bias a PD.

    The residual is purchase accounting: the acquirer re-values what it bought
    at fair value, which no source gives mid-quarter. COF/Discover lands at
    2.0%. 15% is loose enough for that and tight enough to catch a wrong entry.
    """

    TOLERANCE = 0.15

    def test_every_merger_reproduces_the_next_filing(self):
        mergers = config.load_mergers()
        if not mergers:
            pytest.skip("mergers.txt is empty")
        y = duckdb.connect(str(config.y9c_db_path()), read_only=True)
        try:
            problems = []
            for m in mergers:
                pre = y.execute(
                    "SELECT date, total_liab FROM bs_panel_y9c WHERE id_rssd = ? "
                    "AND date <= ? AND total_liab IS NOT NULL "
                    "ORDER BY date DESC LIMIT 1",
                    [m.acquirer_rssd, m.close_date]).fetchone()
                post = y.execute(
                    "SELECT date, total_liab FROM bs_panel_y9c WHERE id_rssd = ? "
                    "AND date > ? AND total_liab IS NOT NULL "
                    "ORDER BY date ASC LIMIT 1",
                    [m.acquirer_rssd, m.close_date]).fetchone()
                if pre is None or post is None:
                    problems.append(f"{m.acquirer_rssd} {m.close_date}: "
                                    "acquirer filings missing around the close")
                    continue
                if m.liab_override is not None:
                    tgt = m.liab_override
                else:
                    row = y.execute(
                        "SELECT total_liab FROM bs_panel_y9c WHERE id_rssd = ? "
                        "AND date <= ? AND total_liab IS NOT NULL "
                        "ORDER BY date DESC LIMIT 1",
                        [m.target_rssd, m.close_date]).fetchone()
                    if row is None:
                        problems.append(
                            f"{m.acquirer_rssd} {m.close_date}: target "
                            f"{m.target_rssd} has no filing before the close - "
                            "needs an explicit liab=")
                        continue
                    tgt = row[0]
                err = (pre[1] + tgt) / post[1] - 1
                if abs(err) > self.TOLERANCE:
                    problems.append(
                        f"{m.acquirer_rssd} {m.close_date} ({m.comment}): bridged "
                        f"{pre[1] + tgt:,.0f} vs filed {post[1]:,.0f} on "
                        f"{post[0]} = {err:+.1%}")
            assert not problems, "\n".join(problems)
        finally:
            y.close()

    def test_cof_discover_is_within_two_percent(self):
        """The worked example, pinned. If this drifts, the Y-9C source changed
        under us and every other line needs re-checking too."""
        y = duckdb.connect(str(config.y9c_db_path()), read_only=True)
        try:
            got = y.execute(
                "SELECT total_liab FROM bs_panel_y9c WHERE id_rssd = ? "
                "AND date = DATE '2025-03-31'", [TARGET_RSSD]).fetchone()
        finally:
            y.close()
        assert got is not None and got[0] == pytest.approx(DFS_Q1_LIAB)
        assert (COF_Q1_LIAB + got[0]) / COF_Q2_LIAB - 1 == pytest.approx(
            0.020, abs=0.005)
