"""pd_panel snapshot archive.

The store answers "what is this PD now" and "what changed since the last run"
(pd_panel_prev, one run deep). It cannot answer "what did this PD read six
months ago" — values inside the rolling window move by design. The dated
parquet files are that record, so these tests pin the properties an auditor
would rely on: one complete panel per run, readable as one table, and a
same-day re-run overwriting rather than duplicating.
"""
from __future__ import annotations

from datetime import date

import duckdb
import pytest

from lfinp import db as db_mod


@pytest.fixture()
def conn(tmp_path):
    c = duckdb.connect(str(tmp_path / "t.duckdb"))
    db_mod.init_schema(c)
    yield c
    c.close()


def _insert(conn, week, permco, np_pd):
    conn.execute(
        "INSERT INTO pd_panel (week_date, permco, rssd, np_PD, merton_PD, sE) "
        "VALUES (?, ?, 1234, ?, 0.001, 0.3)",
        [week, permco, np_pd],
    )


def test_empty_panel_writes_nothing(conn, tmp_path):
    assert db_mod.export_panel_snapshot(conn, date(2026, 8, 21),
                                        out_dir=tmp_path / "s") is None


def test_snapshot_is_a_full_panel_not_a_delta(conn, tmp_path):
    out = tmp_path / "s"
    for wk in [date(2026, 8, 7), date(2026, 8, 14)]:
        _insert(conn, wk, 20265, 0.004)
    p = db_mod.export_panel_snapshot(conn, date(2026, 8, 14), out_dir=out)
    assert p.name == "pd_panel_2026-08-14.parquet"

    # a later run adds a week; the new file still carries the whole history
    _insert(conn, date(2026, 8, 21), 20265, 0.005)
    p2 = db_mod.export_panel_snapshot(conn, date(2026, 8, 21), out_dir=out)
    n = conn.execute(
        f"SELECT COUNT(*) FROM read_parquet('{p2.as_posix()}')").fetchone()[0]
    assert n == 3


def test_revision_is_recoverable_from_the_archive(conn, tmp_path):
    """The whole point: a historical PD that the pipeline later revised is
    still readable at its published value."""
    out = tmp_path / "s"
    _insert(conn, date(2026, 8, 7), 20265, 0.004)
    db_mod.export_panel_snapshot(conn, date(2026, 8, 14), out_dir=out)

    # a vendor revision inside the rolling window changes a published number
    conn.execute("UPDATE pd_panel SET np_PD = 0.009 WHERE week_date = DATE '2026-08-07'")
    db_mod.export_panel_snapshot(conn, date(2026, 8, 21), out_dir=out)

    glob = (out / "pd_panel_*.parquet").as_posix()
    rows = conn.execute(
        f"SELECT snapshot_date, np_PD FROM read_parquet('{glob}') "
        "WHERE week_date = DATE '2026-08-07' ORDER BY snapshot_date"
    ).fetchall()
    assert [r[1] for r in rows] == [0.004, 0.009]
    assert rows[0][0] == date(2026, 8, 14)


def _daily(conn, permco, dates, retxs):
    for dt, rx in zip(dates, retxs):
        conn.execute(
            "INSERT INTO yf_daily (permco, date, close, shares, market_cap, retx) "
            "VALUES (?, ?, 10.0, 1000.0, 10000.0, ?)",
            [permco, dt, rx],
        )


def _pd_input_row(conn, permco, week, date_eff, sE, n_obs):
    conn.execute(
        "INSERT INTO pd_input (permco, week_date, date_eff, rssd, sE, n_obs_252) "
        "VALUES (?, ?, ?, 1234, ?, ?)",
        [permco, week, date_eff, sE, n_obs],
    )


def _series(n):
    """Deterministic pseudo-returns; magnitude is irrelevant, exactness is not."""
    return [((i * 37 % 101) - 50) / 1000.0 for i in range(n)]


def test_retx_csv_reproduces_sE_exactly(conn, tmp_path):
    """The reason the column exists: sE must be re-derivable from the string
    alone, to the last bit. A close-but-not-equal value is useless in an
    audit."""
    import math

    days = [date(2026, 1, 1) + __import__("datetime").timedelta(days=i)
            for i in range(300)]
    rx = _series(300)
    _daily(conn, 20265, days, rx)
    eff = days[-1]
    window = rx[-252:]
    mean = sum(window) / len(window)
    var = sum((x - mean) ** 2 for x in window) / (len(window) - 1)
    sE = math.sqrt(var) * math.sqrt(252)
    _pd_input_row(conn, 20265, date(2026, 10, 30), eff, sE, 252)

    p = db_mod.export_input_snapshot(conn, date(2026, 11, 6), out_dir=tmp_path / "s")
    row = conn.execute(
        f"""SELECT retx_n_rows, retx_n_obs, retx_from, retx_to, sE,
                   (SELECT stddev_samp(CAST(v AS DOUBLE)) * sqrt(252)
                      FROM unnest(str_split(retx_csv, ',')) t(v)
                     WHERE v <> '') AS sE_redone
            FROM read_parquet('{p.as_posix()}')"""
    ).fetchone()
    assert row[0] == 252 and row[1] == 252
    assert row[2] == days[-252] and row[3] == eff
    assert abs(row[4] - row[5]) < 1e-12


def test_missing_return_holds_its_slot(conn, tmp_path):
    """The vol window is 252 ROWS, not 252 non-null returns. A NULL retx must
    be emitted as an empty field: dropping it would pull an extra day into the
    window and the string would no longer describe what sE saw."""
    days = [date(2026, 1, 1) + __import__("datetime").timedelta(days=i)
            for i in range(10)]
    rx = _series(10)
    rx[3] = None
    _daily(conn, 20265, days, rx)
    _pd_input_row(conn, 20265, date(2026, 1, 16), days[-1], 0.3, 9)

    p = db_mod.export_input_snapshot(conn, date(2026, 1, 16), out_dir=tmp_path / "s")
    csv, n_rows, n_obs = conn.execute(
        f"SELECT retx_csv, retx_n_rows, retx_n_obs FROM read_parquet('{p.as_posix()}')"
    ).fetchone()
    fields = csv.split(",")
    assert len(fields) == 10           # every row present
    assert fields[3] == ""             # the gap, in place
    assert (n_rows, n_obs) == (10, 9)


def test_input_snapshot_skips_weeks_with_no_equity_day(conn, tmp_path):
    """date_eff IS NULL (bank stopped trading) -> row kept, series NULL. The
    row must survive: a bank vanishing from the archive is the failure mode
    the coverage gate exists to prevent."""
    _pd_input_row(conn, 20265, date(2026, 1, 16), None, None, None)
    p = db_mod.export_input_snapshot(conn, date(2026, 1, 16), out_dir=tmp_path / "s")
    n, csv = conn.execute(
        f"SELECT COUNT(*), ANY_VALUE(retx_csv) FROM read_parquet('{p.as_posix()}')"
    ).fetchone()
    assert n == 1 and csv is None


def test_same_day_rerun_overwrites(conn, tmp_path):
    out = tmp_path / "s"
    _insert(conn, date(2026, 8, 7), 20265, 0.004)
    db_mod.export_panel_snapshot(conn, date(2026, 8, 21), out_dir=out)
    _insert(conn, date(2026, 8, 14), 20265, 0.005)
    p = db_mod.export_panel_snapshot(conn, date(2026, 8, 21), out_dir=out)
    assert len(list(out.glob("pd_panel_*.parquet"))) == 1
    n = conn.execute(
        f"SELECT COUNT(*) FROM read_parquet('{p.as_posix()}')").fetchone()[0]
    assert n == 2
