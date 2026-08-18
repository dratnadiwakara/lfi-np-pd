"""The parquet feed behind the Shiny dashboard.

This is the one artifact of this pipeline that a stranger reads without any of
its context, so the properties worth pinning are the ones whose failure is
invisible on the page: a bank silently split in two at an RSSD change, a blank
legend entry, a pro-forma week that lost its marker because a NULL became NA,
or a bundle that quietly outgrew the free tier it is deployed to.
"""
from __future__ import annotations

from datetime import date

import duckdb
import pytest

from lfinp import dashboard_export as dx
from lfinp import db as db_mod


@pytest.fixture()
def conn(tmp_path):
    c = duckdb.connect(str(tmp_path / "t.duckdb"))
    db_mod.init_schema(c)
    yield c
    c.close()


def _bank(conn, permco, rssd, name, grp="lender", ticker="TIC", dead=False):
    conn.execute(
        "INSERT INTO bank_group (rssd, grp, permco, dead, name) "
        "VALUES (?, ?, ?, ?, ?)",
        [rssd, grp, permco, dead, name],
    )
    conn.execute(
        "INSERT INTO ticker_hist "
        "(permco, permno, ticker, comnam, shrcd, namedt, nameenddt) "
        "VALUES (?, ?, ?, ?, 11, DATE '1990-01-01', DATE '2026-01-01')",
        [permco, permco * 10, ticker, name],
    )


def _panel(conn, week, permco, rssd, np_pd=0.20, merton=0.05):
    conn.execute(
        "INSERT INTO pd_panel (week_date, permco, rssd, np_PD, merton_PD, sE) "
        "VALUES (?, ?, ?, ?, ?, 0.3)",
        [week, permco, rssd, np_pd, merton],
    )


def _input(conn, week, permco, rssd, bridged):
    conn.execute(
        "INSERT INTO pd_input (permco, week_date, rssd, bs_bridged) "
        "VALUES (?, ?, ?, ?)",
        [permco, week, rssd, bridged],
    )


def _rows(conn, path):
    return conn.execute(
        f"SELECT * FROM read_parquet('{path.as_posix()}')"
    ).fetchdf()


@pytest.fixture()
def seeded(conn):
    """Two banks, three weeks. Enough to exercise every file."""
    _bank(conn, 100, 1001, "ALPHA BANCORP", grp="lender", ticker="ALP")
    _bank(conn, 200, 2002, "BETA TRUST", grp="feebased", ticker="BET")
    for wk in [date(2026, 1, 2), date(2026, 1, 9), date(2026, 1, 16)]:
        _panel(conn, wk, 100, 1001, np_pd=0.20, merton=0.05)
        _panel(conn, wk, 200, 2002, np_pd=0.10, merton=0.03)
    return conn


class TestFilesAndShape:
    def test_writes_all_four_files(self, seeded, tmp_path):
        res = dx.export_dashboard(seeded, tmp_path, min_banks=1)
        for name in dx.FILES:
            p = res.paths[name]
            assert p.exists() and p.stat().st_size > 0, name
        assert res.rows["pd_panel"] == 6
        assert res.rows["banks"] == 2
        assert res.rows["meta"] == 1

    def test_panel_columns_are_the_published_set(self, seeded, tmp_path):
        res = dx.export_dashboard(seeded, tmp_path, min_banks=1)
        cols = list(_rows(seeded, res.paths["pd_panel"]).columns)
        assert cols == ["week_date", "permco", "np_PD", "merton_PD",
                        "bs_bridged"]

    def test_vendor_levels_never_ship(self, seeded, tmp_path):
        """total_liab and market_cap_raw are the closest thing here to
        redistributable vendor data. They must not reach a public bundle."""
        res = dx.export_dashboard(seeded, tmp_path, min_banks=1)
        cols = set(_rows(seeded, res.paths["pd_panel"]).columns)
        assert not cols & {"total_liab", "market_cap_raw", "sE", "E_scaled",
                           "r", "mdef", "mdef_fallback_used"}

    def test_drops_rows_with_no_pd_at_all(self, seeded, tmp_path):
        seeded.execute(
            "INSERT INTO pd_panel (week_date, permco, rssd, np_PD, merton_PD) "
            "VALUES (DATE '2026-01-23', 100, 1001, NULL, NULL)"
        )
        res = dx.export_dashboard(seeded, tmp_path, min_banks=1)
        df = _rows(seeded, res.paths["pd_panel"])
        assert date(2026, 1, 23) not in set(df["week_date"].dt.date)

    def test_meta_carries_the_coverage(self, seeded, tmp_path):
        res = dx.export_dashboard(seeded, tmp_path, min_banks=1)
        meta = _rows(seeded, res.paths["meta"]).iloc[0]
        assert meta["week_min"].date() == date(2026, 1, 2)
        assert meta["week_max"].date() == date(2026, 1, 16)
        assert meta["n_panel_rows"] == 6
        assert meta["n_banks"] == 2


class TestPermcoIsTheSeriesKey:
    def test_rssd_change_stays_one_bank(self, seeded, tmp_path):
        """The trap this export exists to avoid. BNY Mellon, Regions and
        KeyCorp each carry two RSSDs in the real panel; keyed on rssd they
        would render as two half-length series with a gap and no error."""
        _panel(seeded, date(2026, 1, 23), 100, 9999)   # same permco, new rssd
        res = dx.export_dashboard(seeded, tmp_path, min_banks=1)

        banks = _rows(seeded, res.paths["banks"])
        assert (banks["permco"] == 100).sum() == 1

        df = _rows(seeded, res.paths["pd_panel"])
        assert (df["permco"] == 100).sum() == 4          # one continuous series
        assert "rssd" not in df.columns

    def test_duplicate_permco_aborts(self, seeded, tmp_path):
        seeded.execute(
            "INSERT INTO bank_group (rssd, grp, permco, dead, name) "
            "VALUES (3003, 'lender', 100, FALSE, 'ALPHA BANCORP OLD CHARTER')"
        )
        with pytest.raises(ValueError, match="duplicate permco"):
            dx.export_dashboard(seeded, tmp_path, min_banks=1)


class TestGatesAbort:
    def test_empty_panel_raises(self, conn, tmp_path):
        with pytest.raises(ValueError, match="pd_panel is empty"):
            dx.export_dashboard(conn, tmp_path)

    def test_all_null_pd_raises(self, conn, tmp_path):
        _bank(conn, 100, 1001, "ALPHA BANCORP")
        conn.execute(
            "INSERT INTO pd_panel (week_date, permco, rssd, np_PD, merton_PD) "
            "VALUES (DATE '2026-01-02', 100, 1001, NULL, NULL)"
        )
        with pytest.raises(ValueError, match="nothing to publish"):
            dx.export_dashboard(conn, tmp_path)

    def test_bank_with_no_name_raises(self, conn, tmp_path):
        conn.execute(
            "INSERT INTO bank_group (rssd, grp, permco, dead, name) "
            "VALUES (1001, 'lender', 100, FALSE, NULL)"
        )
        _panel(conn, date(2026, 1, 2), 100, 1001)
        with pytest.raises(ValueError, match="no display name"):
            dx.export_dashboard(conn, tmp_path, min_banks=1)

    def test_name_falls_back_to_crsp_comnam(self, conn, tmp_path):
        """A NULL bank_group.name is survivable when CRSP knows the company;
        only a bank nobody can name is fatal."""
        conn.execute(
            "INSERT INTO bank_group (rssd, grp, permco, dead, name) "
            "VALUES (1001, 'lender', 100, FALSE, NULL)"
        )
        conn.execute(
            "INSERT INTO ticker_hist "
            "(permco, permno, ticker, comnam, shrcd, namedt, nameenddt) "
            "VALUES (100, 1000, 'ALP', 'ALPHA BANCORP', 11, "
            "DATE '1990-01-01', DATE '2026-01-01')"
        )
        _panel(conn, date(2026, 1, 2), 100, 1001)
        res = dx.export_dashboard(conn, tmp_path, min_banks=1)
        assert _rows(conn, res.paths["banks"]).iloc[0]["name"] == "ALPHA BANCORP"


class TestMeanPD:
    def test_mean_is_the_equal_weight_cross_section(self, seeded, tmp_path):
        res = dx.export_dashboard(seeded, tmp_path, min_banks=1)
        df = _rows(seeded, res.paths["mean_pd"])
        row = df[df["week_date"].dt.date == date(2026, 1, 9)].iloc[0]
        assert row["n_banks"] == 2
        assert row["np_PD"] == pytest.approx((0.20 + 0.10) / 2)
        assert row["merton_PD"] == pytest.approx((0.05 + 0.03) / 2)

    def test_thin_weeks_are_dropped(self, seeded, tmp_path):
        """A cross-sectional mean over two banks is a composition artifact,
        not a market reading."""
        _panel(seeded, date(2026, 1, 30), 100, 1001, np_pd=0.9)
        res = dx.export_dashboard(seeded, tmp_path, min_banks=2)
        weeks = set(_rows(seeded, res.paths["mean_pd"])["week_date"].dt.date)
        assert date(2026, 1, 30) not in weeks     # 1 bank that week
        assert date(2026, 1, 9) in weeks          # 2 banks

    def test_mean_ignores_banks_with_null_pd(self, seeded, tmp_path):
        conn = seeded
        conn.execute(
            "INSERT INTO pd_panel (week_date, permco, rssd, np_PD, merton_PD) "
            "VALUES (DATE '2026-01-09', 300, 3003, NULL, NULL)"
        )
        res = dx.export_dashboard(conn, tmp_path, min_banks=1)
        df = _rows(conn, res.paths["mean_pd"])
        row = df[df["week_date"].dt.date == date(2026, 1, 9)].iloc[0]
        assert row["n_banks"] == 2
        assert row["np_PD"] == pytest.approx(0.15)


class TestBridgedFlag:
    def test_bridged_week_is_true(self, seeded, tmp_path):
        _input(seeded, date(2026, 1, 9), 100, 1001, True)
        res = dx.export_dashboard(seeded, tmp_path, min_banks=1)
        df = _rows(seeded, res.paths["pd_panel"])
        hit = df[(df["permco"] == 100)
                 & (df["week_date"].dt.date == date(2026, 1, 9))].iloc[0]
        assert bool(hit["bs_bridged"]) is True

    def test_missing_input_row_is_false_not_null(self, seeded, tmp_path):
        """R's ifelse() propagates NA, so a NULL here would silently drop the
        marker instead of showing the week as ordinary."""
        res = dx.export_dashboard(seeded, tmp_path, min_banks=1)
        df = _rows(seeded, res.paths["pd_panel"])
        assert df["bs_bridged"].isna().sum() == 0
        assert not df["bs_bridged"].any()


class TestRebuild:
    def test_rerun_overwrites_rather_than_doubling(self, seeded, tmp_path):
        first = dx.export_dashboard(seeded, tmp_path, min_banks=1)
        second = dx.export_dashboard(seeded, tmp_path, min_banks=1)
        assert second.rows == first.rows
        assert _rows(seeded, second.paths["meta"]).shape[0] == 1

    def test_bundle_stays_within_the_free_tier_budget(self, seeded, tmp_path):
        res = dx.export_dashboard(seeded, tmp_path, min_banks=1)
        assert res.total_bytes < dx.MAX_TOTAL_BYTES
