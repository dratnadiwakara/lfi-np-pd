"""links.txt: permco -> RSSD overrides for filers the vendor link did not follow.

The defect: Flagstar's holding company (RSSD 2132932) merged into its bank on
2025-10-17 and stopped filing Y-9C at 2025-09-30, but the CRSP-FRB link kept
mapping permco 12608 to 2132932 through 2026-06-30. The panel picks a week's
filer by ASOF on (permco, quarter_end), so every week from 2025-10 on would
price 2026 equity against the 2025-09-30 balance sheet, with no abort.

What these tests pin:
1. The file parses exactly or raises, like banks.txt and mergers.txt.
2. An override replaces the vendor rows from from= on. Adding beside them would
   leave two RSSDs in one (permco, quarter_end) and make the ASOF pick
   arbitrary.
3. Weeks before from= still see the vendor's RSSD.
4. If the vendor ever links the new RSSD to another permco, scope resolution
   aborts rather than picking one.
"""
from __future__ import annotations

from datetime import date

import duckdb
import pytest

from lfinp import config, sources
from lfinp import db as db_mod


def write_links(tmp_path, body: str):
    p = tmp_path / "links.txt"
    p.write_text(body, encoding="utf-8")
    return p


class TestLinksFile:
    def test_parses_an_override(self, tmp_path):
        p = write_links(tmp_path, "# c\n12608  694904  from=2025-12-31  # FLG\n")
        (o,) = config.load_link_overrides(p)
        assert (o.permco, o.rssd, o.from_quarter) == (12608, 694904, date(2025, 12, 31))
        assert o.comment == "FLG"

    def test_missing_file_is_not_an_error(self, tmp_path):
        assert config.load_link_overrides(tmp_path / "nope.txt") == []

    @pytest.mark.parametrize("line", [
        "12608 694904",                        # no from=
        "12608 694904 from=2025-11-30",        # not a quarter end
        "12608 694904 from=31-12-2025",        # not ISO
        "12608 694904 to=2025-12-31",          # wrong key
        "12608 x from=2025-12-31",             # non-integer
        "12608 0 from=2025-12-31",             # non-positive
        "12608 694904 from=2025-12-31 extra",  # trailing token
    ])
    def test_bad_lines_raise(self, tmp_path, line):
        with pytest.raises(ValueError):
            config.load_link_overrides(write_links(tmp_path, line + "\n"))

    def test_duplicate_permco_raises(self, tmp_path):
        p = write_links(tmp_path, "12608 694904 from=2025-12-31\n"
                                  "12608 694905 from=2026-03-31\n")
        with pytest.raises(ValueError, match="Duplicate"):
            config.load_link_overrides(p)

    def test_the_real_file_parses(self):
        assert {o.permco for o in config.load_link_overrides()} == {43147, 12608}


@pytest.fixture()
def conn(tmp_path):
    c = duckdb.connect(str(tmp_path / "t.duckdb"))
    db_mod.init_schema(c)
    # Flagstar's vendor rows: the dead holding company RSSD on every quarter.
    c.executemany(
        "INSERT INTO link VALUES (12608, 2132932, ?, 'NYCB', ?)",
        [(date(2025, 6, 30), True), (date(2025, 9, 30), False),
         (date(2025, 12, 31), False), (date(2026, 3, 31), False),
         (date(2026, 6, 30), False)])
    yield c
    c.close()


FLG = config.LinkOverride(permco=12608, rssd=694904,
                          from_quarter=date(2025, 12, 31), comment="FLG")


class TestApply:
    def test_replaces_vendor_rows_from_the_quarter_on(self, conn):
        n = sources.apply_link_overrides(conn, [FLG])
        assert n == 3, "2025-12-31, 2026-03-31, 2026-06-30"
        rows = conn.execute("SELECT quarter_end, rssd, confirmed FROM link "
                            "WHERE permco = 12608 ORDER BY quarter_end").fetchall()
        assert rows == [
            (date(2025, 6, 30), 2132932, True),
            (date(2025, 9, 30), 2132932, False),
            (date(2025, 12, 31), 694904, True),
            (date(2026, 3, 31), 694904, True),
            (date(2026, 6, 30), 694904, True),
        ]

    def test_one_rssd_per_permco_quarter(self, conn):
        sources.apply_link_overrides(conn, [FLG])
        dup = conn.execute("SELECT permco, quarter_end FROM link GROUP BY 1, 2 "
                           "HAVING COUNT(*) > 1").fetchall()
        assert dup == [], "two RSSDs in one quarter make the ASOF pick arbitrary"

    def test_asof_switches_filer_at_the_quarter(self, conn):
        """The panel's own join shape: weeks before from= keep the old filer."""
        sources.apply_link_overrides(conn, [FLG])
        got = dict(conn.execute("""
            SELECT w.d, l.rssd FROM (VALUES (DATE '2025-11-07'), (DATE '2026-01-02'),
                                            (DATE '2026-10-02')) w(d)
            ASOF LEFT JOIN link l ON l.permco = 12608 AND w.d >= l.quarter_end
        """).fetchall())
        assert got == {date(2025, 11, 7): 2132932, date(2026, 1, 2): 694904,
                       date(2026, 10, 2): 694904}

    def test_new_rssd_resolves_to_the_permco(self, conn):
        sources.apply_link_overrides(conn, [FLG])
        assert sources.permcos_for_rssds(conn, [694904]) == {694904: 12608}

    def test_vendor_linking_the_new_rssd_elsewhere_aborts(self, conn):
        """If the vendor catches up under a different permco, the override is
        stale. Two permcos for one RSSD must stop the build."""
        conn.execute("INSERT INTO link VALUES (99999, 694904, DATE '2026-06-30', 'x', TRUE)")
        sources.apply_link_overrides(conn, [FLG])
        with pytest.raises(RuntimeError, match="multiple permcos"):
            sources.permcos_for_rssds(conn, [694904])

    def test_no_overrides_changes_nothing(self, conn):
        before = conn.execute("SELECT * FROM link ORDER BY ALL").fetchall()
        assert sources.apply_link_overrides(conn, []) == 0
        assert conn.execute("SELECT * FROM link ORDER BY ALL").fetchall() == before
