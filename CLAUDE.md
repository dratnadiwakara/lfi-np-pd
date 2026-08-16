# lfi-np-pd — agent context

Minimal, accuracy-first weekly NP (Nagel–Purnanandam 2019) + Merton PD for
the banks in `banks.txt` (24 LFI BHCs + SVB). Full rationale and gate table
in README.md; chronological decisions in NOTES.md. This repo replaces the
LFI use-case of the sibling `bank-pd` repo, which stays untouched.

## Python venv

Same venv as bank-pd. **Do not create `.venv` here.**

```
C:\envs\bank-pd-venv\Scripts\python.exe
```

Run everything from the repo root with `PYTHONPATH=.`:

```bash
PYTHONPATH=. "C:/envs/bank-pd-venv/Scripts/python.exe" -m lfinp.cli update   # weekly
PYTHONPATH=. "C:/envs/bank-pd-venv/Scripts/python.exe" -m lfinp.cli status
PYTHONPATH=. "C:/envs/bank-pd-venv/Scripts/python.exe" -m lfinp.cli checks --strict
PYTHONPATH=. "C:/envs/bank-pd-venv/Scripts/python.exe" -m pytest tests -q
```

## Invariants — do not weaken (tests enforce them)

1. **`retx` is a within-pull price return.** Never derived from market cap
   (share-count noise fakes returns), never differenced across pulls
   (splits re-base Yahoo's stored closes). `tests/test_retx.py` carries
   frozen real-world fixtures (NTRS vendor glitch, FITB overshoot, COF
   Discover M&A) that fail if anyone reverts to the market-cap method.
2. **Rolling wholesale re-pull.** `update` replaces the last 15 months of
   `yf_daily` per run; the diff vs stored values goes to `pull_diff` and
   drives recompute of every Friday whose 252-day vol window is touched.
3. **Shares are grounded to CRSP `shrout`.** Yahoo merges share histories
   across ticker renames (pre-rename "BNY" shares belonged to a different
   entity). CRSP-covered dates use shrout; post-edge Yahoo shares are
   trusted only once within `SHARES_ANCHOR_TOL` of the CRSP anchor; never
   consistent -> abort.
4. **CRSP rows are immutable** once fetched (legacy `crsp.dsf` +
   `crsp.dsf_v2`, v2 accepted only after validating retx/mcap equality on
   the 2024 overlap; v2 rounds retx at 1e-6 — tolerance is 5e-6).
5. **Gates abort; nothing silently degrades.** A bank that stops updating
   must fail `coverage`, not vanish (that is how bank-pd lost BNY Mellon
   for 10 weeks after the BK→BNY rename).
6. ASCII-only console output (Windows cp1252 kills fancy glyphs mid-run).

## Gotchas

- `wrds.Connection()` **must** get credentials from
  `config.load_secrets()`; the bare constructor prompts on stdin and hangs
  forever in non-interactive shells.
- Never `SELECT MAX(dlycaldt) FROM crsp.dsf_v2` without a permno filter —
  full-table scan, tens of minutes. Use `sources.v2_max_date`.
- CRSP `shrout` is in **thousands**; Yahoo share counts are raw. mcap is
  stored in thousands USD everywhere (CRSP convention).
- yfinance quirks: `history()` closes are split-adjusted even with
  `auto_adjust=False`; `get_shares_full()` is raw counts with duplicate
  timestamps (conflicting values around splits) and merged histories across
  renames.
- Compute is Delaunay-bound: ~15–20 min per run regardless of row count.
- Real market moves can exceed 10% daily (Apr 2025 tariff days hit 14.8% on
  COF) — don't tighten plausibility thresholds below that.

## Scope changes

Edit `banks.txt` (one RSSD/line, `dead` flag = compute-only, no Yahoo),
then re-run `lfinp init` — CRSP is watermarked, Yahoo pulls new live banks,
compute fills missing rows. RSSD→permco resolution goes through the link
table and refuses missing/ambiguous mappings.

## External sources (read-only)

Identical to bank-pd: `C:\empirical-data-construction\...` (Y-9C view
`bs_panel_y9c`, Call Reports view `bs_panel`, link view `crsp_frb_link`),
FRED DGS10 API, WRDS, secrets `C:\key-variables\key-variables.yaml`.
Local store: `data/lfi_pd.duckdb`.
