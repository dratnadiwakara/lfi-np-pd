"""
Yahoo Finance equity pull: rolling 15-month wholesale re-pull.

Design invariants (tested in tests/test_retx.py — do not weaken):

1. `retx` is computed ONLY from closes inside a single pull
   (close.pct_change on the pulled series). Yahoo back-adjusts prices when a
   stock splits, so a stored close and a freshly pulled close for the same
   date may be on different bases. Stored closes are audit data; they are
   NEVER differenced against new pulls.

2. `market_cap` uses split-corrected shares. `Ticker.get_shares_full()`
   returns raw as-reported share counts while `history()` closes are
   split-adjusted; multiplying them directly understates pre-split market
   cap by the split factor. We multiply each date's raw shares by the
   cumulative factor of all splits occurring AFTER that date (from the
   `Stock Splits` action column) before forming close * shares.

3. Share-count returns are never used for `retx`. Market-cap pct-change
   conflates issuance/buybacks/vendor-revisions with returns (observed:
   fake +65.8% on COF's Discover close, -33.9%/+49.0% on a pure NTRS
   vendor glitch) and is why this module exists.

4. The window is replaced wholesale (delete + insert) each run, so vendor
   revisions self-heal and the whole vol window shares one basis.

Share-count glitch repair: a level segment shorter than
SHARE_JUMP_PERSIST_DAYS that begins and ends with jumps and reverts to
within 2% of the prior level is treated as a vendor glitch and repaired to
the prior level (logged). Persistent jumps (M&A) are kept.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Optional

import duckdb
import numpy as np
import pandas as pd

from . import config

# History is pulled with this extra lead so that (a) the first in-window day
# has a within-pull previous close for retx, and (b) any split in the shares
# lookback shadow is visible in the actions column.
HISTORY_LEAD_DAYS = 130
SHARES_LOOKBACK_DAYS = 120


@dataclass
class PullResult:
    permco: int
    ticker: str
    df: pd.DataFrame                    # index date; close, shares, market_cap, retx, retx_synthetic
    warnings: list[str] = field(default_factory=list)
    shares_repairs: list[dict] = field(default_factory=list)   # {date, old, new}
    unexplained_recent_jump: Optional[dict] = None             # {date, ratio} -> abort signal


def window_bounds(today: Optional[date] = None) -> tuple[date, date]:
    """(window_start, window_end). Window = last YF_REPULL_MONTHS months."""
    end = today or date.today()
    start = end - timedelta(days=round(config.YF_REPULL_MONTHS * 30.44))
    return start, end


def resolve_ticker(
    conn: duckdb.DuckDBPyConnection,
    permco: int,
    overrides: Optional[dict[str, str]] = None,
) -> str:
    """CRSP latest ticker -> Yahoo symbol via alias map. Raises on missing
    history or on the ticker-reuse guard (CRSP ticker ended long before the
    CRSP edge -> symbol may now belong to an unrelated company)."""
    row = conn.execute(
        """
        SELECT ticker, nameenddt FROM ticker_hist
        WHERE permco = ? AND ticker IS NOT NULL
        ORDER BY nameenddt DESC, namedt DESC, ticker ASC LIMIT 1
        """,
        [permco],
    ).fetchone()
    if row is None:
        raise RuntimeError(f"permco {permco}: no ticker history")
    crsp_ticker, nameenddt = str(row[0]).upper(), row[1]
    aliases = dict(config.CRSP_TO_YF_ALIASES)
    if overrides:
        aliases.update({k.upper(): v for k, v in overrides.items()})
    edge = conn.execute("SELECT MAX(date) FROM crsp_daily").fetchone()[0]
    if edge is not None and crsp_ticker not in aliases:
        gap = (edge - nameenddt).days if hasattr(nameenddt, "toordinal") else 0
        if gap > config.TICKER_REUSE_GUARD_DAYS:
            raise RuntimeError(
                f"permco {permco}: ticker {crsp_ticker!r} last valid {nameenddt}, "
                f"{gap}d before CRSP edge {edge} - reuse risk. Add an alias to "
                f"CRSP_TO_YF_ALIASES or pass --ticker-map {crsp_ticker}=<YF>."
            )
    if crsp_ticker in aliases:
        return aliases[crsp_ticker]
    return crsp_ticker.replace(".", "-")


# -- shares handling ------------------------------------------------------------


def dedup_shares(shares: pd.Series) -> tuple[pd.Series, list[str]]:
    """Collapse duplicate timestamps. When duplicates disagree by >0.5%,
    pick the value closest to the median of the +-3 surrounding clean
    observations instead of trusting insertion order."""
    warnings: list[str] = []
    if shares.index.has_duplicates:
        grouped = shares.groupby(level=0)
        clean_vals: dict = {}
        ambiguous: dict = {}
        for ts, grp in grouped:
            vals = grp.values.astype(float)
            if len(vals) == 1 or (vals.max() / max(vals.min(), 1.0) - 1) <= 0.005:
                clean_vals[ts] = vals[-1]
            else:
                ambiguous[ts] = vals
        base = pd.Series(clean_vals).sort_index()
        for ts, vals in ambiguous.items():
            pos = base.index.searchsorted(ts)
            lo, hi = max(0, pos - 3), min(len(base), pos + 3)
            neighborhood = base.iloc[lo:hi]
            if len(neighborhood):
                target = float(np.median(neighborhood.values))
                pick = vals[np.argmin(np.abs(vals - target))]
            else:
                pick = vals[-1]
            base.loc[ts] = float(pick)
            warnings.append(
                f"ambiguous duplicate share counts at {ts.date() if hasattr(ts, 'date') else ts}: "
                f"{sorted(vals.tolist())} -> {pick:,.0f}"
            )
        return base.sort_index(), warnings
    return shares.sort_index(), warnings


def split_correct_shares(
    shares_raw: pd.Series, splits: pd.Series,
) -> pd.Series:
    """Multiply each date's raw share count by the cumulative factor of all
    splits strictly AFTER that date, putting shares on the same basis as
    Yahoo's split-adjusted closes."""
    s = shares_raw.copy()
    events = splits[splits > 0]
    for split_date, factor in events.items():
        s.loc[s.index < split_date] = s.loc[s.index < split_date] * float(factor)
    return s


def repair_share_glitches(
    shares: pd.Series,
    splits: pd.Series,
) -> tuple[pd.Series, list[dict], list[str], Optional[dict]]:
    """Detect and repair vendor share-count glitches on the daily (ffilled)
    series. Returns (repaired, repairs, warnings, unexplained_recent_jump).

    Segments the series at day-over-day jumps > SHARE_JUMP_THRESHOLD that no
    split explains. A short segment (< SHARE_JUMP_PERSIST_DAYS) whose exit
    level reverts to within 2% of the entry level is a glitch -> repaired to
    the prior level. A persistent new level (M&A, issuance) is kept. An
    unexplained jump within the last 5 trading days that cannot show
    persistence yet is returned as an abort signal."""
    s = shares.astype(float).copy()
    warnings: list[str] = []
    repairs: list[dict] = []
    unexplained_recent: Optional[dict] = None

    ratio = s / s.shift(1)
    jump_idx = [
        ts for ts, rv in ratio.items()
        if pd.notna(rv) and abs(rv - 1.0) > config.SHARE_JUMP_THRESHOLD
    ]
    split_events = splits[splits > 0]

    def _split_explains(ts, rv) -> bool:
        for sd, f in split_events.items():
            if abs((pd.Timestamp(sd) - pd.Timestamp(ts)).days) <= 5:
                if abs(rv / float(f) - 1.0) <= 0.02 or abs(rv * float(f) - 1.0) <= 0.02:
                    return True
        return False

    unexplained = [(ts, float(ratio[ts])) for ts, _ in
                   [(t, None) for t in jump_idx]
                   if not _split_explains(ts, float(ratio[ts]))]

    i = 0
    positions = {ts: n for n, ts in enumerate(s.index)}
    last_pos = len(s) - 1
    while i < len(unexplained):
        ts, rv = unexplained[i]
        pos = positions[ts]
        pre_level = float(s.iloc[pos - 1]) if pos > 0 else float(s.iloc[pos])
        # Find the segment end: next unexplained jump, if any
        if i + 1 < len(unexplained):
            next_ts = unexplained[i + 1][0]
            end_pos = positions[next_ts]          # segment is [pos, end_pos)
        else:
            end_pos = last_pos + 1
        seg_len = end_pos - pos
        post_level = float(s.iloc[end_pos]) if end_pos <= last_pos else None

        if seg_len >= config.SHARE_JUMP_PERSIST_DAYS and end_pos > last_pos:
            # Jump to a level that persists to the end of data: real change.
            warnings.append(
                f"share jump x{rv:.3f} at {ts.date() if hasattr(ts, 'date') else ts} "
                f"persisted {seg_len}d -> kept (M&A/issuance)"
            )
            i += 1
            continue
        reverts = (
            post_level is not None
            and abs(post_level / pre_level - 1.0) <= 0.02
        )
        if seg_len < config.SHARE_JUMP_PERSIST_DAYS and reverts:
            # Short excursion that returns to the prior level: vendor glitch.
            for p in range(pos, end_pos):
                repairs.append({
                    "date": s.index[p], "old": float(s.iloc[p]), "new": pre_level,
                })
                s.iloc[p] = pre_level
            warnings.append(
                f"vendor share glitch repaired: {seg_len}d segment from "
                f"{ts.date() if hasattr(ts, 'date') else ts} "
                f"(x{rv:.3f} then reverted) -> held at {pre_level:,.0f}"
            )
            i += 2 if i + 1 < len(unexplained) else 1
            continue
        if (last_pos - pos) < 5:
            # Too recent to assess persistence: refuse rather than guess.
            unexplained_recent = {"date": ts, "ratio": rv}
            i += 1
            continue
        # Unexplained, not reverting, but persisted >= 5 days: treat as real
        # (issuance/buyback) with a warning.
        warnings.append(
            f"share jump x{rv:.3f} at {ts.date() if hasattr(ts, 'date') else ts} "
            f"did not revert -> kept with warning"
        )
        i += 1

    return s, repairs, warnings, unexplained_recent


# -- assembly (network-free; unit-tested against frozen fixtures) -------------------


def assemble(
    close: pd.Series,
    splits: pd.Series,
    shares_raw: Optional[pd.Series],
    window_start: date,
    *,
    fallback_shares: Optional[float] = None,
) -> Optional[pd.DataFrame]:
    """Turn raw pull components into the final frame: dedup + split-correct
    + glitch-repair shares, market_cap, within-pull retx, buffer drop.
    Indexes must be tz-naive normalized Timestamps. See module invariants."""
    close = close[~close.index.duplicated(keep="last")].sort_index().dropna()
    if close.empty:
        return None
    warnings: list[str] = []
    if shares_raw is not None and not shares_raw.empty:
        shares_raw, dedup_warn = dedup_shares(shares_raw)
        warnings.extend(dedup_warn)
        shares_adj_sparse = split_correct_shares(shares_raw, splits)
        shares = shares_adj_sparse.reindex(
            shares_adj_sparse.index.union(close.index)
        ).sort_index().ffill().reindex(close.index)
    else:
        if not fallback_shares:
            return None
        warnings.append(
            "get_shares_full unavailable -> constant current "
            "sharesOutstanding used for the whole window"
        )
        shares = pd.Series(float(fallback_shares), index=close.index)

    shares = shares.astype(float)
    if shares.isna().any():
        if shares.first_valid_index() is None:
            return None
        shares = shares.bfill()
        warnings.append("leading share counts backfilled from first observation")

    shares, repairs, rep_warn, recent_jump = repair_share_glitches(shares, splits)
    warnings.extend(rep_warn)

    df = pd.DataFrame({"close": close, "shares": shares})
    df["market_cap"] = df["close"] * df["shares"] / 1_000.0  # raw USD -> thousands
    # retx: WITHIN-pull close-to-close (see module invariant #1)
    df["retx"] = df["close"].pct_change()
    gaps = df.index.to_series().diff().dt.days
    df["retx_synthetic"] = (gaps > config.SYNTHETIC_GAP_DAYS).fillna(False)
    # Drop buffer rows only now (their closes fed the first retx)
    df = df[df.index >= pd.Timestamp(window_start)]
    df.index = [ts.date() for ts in df.index]
    df.index.name = "date"
    df.attrs["warnings"] = warnings
    df.attrs["shares_repairs"] = repairs
    df.attrs["unexplained_recent_jump"] = recent_jump
    return df


def ground_shares_to_crsp(
    conn: duckdb.DuckDBPyConnection,
    permco: int,
    df: pd.DataFrame,
) -> pd.DataFrame:
    """Anchor the Yahoo share count to CRSP shrout.

    Yahoo's price history under a renamed ticker is the right security, but
    its share history can belong to a previous holder of the symbol
    (observed: pre-rename 'BNY' shares were a different entity's, 24.1M vs
    the bank's ~713M). CRSP shrout (thousands) is authoritative wherever
    CRSP covers a date, so:

      - dates covered by CRSP: shares := shrout * 1000 (those rows are
        masked by the equity_daily view anyway; this keeps the level check
        meaningful instead of tripping on Yahoo's merged history);
      - dates after the CRSP edge: Yahoo shares are trusted only from the
        first date they come within SHARES_ANCHOR_TOL of the CRSP-edge
        anchor; the leading inconsistent span is held at the anchor value;
      - if Yahoo NEVER becomes consistent with the anchor, raise — that is
        a genuine share-basis mismatch (wrong company / share class), not
        a rename artifact.

    market_cap is recomputed; retx is price-based and untouched."""
    crsp = conn.execute(
        "SELECT date, shrout FROM crsp_daily "
        "WHERE permco = ? AND shrout IS NOT NULL ORDER BY date",
        [permco],
    ).fetchdf()
    if not len(crsp):
        return df                       # no CRSP history (never-listed era)
    crsp["date"] = pd.to_datetime(crsp["date"]).dt.date
    shrout_map = dict(zip(crsp["date"], crsp["shrout"] * 1000.0))
    edge = crsp["date"].max()
    anchor = float(crsp.loc[crsp["date"] == edge, "shrout"].iloc[0]) * 1000.0

    out = df.copy()
    grounded = 0
    for d in out.index:
        if d in shrout_map:
            out.loc[d, "shares"] = shrout_map[d]
            grounded += 1
    post = [d for d in out.index if d > edge]
    if post:
        ok_from = None
        for d in post:
            if abs(float(out.loc[d, "shares"]) / anchor - 1.0) <= config.SHARES_ANCHOR_TOL:
                ok_from = d
                break
        if ok_from is None:
            raise RuntimeError(
                f"permco {permco}: Yahoo share count never comes within "
                f"{config.SHARES_ANCHOR_TOL:.0%} of the CRSP anchor "
                f"({anchor:,.0f} at {edge}) - genuine share-basis mismatch, "
                f"refusing to import."
            )
        held = [d for d in post if d < ok_from]
        for d in held:
            out.loc[d, "shares"] = anchor
        if held:
            out.attrs.setdefault("warnings", []).append(
                f"shares held at CRSP anchor {anchor:,.0f} for {len(held)} day(s) "
                f"({held[0]}..{held[-1]}) until Yahoo became consistent at {ok_from}"
            )
    out["market_cap"] = out["close"] * out["shares"] / 1_000.0
    if grounded:
        out.attrs.setdefault("warnings", []).append(
            f"shares grounded to CRSP shrout on {grounded} CRSP-covered day(s)"
        )
    return out


# -- the pull ---------------------------------------------------------------------


def pull_one(
    yf_module,
    ticker: str,
    window_start: date,
    window_end: date,
    *,
    retries: int = 2,
    backoff: float = 2.0,
) -> Optional[pd.DataFrame]:
    """One ticker's full window: close, shares (split-corrected, repaired),
    market_cap, retx (within-pull), retx_synthetic. Buffer rows dropped.
    Returns None when Yahoo has no price data. Attaches metadata via attrs:
    df.attrs['warnings'], df.attrs['shares_repairs'],
    df.attrs['unexplained_recent_jump']."""
    hist_start = window_start - timedelta(days=HISTORY_LEAD_DAYS)
    last_exc: Optional[Exception] = None
    for attempt in range(retries + 1):
        try:
            t = yf_module.Ticker(ticker)
            hist = t.history(
                start=hist_start.isoformat(),
                end=(window_end + timedelta(days=1)).isoformat(),
                auto_adjust=False,
                actions=True,
            )
            if hist is None or hist.empty or "Close" not in hist:
                return None
            close = hist["Close"].copy()
            splits = (
                hist["Stock Splits"].copy()
                if "Stock Splits" in hist else pd.Series(dtype=float)
            )
            # Normalize indexes to tz-naive midnight timestamps
            for s in (close, splits):
                idx = pd.to_datetime(s.index)
                if getattr(idx, "tz", None) is not None:
                    idx = idx.tz_localize(None)
                s.index = idx.normalize()
            try:
                shares_raw = t.get_shares_full(
                    start=(hist_start - timedelta(days=SHARES_LOOKBACK_DAYS)).isoformat(),
                    end=(window_end + timedelta(days=1)).isoformat(),
                )
            except Exception:
                shares_raw = None
            if shares_raw is not None and not shares_raw.empty:
                idx = pd.to_datetime(shares_raw.index)
                if getattr(idx, "tz", None) is not None:
                    idx = idx.tz_localize(None)
                shares_raw.index = idx.normalize()
                fallback = None
            else:
                shares_raw = None
                info = getattr(t, "info", {}) or {}
                fallback = info.get("sharesOutstanding")

            return assemble(close, splits, shares_raw, window_start,
                            fallback_shares=fallback)
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if attempt < retries:
                time.sleep(backoff)
    raise RuntimeError(f"yfinance pull failed for {ticker}: {last_exc}")


def pull_all(
    conn: duckdb.DuckDBPyConnection,
    permcos: list[int],
    *,
    window_start: Optional[date] = None,
    window_end: Optional[date] = None,
    ticker_map: Optional[dict[str, str]] = None,
    sleep_between: float = 0.4,
) -> dict[int, PullResult]:
    """Pull every permco's window. Raises on ticker-resolution failure or on
    a pull that errors after retries — a silently missing bank is exactly
    the failure mode this repo exists to prevent."""
    import yfinance
    ws, we = window_bounds()
    if window_start:
        ws = window_start
    if window_end:
        we = window_end
    out: dict[int, PullResult] = {}
    for permco in permcos:
        ticker = resolve_ticker(conn, permco, ticker_map)
        df = pull_one(yfinance, ticker, ws, we)
        if df is None or df.empty:
            raise RuntimeError(
                f"permco {permco} ({ticker}): Yahoo returned no price data for "
                f"{ws}..{we}. Possible delisting or ticker rename - investigate "
                f"before the bank silently drops out."
            )
        df = ground_shares_to_crsp(conn, permco, df)
        out[permco] = PullResult(
            permco=permco,
            ticker=ticker,
            df=df,
            warnings=list(df.attrs.get("warnings", [])),
            shares_repairs=list(df.attrs.get("shares_repairs", [])),
            unexplained_recent_jump=df.attrs.get("unexplained_recent_jump"),
        )
        time.sleep(sleep_between)
    return out


# -- storage ------------------------------------------------------------------------


def diff_against_stored(
    conn: duckdb.DuckDBPyConnection,
    pulls: dict[int, PullResult],
) -> pd.DataFrame:
    """Compare the new pull against stored yf_daily rows on shared
    (permco, date). Returns a DataFrame(permco, date, field, old, new) of
    material changes (market_cap rel > DIFF_MCAP_REL, retx abs >
    DIFF_RETX_ABS)."""
    frames = []
    for permco, pr in pulls.items():
        new = pr.df.reset_index()[["date", "market_cap", "retx"]].copy()
        new["permco"] = permco
        frames.append(new)
    if not frames:
        return pd.DataFrame(columns=["permco", "date", "field", "old", "new"])
    newdf = pd.concat(frames, ignore_index=True)
    conn.register("_yf_new", newdf)
    try:
        diff = conn.execute(
            f"""
            SELECT n.permco, n.date,
                   o.market_cap AS old_mcap, n.market_cap AS new_mcap,
                   o.retx AS old_retx, n.retx AS new_retx
            FROM _yf_new n
            JOIN yf_daily o ON o.permco = n.permco AND o.date = n.date
            WHERE (o.market_cap IS NOT NULL AND n.market_cap IS NOT NULL
                   AND ABS(n.market_cap / NULLIF(o.market_cap, 0) - 1)
                       > {config.DIFF_MCAP_REL})
               OR (o.retx IS NOT NULL AND n.retx IS NOT NULL
                   AND ABS(n.retx - o.retx) > {config.DIFF_RETX_ABS})
            """
        ).fetchdf()
    finally:
        conn.unregister("_yf_new")
    rows = []
    for r in diff.itertuples():
        if (pd.notna(r.old_mcap) and pd.notna(r.new_mcap)
                and abs(r.new_mcap / r.old_mcap - 1) > config.DIFF_MCAP_REL):
            rows.append({"permco": r.permco, "date": r.date, "field": "market_cap",
                         "old": r.old_mcap, "new": r.new_mcap})
        if (pd.notna(r.old_retx) and pd.notna(r.new_retx)
                and abs(r.new_retx - r.old_retx) > config.DIFF_RETX_ABS):
            rows.append({"permco": r.permco, "date": r.date, "field": "retx",
                         "old": r.old_retx, "new": r.new_retx})
    return pd.DataFrame(rows, columns=["permco", "date", "field", "old", "new"])


def replace_window(
    conn: duckdb.DuckDBPyConnection,
    pulls: dict[int, PullResult],
    window_start: date,
) -> int:
    """Delete each pulled permco's rows from window_start on, insert the new
    pull. Wholesale replacement is the point: vendor revisions self-heal."""
    frames = []
    for permco, pr in pulls.items():
        d = pr.df.reset_index()
        d["permco"] = permco
        d["ticker"] = pr.ticker
        frames.append(d[["permco", "date", "close", "shares", "market_cap",
                         "retx", "retx_synthetic", "ticker"]])
    if not frames:
        return 0
    batch = pd.concat(frames, ignore_index=True)
    conn.register("_yf_batch", batch)
    try:
        conn.execute(
            """
            DELETE FROM yf_daily
            WHERE date >= ?
              AND permco IN (SELECT DISTINCT permco FROM _yf_batch)
            """,
            [window_start],
        )
        conn.execute(
            """
            INSERT INTO yf_daily
                  (permco, date, close, shares, market_cap, retx,
                   retx_synthetic, ticker)
            SELECT permco, date, close, shares, market_cap, retx,
                   retx_synthetic, ticker
            FROM _yf_batch
            ON CONFLICT (permco, date) DO NOTHING
            """
        )
    finally:
        conn.unregister("_yf_batch")
    return int(len(batch))
