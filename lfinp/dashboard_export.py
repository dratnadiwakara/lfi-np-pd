"""Build the minimal parquet feed behind the Shiny dashboard (shiny/).

Distinct from the snapshot writers in db.py, which archive the panel because a
previously-published number is otherwise unrecoverable. This export is the
opposite kind of artifact: fully rebuildable, publication-facing, and narrow.
Four small files, no vendor levels, no diagnostics.

    python -m lfinp.cli export-dashboard

The series key is `permco`, not `rssd`. Three permcos in this panel carry two
RSSDs across history (Regions, BNY Mellon, KeyCorp), so keying on rssd would
split those banks into part-series with a gap in the middle and no error.

Gates abort rather than degrade, same as the rest of the pipeline: an empty
panel, a duplicated permco, or a bank with no display name raises instead of
shipping a dashboard that is quietly wrong.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

import duckdb

from . import config

# Files this export owns. Named here so a stale file from an earlier version
# cannot survive a rebuild and be read by the app as if it were current.
FILES = ("pd_panel", "banks", "mean_pd", "meta")

# The bundle is deployed to a free shinyapps.io instance and read fully into
# memory at app startup. Well above today's ~0.4 MB; a breach means someone
# added a column that does not belong in a public feed.
MAX_TOTAL_BYTES = 2 * 1024 * 1024


@dataclass(frozen=True)
class DashboardExport:
    out_dir: Path
    paths: dict[str, Path]
    rows: dict[str, int]
    total_bytes: int
    week_min: Optional[str]
    week_max: Optional[str]

    def summary(self) -> str:
        return (
            f"{self.rows['pd_panel']:,} panel rows, {self.rows['banks']} banks, "
            f"{self.rows['mean_pd']:,} mean weeks, "
            f"{self.week_min} .. {self.week_max}, "
            f"{self.total_bytes / 1024:.0f} KB"
        )


def _copy(conn: duckdb.DuckDBPyConnection, sql: str, path: Path) -> int:
    """COPY a SELECT to parquet. DuckDB will not overwrite, so unlink first."""
    path.unlink(missing_ok=True)
    conn.execute(
        f"COPY ({sql}) TO '{path.as_posix()}' "
        "(FORMAT PARQUET, COMPRESSION ZSTD)"
    )
    return int(
        conn.execute(
            f"SELECT COUNT(*) FROM read_parquet('{path.as_posix()}')"
        ).fetchone()[0]
    )


PANEL_SQL = """
SELECT p.week_date, p.permco, p.np_PD, p.merton_PD,
       COALESCE(i.bs_bridged, FALSE) AS bs_bridged
FROM pd_panel p
LEFT JOIN pd_input i USING (permco, week_date)
WHERE p.np_PD IS NOT NULL OR p.merton_PD IS NOT NULL
ORDER BY p.permco, p.week_date
"""

# bank_group is the authority on scope (one row per bank, carries grp and the
# dead flag); ticker_hist supplies the trading symbol as of its last name row.
BANKS_SQL = """
WITH latest_tick AS (
  SELECT permco, ticker, comnam,
         ROW_NUMBER() OVER (PARTITION BY permco ORDER BY nameenddt DESC) AS rn
  FROM ticker_hist
)
SELECT bg.permco, bg.rssd,
       COALESCE(bg.name, t.comnam) AS name,
       t.ticker, bg.grp, bg.dead
FROM bank_group bg
LEFT JOIN latest_tick t ON t.permco = bg.permco AND t.rn = 1
WHERE bg.permco IN (SELECT DISTINCT permco FROM pd_panel)
ORDER BY name
"""

MEAN_SQL = """
SELECT week_date,
       COUNT(np_PD)   AS n_banks,
       AVG(np_PD)     AS np_PD,
       AVG(merton_PD) AS merton_PD
FROM pd_panel
GROUP BY week_date
HAVING COUNT(np_PD) >= {min_banks}
ORDER BY week_date
"""


def _check_banks(conn: duckdb.DuckDBPyConnection, path: Path) -> None:
    """A blank legend entry or a doubled series is the kind of thing that reads
    as data. Refuse to publish either."""
    p = path.as_posix()
    n, n_permco, n_nameless = conn.execute(
        f"""
        SELECT COUNT(*), COUNT(DISTINCT permco),
               COUNT(*) FILTER (WHERE name IS NULL OR name = '')
        FROM read_parquet('{p}')
        """
    ).fetchone()
    if not n:
        raise ValueError("dashboard export: banks.parquet is empty")
    if n != n_permco:
        dupes = conn.execute(
            f"""SELECT permco FROM read_parquet('{p}')
                GROUP BY permco HAVING COUNT(*) > 1"""
        ).fetchdf()["permco"].tolist()
        raise ValueError(
            f"dashboard export: {n - n_permco} duplicate permco row(s) in "
            f"banks.parquet: {dupes} - one row per bank or the selector "
            "offers the same bank twice"
        )
    if n_nameless:
        bad = conn.execute(
            f"""SELECT permco FROM read_parquet('{p}')
                WHERE name IS NULL OR name = ''"""
        ).fetchdf()["permco"].tolist()
        raise ValueError(
            f"dashboard export: {n_nameless} bank(s) with no display name: "
            f"permco {bad} - would render as a blank legend entry"
        )


def export_dashboard(
    conn: duckdb.DuckDBPyConnection,
    out_dir: Optional[Path] = None,
    *,
    min_banks: Optional[int] = None,
) -> DashboardExport:
    """Write pd_panel / banks / mean_pd / meta parquet into out_dir.

    Read-only against the store. Idempotent: each file is replaced whole, so a
    re-run produces the same bundle rather than appending to it.
    """
    target = Path(out_dir) if out_dir else config.dashboard_dir()
    target.mkdir(parents=True, exist_ok=True)
    mb = config.DASHBOARD_MIN_BANKS if min_banks is None else min_banks

    n_panel = int(conn.execute("SELECT COUNT(*) FROM pd_panel").fetchone()[0])
    if not n_panel:
        raise ValueError(
            "dashboard export: pd_panel is empty - run 'lfinp update' first"
        )

    paths = {name: target / f"{name}.parquet" for name in FILES}
    rows: dict[str, int] = {}

    rows["pd_panel"] = _copy(conn, PANEL_SQL, paths["pd_panel"])
    if not rows["pd_panel"]:
        raise ValueError(
            "dashboard export: every pd_panel row has NULL np_PD and NULL "
            "merton_PD - nothing to publish"
        )

    rows["banks"] = _copy(conn, BANKS_SQL, paths["banks"])
    _check_banks(conn, paths["banks"])

    rows["mean_pd"] = _copy(
        conn, MEAN_SQL.format(min_banks=int(mb)), paths["mean_pd"]
    )

    panel_pq = paths["pd_panel"].as_posix()
    week_min, week_max = conn.execute(
        f"SELECT MIN(week_date), MAX(week_date) FROM read_parquet('{panel_pq}')"
    ).fetchone()

    # meta rides with the data so the app can print its own vintage: a deploy
    # that was never refreshed is then visible on the page, not just in git.
    # built_at is text, not TIMESTAMP: R reads a parquet timestamp as UTC and
    # shifts it into the viewer's zone, which made the footer read four hours
    # before the export actually ran.
    meta_sql = (
        "SELECT "
        f"'{datetime.now():%Y-%m-%d %H:%M:%S}' AS built_at, "
        f"DATE '{week_min}' AS week_min, "
        f"DATE '{week_max}' AS week_max, "
        f"{rows['pd_panel']} AS n_panel_rows, "
        f"{rows['banks']} AS n_banks, "
        f"{int(mb)} AS min_banks"
    )
    rows["meta"] = _copy(conn, meta_sql, paths["meta"])

    total = sum(p.stat().st_size for p in paths.values())
    if total > MAX_TOTAL_BYTES:
        raise ValueError(
            f"dashboard export: bundle is {total / 1024 / 1024:.1f} MB, over the "
            f"{MAX_TOTAL_BYTES / 1024 / 1024:.0f} MB budget - the app reads every "
            "file into memory on a free shinyapps.io instance"
        )

    return DashboardExport(
        out_dir=target,
        paths=paths,
        rows=rows,
        total_bytes=total,
        week_min=str(week_min) if week_min else None,
        week_max=str(week_max) if week_max else None,
    )
