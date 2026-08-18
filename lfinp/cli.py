"""
lfi-np-pd CLI.

  lfinp init    schema + full backfill (CRSP history, Yahoo era, first compute)
  lfinp update  the one weekly command: FRED -> CRSP top-up -> Yahoo 15-month
                re-pull with gates -> rebuild panel -> targeted recompute ->
                post-checks. Non-zero exit on any gate failure.
  lfinp status  freshness + coverage + last check results (read-only)
  lfinp checks  standing diagnostics (read-only; --strict exits 1 on warns)

All console output is ASCII; stdout is reconfigured to UTF-8 defensively.
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

import pandas as pd

from . import checks as checks_mod
from . import compute as compute_mod
from . import config, db as db_mod, groups, panel, sources, yf
from .db import ack_flag, get_connection, init_schema


def _log(msg: str) -> None:
    from datetime import datetime
    print(f"[{datetime.now():%H:%M:%S}] {msg}")


def _parse_ticker_map(s: Optional[str]) -> dict[str, str]:
    if not s:
        return {}
    out = {}
    for pair in s.split(","):
        k, _, v = pair.partition("=")
        if not k or not v:
            raise SystemExit(f"Bad --ticker-map entry: {pair!r} (want CRSP=YF)")
        out[k.strip().upper()] = v.strip().upper()
    return out


def _scope(conn) -> tuple[dict[int, int], list[int], list[int]]:
    """Returns (rssd->permco map, all permcos, live permcos)."""
    banks = config.load_banks()
    mapping = sources.permcos_for_rssds(conn, [b.rssd for b in banks])
    all_permcos = sorted(set(mapping.values()))
    live_permcos = sorted({mapping[b.rssd] for b in banks if not b.dead})
    return mapping, all_permcos, live_permcos


def _crsp_edge(conn) -> Optional[date]:
    row = conn.execute("SELECT MAX(date) FROM crsp_daily").fetchone()
    return row[0] if row else None


def _run_yahoo(
    conn,
    live_permcos: list[int],
    *,
    window_start: date,
    ticker_map: dict[str, str],
    allow_share_jump: bool,
    skip_level_check: bool,
) -> tuple[int, dict]:
    """Pull, gate, insert. Returns (rows_inserted, recompute_spans).
    Raises SystemExit on gate failure — nothing inserted in that case."""
    _log(f"yahoo pull: {len(live_permcos)} permco(s), window {window_start}..today")
    pulls = yf.pull_all(conn, live_permcos, window_start=window_start,
                        ticker_map=ticker_map)
    for pr in pulls.values():
        for w in pr.warnings:
            _log(f"  WARN {pr.ticker}: {w}")

    c1 = checks_mod.check_share_jumps(pulls).log(conn)
    _log(f"check share_jumps: {c1.summary}")
    if not c1.passed and not allow_share_jump:
        raise SystemExit(f"ABORT: {c1.summary}")

    c2 = checks_mod.check_crsp_xval(conn, pulls).log(conn)
    _log(f"check crsp_xval: {c2.summary}")
    if not c2.passed:
        raise SystemExit(f"ABORT: {c2.summary}")

    c4 = checks_mod.check_level_continuity(conn, pulls).log(conn)
    _log(f"check level_continuity: {c4.summary}")
    if not c4.passed and not skip_level_check:
        raise SystemExit(f"ABORT: {c4.summary}")

    diff = yf.diff_against_stored(conn, pulls)
    c3 = checks_mod.record_pull_diff(conn, diff).log(conn)
    _log(f"check pull_diff: {c3.summary}")
    spans = checks_mod.recompute_spans(diff)
    for permco, (d0, d1) in spans.items():
        _log(f"  revision: permco {permco} changed from {d0} -> recompute through {d1}")

    n = yf.replace_window(conn, pulls, window_start)
    _log(f"yf_daily: replaced window with {n} row(s)")
    return n, spans


def _post_checks(conn, rssds: list[int], *, since: Optional[date],
                 strict: bool = False) -> bool:
    """Output-side anomaly suite. Warn-level by default: these describe the
    computed panel, and a real crisis legitimately trips them. Anything
    listed has NOT been acked, so it is unexplained by construction."""
    results = [
        checks_mod.check_panel_revisions(conn),
        checks_mod.check_se_steps(conn, rssds, since=since),
        checks_mod.check_peer_divergence(conn, rssds, since=since),
        checks_mod.check_pd_plausibility(conn, rssds, since=since),
        checks_mod.check_pd_decoupling(conn, rssds, since=since),
        checks_mod.check_bs_jumps(conn, rssds, since=since),
        checks_mod.check_fallback_rate(conn, rssds),
        checks_mod.check_group_composition(conn, since=since),
        checks_mod.check_issuance_bs_lag(conn, rssds, since=since),
    ]
    any_flag = False
    for r in results:
        r.log(conn)
        mark = "OK  " if r.passed else "FLAG"
        _log(f"check {r.name:<18} [{mark}] {r.summary}")
        if not r.passed:
            any_flag = True
            if len(r.details):
                print(r.details.head(25).to_string(index=False))
                if len(r.details) > 25:
                    print(f"  ... {len(r.details) - 25} more")
    if any_flag:
        _log("review flags, then suppress the explained ones:")
        _log("  lfinp ack --check <name> --rssd <id> --date <YYYY-MM-DD> "
             "--verdict real --note '...'")
    return not (any_flag and strict)


def _snapshot_panel_file(conn) -> None:
    """Archive the published panel and the inputs behind it. Runs after the
    checks, so a file always corresponds to a panel whose gates were
    evaluated. The pair is self-contained: outputs, inputs, and the return
    series each sE came from, so an old number can be re-derived without the
    live store."""
    p_panel = db_mod.export_panel_snapshot(conn)
    if p_panel is None:
        _log("snapshot: pd_panel empty, nothing archived")
        return
    p_input = db_mod.export_input_snapshot(conn)
    p_group = db_mod.export_group_snapshot(conn)
    n = len(list(config.snapshot_dir().glob("pd_panel_*.parquet")))
    _log(f"snapshot: wrote {p_panel.name}"
         + (f" + {p_input.name}" if p_input else "")
         + (f" + {p_group.name}" if p_group else "")
         + f" ({n} archived run(s) in {config.snapshot_dir()})")


def _rebuild_and_compute(
    conn,
    all_permcos: list[int],
    *,
    recompute_since: Optional[date],
) -> int:
    """Rebuild fred_weekly + pd_input, then compute. Incremental for new
    weeks; when recompute_since is set, also recompute everything from that
    date (revision-driven)."""
    n_fw = panel.build_fred_weekly(conn)
    _log(f"fred_weekly: {n_fw} row(s)")
    n_pi = panel.build_pd_input(conn, all_permcos)
    _log(f"pd_input: {n_pi} row(s)")
    # Group indices are built from pd_input, so they are ready before compute
    # and ride along in the same kernel call. The Delaunay build is a fixed
    # cost per call - running groups separately would pay it twice.
    _log(f"group_daily: {groups.build_group_daily(conn)} row(s)")
    _log(f"group_input: {groups.build_group_input(conn)} row(s)")

    since = recompute_since.isoformat() if recompute_since else None
    incremental = recompute_since is None
    df = compute_mod.assemble_inputs(
        conn, permco_filter=all_permcos,
        week_date_min=since, exclude_existing=incremental,
    )
    df_g = groups.assemble_group_inputs(
        conn, week_date_min=since, exclude_existing=incremental,
    )
    if df.empty and df_g.empty:
        _log("compute: nothing to do")
        return 0
    n_snap = db_mod.snapshot_panel(conn)
    _log(f"snapshot: froze {n_snap} pd_panel row(s) for revision diffing")
    _log(f"compute: {len(df)} bank row(s) + {len(df_g)} group row(s) "
         f"(Delaunay build dominates; ~15-20 min)")
    both = pd.concat([df, df_g], ignore_index=True) if not df_g.empty else df
    result = compute_mod.run_compute(both)
    # groups carry synthetic negative permcos; that is the whole split rule
    is_grp = result["permco"] < 0
    n = compute_mod.upsert_pd_panel(conn, result[~is_grp])
    _log(f"pd_panel: upserted {n} row(s)")
    ng = groups.upsert_group_panel(conn, result[is_grp])
    _log(f"group_panel: upserted {ng} row(s)")
    return n


# -- subcommands ------------------------------------------------------------------


def cmd_init(args) -> int:
    conn = get_connection()
    try:
        init_schema(conn)
        _log("schema initialised")
        if args.dry_run:
            for t in conn.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema='main' ORDER BY 1"
            ).fetchall():
                print(f"  {t[0]}")
            return 0

        n = sources.refresh_link(conn)
        _log(f"link: {n} row(s)")
        mapping, all_permcos, live_permcos = _scope(conn)
        for rssd, permco in sorted(mapping.items()):
            _log(f"  rssd {rssd} -> permco {permco}")
        _log(f"bank_group: {db_mod.sync_bank_groups(conn, mapping)} bank(s)")
        _log(f"merger_event: {db_mod.sync_mergers(conn)} merger(s)")

        secrets = config.load_secrets()
        wdb = sources.connect_wrds(secrets.wrds_username, secrets.wrds_password)
        _log("WRDS connected")
        n = sources.fetch_ticker_hist(conn, wdb, all_permcos)
        _log(f"ticker_hist: {n} row(s)")

        v2max = None
        bad = None
        if not args.no_v2:
            v2max = sources.v2_max_date(wdb)
            _log(f"crsp.dsf_v2 max date: {v2max}")
            # Legacy edge discovered after the legacy fetch; validate first
            if v2max and v2max >= date(2025, 6, 30):
                bad = sources.validate_v2_against_legacy(wdb, all_permcos)
                if len(bad):
                    _log(f"WARN dsf_v2 disagrees with legacy dsf on {len(bad)} row(s); "
                         f"v2 NOT used. First rows:\n{bad.head(10).to_string()}")
                else:
                    _log("dsf_v2 validated against legacy dsf (retx 1e-8, mcap 0.1%)")
        n, crsp_min = sources.fetch_crsp_daily(conn, wdb, all_permcos)
        _log(f"crsp_daily (legacy dsf): +{n} row(s)")
        edge = _crsp_edge(conn)
        if v2max and edge and v2max > edge and bad is not None and not len(bad):
            n, v2_min = sources.fetch_crsp_daily(conn, wdb, all_permcos,
                                                 use_v2_after=edge.isoformat())
            _log(f"crsp_daily (dsf_v2 past {edge}): +{n} row(s)")
            if v2_min is not None and (crsp_min is None or v2_min < crsp_min):
                crsp_min = v2_min
            edge = _crsp_edge(conn)
        _log(f"CRSP edge: {edge}")

        n = sources.fetch_dgs10_incremental(conn, secrets.fred_api_key)
        _log(f"fred_dgs10: +{n} row(s)")

        # Yahoo backfill: from 6 months before the CRSP edge (gives the
        # cross-validation check a solid overlap) through today.
        window_start = (edge - timedelta(days=183)) if edge else date.today()
        _run_yahoo(
            conn, live_permcos,
            window_start=window_start,
            ticker_map=_parse_ticker_map(args.ticker_map),
            allow_share_jump=args.allow_share_jump,
            skip_level_check=args.skip_level_check,
        )
        cov = checks_mod.check_coverage(conn, live_permcos).log(conn)
        _log(f"check coverage: {cov.summary}")
        if not cov.passed:
            raise SystemExit(f"ABORT: {cov.summary}")

        # New CRSP rows replace Yahoo rows in the view and shift every vol
        # window touching them -> recompute from the earliest new CRSP day.
        _rebuild_and_compute(conn, all_permcos, recompute_since=crsp_min)
        _post_checks(conn, config.active_rssds(), since=None)
        _snapshot_panel_file(conn)
        _log("init complete")
        return 0
    finally:
        conn.close()


def cmd_update(args) -> int:
    conn = get_connection()
    try:
        init_schema(conn)
        mapping, all_permcos, live_permcos = _scope(conn)
        _log(f"bank_group: {db_mod.sync_bank_groups(conn, mapping)} bank(s)")
        _log(f"merger_event: {db_mod.sync_mergers(conn)} merger(s)")
        secrets = config.load_secrets()

        n = sources.fetch_dgs10_incremental(conn, secrets.fred_api_key)
        _log(f"fred_dgs10: +{n} row(s)")

        crsp_min = None
        if not args.no_wrds:
            try:
                wdb = sources.connect_wrds(secrets.wrds_username, secrets.wrds_password)
                n, crsp_min = sources.fetch_crsp_daily(conn, wdb, all_permcos)
                _log(f"crsp_daily top-up: +{n} row(s)")
            except Exception as exc:  # noqa: BLE001
                _log(f"WARN WRDS unavailable, continuing on Yahoo alone: {exc}")

        ws, _ = yf.window_bounds()
        _, spans = _run_yahoo(
            conn, live_permcos,
            window_start=ws,
            ticker_map=_parse_ticker_map(args.ticker_map),
            allow_share_jump=args.allow_share_jump,
            skip_level_check=args.skip_level_check,
        )
        cov = checks_mod.check_coverage(conn, live_permcos).log(conn)
        _log(f"check coverage: {cov.summary}")
        if not cov.passed:
            raise SystemExit(f"ABORT: {cov.summary}")

        candidates = [d0 for d0, _ in spans.values()]
        if crsp_min is not None:
            candidates.append(crsp_min)
        recompute_since = min(candidates, default=None)
        _rebuild_and_compute(conn, all_permcos, recompute_since=recompute_since)
        # Post-checks scoped to the rolling era: older weeks are frozen CRSP
        # history that init already triaged.
        _post_checks(conn, config.active_rssds(),
                     since=date.today() - timedelta(days=430))
        _snapshot_panel_file(conn)
        _log("update complete")
        return 0
    finally:
        conn.close()


def _print_dashboard_status(conn) -> None:
    """One line on the Shiny feed. The export is manual, so the failure mode is
    forgetting it: the public chart silently keeps showing last month's panel.
    This makes that visible next to the tables it is derived from."""
    meta = config.dashboard_dir() / "meta.parquet"
    if not meta.exists():
        print(f"  {'dashboard':<12} not exported yet "
              "(lfinp export-dashboard)")
        return
    try:
        built_at, wk = conn.execute(
            f"SELECT built_at, week_max FROM read_parquet('{meta.as_posix()}')"
        ).fetchone()
        panel_max = conn.execute(
            "SELECT MAX(week_date) FROM pd_panel"
        ).fetchone()[0]
    except Exception as exc:  # noqa: BLE001
        print(f"  {'dashboard':<12} unreadable: {exc}")
        return
    mark = "" if panel_max is None or wk >= panel_max else \
        f"   STALE (panel has {panel_max})"
    print(f"  {'dashboard':<12} latest {wk}   built {str(built_at)[:16]}{mark}")


def cmd_status(args) -> int:
    conn = get_connection(read_only=True)
    try:
        today = date.today()
        print(f"=== lfi-np-pd status ===  today {today}")
        for label, sql in [
            ("crsp_daily", "SELECT MAX(date), COUNT(*) FROM crsp_daily"),
            ("yf_daily", "SELECT MAX(date), COUNT(*) FROM yf_daily"),
            ("fred_dgs10", "SELECT MAX(date), COUNT(*) FROM fred_dgs10"),
            ("pd_input", "SELECT MAX(week_date), COUNT(*) FROM pd_input"),
            ("pd_panel", "SELECT MAX(week_date), COUNT(*) FROM pd_panel"),
            ("group_panel", "SELECT MAX(week_date), COUNT(*) FROM group_panel"),
        ]:
            mx, n = conn.execute(sql).fetchone()
            print(f"  {label:<12} latest {mx}   rows {n:,}")
        snaps = sorted(config.snapshot_dir().glob("pd_panel_*.parquet"))
        if snaps:
            print(f"  {'snapshots':<12} latest "
                  f"{snaps[-1].stem.removeprefix('pd_panel_')}   files {len(snaps)}")
        else:
            print(f"  {'snapshots':<12} none yet")
        _print_dashboard_status(conn)
        print("\nPer-bank pd_panel tail:")
        df = conn.execute(
            """
            SELECT p.rssd,
                   (SELECT name FROM link l WHERE l.rssd = p.rssd
                    ORDER BY quarter_end DESC LIMIT 1) AS name,
                   MAX(p.week_date) AS last_week,
                   MAX(np_PD) FILTER (WHERE p.week_date =
                       (SELECT MAX(week_date) FROM pd_panel)) AS np_pd_latest
            FROM pd_panel p GROUP BY p.rssd ORDER BY name
            """
        ).fetchdf()
        # from banks.txt, not the bank_group table: status runs read-only and
        # must still work against a store written before the table existed.
        grp = config.bank_groups()
        df.insert(1, "group", df["rssd"].map(grp).fillna("?"))
        df = df.sort_values(["group", "name"], kind="stable")
        print(df.to_string(index=False))
        n_grp = conn.execute("SELECT COUNT(*) FROM group_panel").fetchone()[0]
        if n_grp:
            print("\nGroup panel (each group as one merged bank):")
            gdf = conn.execute(
                """
                SELECT grp, MAX(week_date) AS last_week,
                       MAX(n_members) FILTER (WHERE week_date =
                           (SELECT MAX(week_date) FROM group_panel)) AS members,
                       MAX(sE)    FILTER (WHERE week_date =
                           (SELECT MAX(week_date) FROM group_panel)) AS sE,
                       MAX(np_PD) FILTER (WHERE week_date =
                           (SELECT MAX(week_date) FROM group_panel)) AS np_pd_latest
                FROM group_panel GROUP BY grp ORDER BY grp
                """
            ).fetchdf()
            print(gdf.to_string(index=False))
        print("\nLast check results:")
        df = conn.execute(
            """
            SELECT check_name, run_at, passed, details FROM (
              SELECT *, ROW_NUMBER() OVER (PARTITION BY check_name
                                           ORDER BY run_at DESC) AS rn
              FROM check_log) WHERE rn = 1 ORDER BY check_name
            """
        ).fetchdf()
        for r in df.itertuples():
            mark = "OK  " if r.passed else "FAIL"
            print(f"  [{mark}] {r.check_name:<18} {r.run_at}  {r.details[:100]}")
        return 0
    finally:
        conn.close()


def cmd_checks(args) -> int:
    """Read-only standing diagnostics. Default scope is the rolling era;
    --all sweeps the full history (noisy until flags are acked)."""
    conn = get_connection(read_only=True)
    try:
        _, all_permcos, live_permcos = _scope(conn)
        rssds = config.active_rssds()
        if args.all:
            since = None
        elif args.years:
            since = date.today() - timedelta(days=round(args.years * 365.25))
        else:
            since = date.today() - timedelta(days=430)
        _log(f"scope: {len(rssds)} banks, since {since or 'inception'}")
        failed = False

        cov = checks_mod.check_coverage(conn, live_permcos)
        print(f"[{'OK  ' if cov.passed else 'FAIL'}] coverage: {cov.summary}")
        failed |= not cov.passed

        for r in [
            checks_mod.check_se_steps(conn, rssds, since=since),
            checks_mod.check_peer_divergence(conn, rssds, since=since),
            checks_mod.check_pd_plausibility(conn, rssds, since=since),
            checks_mod.check_pd_decoupling(conn, rssds, since=since),
            checks_mod.check_bs_jumps(conn, rssds, since=since),
            checks_mod.check_fallback_rate(conn, rssds),
            checks_mod.check_group_composition(conn, since=since),
            checks_mod.check_issuance_bs_lag(conn, rssds, since=since),
        ]:
            mark = "OK  " if r.passed else "FLAG"
            print(f"[{mark}] {r.name}: {r.summary}")
            if not r.passed:
                if len(r.details):
                    print(r.details.head(40).to_string(index=False))
                if args.strict:
                    failed = True
        return 1 if failed else 0
    finally:
        conn.close()


def _run_named_check(conn, name: str, rssds: list[int], permcos: list[int]):
    fns = {
        "se_steps": lambda: checks_mod.check_se_steps(conn, rssds),
        "peer_divergence": lambda: checks_mod.check_peer_divergence(conn, rssds),
        "pd_plausibility": lambda: checks_mod.check_pd_plausibility(conn, rssds),
        "pd_decoupling": lambda: checks_mod.check_pd_decoupling(conn, rssds),
        "bs_jumps": lambda: checks_mod.check_bs_jumps(conn, rssds),
        "fallback_rate": lambda: checks_mod.check_fallback_rate(conn, rssds),
        "panel_revisions": lambda: checks_mod.check_panel_revisions(conn),
        "group_composition": lambda: checks_mod.check_group_composition(conn),
        "issuance_bs_lag": lambda: checks_mod.check_issuance_bs_lag(conn, rssds),
    }
    if name not in fns:
        raise SystemExit(f"unknown check {name!r}; choose from {sorted(fns)}")
    return fns[name]()


def cmd_ack(args) -> int:
    """Record that flags have been reviewed and explained, suppressing them
    from future runs. This is what keeps the checks meaningful: an un-acked
    flag is by definition something nobody has looked at.

    Two forms: an exact flag (--date), or every outstanding flag of that
    check in a window (--from/--to, optionally --rssd all). The window form
    re-runs the check and only acks flags that actually exist, so a typo
    acks nothing rather than silently muting a future flag."""
    conn = get_connection()
    try:
        init_schema(conn)
        _, all_permcos, _ = _scope(conn)
        rssds = config.active_rssds()

        if args.date:
            d = date.fromisoformat(args.date)
            targets = [(int(x), d) for x in args.rssd.split(",") if x.strip()]
        else:
            if not (args.__dict__.get("from_date") and args.to):
                raise SystemExit("need either --date or both --from and --to")
            d0 = date.fromisoformat(args.from_date)
            d1 = date.fromisoformat(args.to)
            r = _run_named_check(conn, args.check, rssds, all_permcos)
            det = r.details
            if not len(det):
                _log(f"no outstanding {args.check} flags to ack")
                return 0
            want = None if args.rssd in (None, "all") else {
                int(x) for x in args.rssd.split(",") if x.strip()}
            targets = [
                (int(row["rssd"]), row["ref_date"])
                for _, row in det.iterrows()
                if d0 <= row["ref_date"] <= d1
                and (want is None or int(row["rssd"]) in want)
            ]
            if not targets:
                _log(f"no {args.check} flags in {d0}..{d1}")
                return 0

        for rssd, d in targets:
            ack_flag(conn, args.check, rssd, d, args.verdict, args.note or "")
        _log(f"acked {len(targets)} {args.check} flag(s) as '{args.verdict}'"
             + (f": {args.note}" if args.note else ""))
        return 0
    finally:
        conn.close()


def cmd_export_dashboard(args) -> int:
    """Rebuild the parquet feed the Shiny app reads (shiny/data/).

    Read-only on the store: DuckDB is single-writer, so a dashboard refresh
    must never be able to block a weekly run. Deliberately not called from
    update - publishing is a human decision, and nothing new should be able to
    fail a 20-minute run."""
    from . import dashboard_export

    conn = get_connection(read_only=True)
    try:
        res = dashboard_export.export_dashboard(
            conn,
            Path(args.out) if args.out else None,
            min_banks=args.min_banks,
        )
        _log(f"dashboard: {res.summary()}")
        for name in dashboard_export.FILES:
            p = res.paths[name]
            _log(f"  {p.name:<18} {res.rows[name]:>7,} rows  "
                 f"{p.stat().st_size / 1024:>7.1f} KB")
        _log(f"wrote to {res.out_dir}")
        _log("review locally, then commit shiny/data and deploy:")
        _log("  shiny::runApp('shiny', port = 4500, launch.browser = TRUE)")
        return 0
    finally:
        conn.close()


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

    p = argparse.ArgumentParser(prog="lfinp", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    for name, fn in [("init", cmd_init), ("update", cmd_update)]:
        sp = sub.add_parser(name)
        sp.add_argument("--ticker-map", help="CRSP=YF overrides, comma-separated")
        sp.add_argument("--allow-share-jump", action="store_true",
                        help="proceed despite a recent unexplained share jump")
        sp.add_argument("--skip-level-check", action="store_true",
                        help="bypass the market-cap level continuity gate")
        sp.set_defaults(func=fn)
        if name == "init":
            sp.add_argument("--dry-run", action="store_true",
                            help="create schema, print tables, stop")
            sp.add_argument("--no-v2", action="store_true",
                            help="skip crsp.dsf_v2 even if available")
        else:
            sp.add_argument("--no-wrds", action="store_true",
                            help="skip the WRDS top-up attempt")

    sp = sub.add_parser("status")
    sp.set_defaults(func=cmd_status)

    sp = sub.add_parser("checks", help="read-only anomaly diagnostics")
    sp.add_argument("--strict", action="store_true",
                    help="exit 1 when any flag is outstanding")
    sp.add_argument("--all", action="store_true",
                    help="sweep full history instead of the rolling era")
    sp.add_argument("--years", type=float,
                    help="look back N years (default: 430 days rolling era)")
    sp.set_defaults(func=cmd_checks)

    sp = sub.add_parser("export-dashboard",
                        help="rebuild shiny/data/*.parquet for the Shiny app")
    sp.add_argument("--out", help="output dir (default shiny/data)")
    sp.add_argument("--min-banks", type=int, default=None,
                    help="drop mean-PD weeks thinner than this "
                         f"(default {config.DASHBOARD_MIN_BANKS})")
    sp.set_defaults(func=cmd_export_dashboard)

    sp = sub.add_parser("ack", help="mark reviewed flags as explained")
    sp.add_argument("--check", required=True,
                    help="check name, e.g. se_steps / peer_divergence")
    sp.add_argument("--rssd", default="all",
                    help="RSSD(s) comma-separated, or 'all' with --from/--to")
    sp.add_argument("--date", help="exact flag ref_date (YYYY-MM-DD)")
    sp.add_argument("--from", dest="from_date",
                    help="window start; acks every matching flag in the range")
    sp.add_argument("--to", help="window end")
    sp.add_argument("--verdict", default="real",
                    choices=["real", "artifact", "accepted"])
    sp.add_argument("--note", help="why - future you will want this")
    sp.set_defaults(func=cmd_ack)

    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
