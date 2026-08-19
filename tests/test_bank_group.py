"""Business-model grouping.

banks.txt is the source of truth; bank_group mirrors it. The properties that
matter: every bank is classified (an unlabelled bank drops out of every
group-wise cut without anyone noticing), a bad label fails loudly at parse
time, and the mirror is a full replace so a removed bank cannot linger.
"""
from __future__ import annotations

import duckdb
import pytest

from lfinp import config
from lfinp import db as db_mod


def _write(tmp_path, body):
    p = tmp_path / "banks.txt"
    p.write_text(body, encoding="utf-8")
    return p


class TestParse:
    def test_every_real_bank_is_classified(self):
        banks = config.load_banks()
        assert len(banks) == 37
        assert all(b.group in config.BANK_GROUPS for b in banks), \
            "unclassified bank would vanish from group-wise cuts"

    def test_every_group_is_populated(self):
        counts = {}
        for b in config.load_banks():
            counts[b.group] = counts.get(b.group, 0) + 1
        assert set(counts) == set(config.BANK_GROUPS)
        # a group of one is a single bank wearing a label, not a peer set
        assert min(counts.values()) >= 2

    def test_the_two_broker_dealers_are_not_money_center(self):
        """GS/MS are wholesale-funded; JPM/BAC/C/WFC are deposit-funded. Merging
        them was the original mistake and the whole reason for a 4th group."""
        g = config.bank_groups()
        assert g[2380443] == "dealer" and g[2162966] == "dealer"   # GS, MS
        assert g[1039502] == "moneycenter" and g[1073757] == "moneycenter"

    def test_dead_bank_keeps_its_group(self):
        svb = [b for b in config.load_banks() if b.rssd == 1031449]
        assert len(svb) == 1 and svb[0].dead and svb[0].group == "lender"
        # SVB stays in published group history; the other failed banks do not
        assert not svb[0].nogroup

    def test_nogroup_banks_still_carry_a_group_label(self):
        for b in config.load_banks():
            if b.nogroup:
                assert b.group in config.BANK_GROUPS

    def test_missing_group_is_an_error(self, tmp_path):
        with pytest.raises(ValueError, match="Missing group"):
            config.load_banks(_write(tmp_path, "1039502  # JPM\n"))

    def test_unknown_group_is_an_error(self, tmp_path):
        with pytest.raises(ValueError, match="Unknown group"):
            config.load_banks(_write(tmp_path, "1039502 group=regional  # JPM\n"))

    def test_dead_and_group_compose(self, tmp_path):
        b = config.load_banks(_write(tmp_path, "1031449 dead group=lender # SVB\n"))[0]
        assert (b.dead, b.group) == (True, "lender")


class TestMirror:
    def test_sync_replaces_rather_than_merges(self, tmp_path, monkeypatch):
        conn = duckdb.connect(str(tmp_path / "t.duckdb"))
        db_mod.init_schema(conn)

        monkeypatch.setattr(config, "BANKS_PATH",
                            _write(tmp_path, "1039502 group=moneycenter # JPM\n"
                                             "1068025 group=lender    # KEY\n"))
        assert db_mod.sync_bank_groups(conn) == 2

        # bank dropped from scope must not linger with a stale group
        monkeypatch.setattr(config, "BANKS_PATH",
                            _write(tmp_path, "1039502 group=moneycenter # JPM\n"))
        assert db_mod.sync_bank_groups(conn) == 1
        rows = conn.execute("SELECT rssd, grp, dead FROM bank_group").fetchall()
        assert rows == [(1039502, "moneycenter", False)]
        conn.close()
