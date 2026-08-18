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

## Scratch work goes in `.scratchpad/`

**All exploratory and temporary work happens in `.scratchpad/` unless I say
otherwise.** Diagnostic scripts, one-off queries, plots, notebooks, sample
extracts, draft implementations -- all of it lands there, not at the repo
root and not inside `lfinp/`. The folder is gitignored, so nothing in it
reaches a commit.

The main pipeline (`lfinp/`, `tests/`, `banks.txt`, `mergers.txt`, the docs)
stays untouched until I have looked at the scratch work and am happy with it.
**I will say explicitly when to integrate.** Do not fold scratch work into the
pipeline on your own initiative, however finished it looks.

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

## Dashboard (`shiny/`)

R Shiny app for shinyapps.io, fed by `lfinp export-dashboard` ->
`lfinp/dashboard_export.py` -> four parquets in `shiny/data/`. Rules:

- The app **never opens the DuckDB store**. Parquet only: DuckDB is
  single-writer and a reader would block the weekly run.
- **`permco` is the series key, not `rssd`.** Three banks change RSSD
  mid-history; an rssd key splits them silently.
- **No vendor levels in the feed** (`total_liab`, `market_cap_raw`, `sE`, `r`,
  fallback flags). Derived PDs only.
- `shiny/data/*.parquet` **is committed** — rsconnect uploads from disk. The
  root `.gitignore` therefore anchors `/data/`; a bare `data/` would swallow it.
- The export is manual and read-only, never wired into `update`. `status`
  prints a `dashboard` line and marks it `STALE` when the feed is behind.

Deploy target: **shinyapps.io account `dimuthu-r`**, app name `lfi-np-pd` ->
`https://dimuthu-r.shinyapps.io/lfi-np-pd/`. Publish with
`source("shiny/deploy.R")` from the repo root.

The account token and secret are **not in this repo**. `rsconnect` stores them
under `%APPDATA%/R/config/R/rsconnect/` once `rsconnect::setAccountInfo()` has
been run; `rsconnect/` is gitignored here for the same reason. Never paste the
token or secret into a tracked file - CLAUDE.md, README.md and deploy.R are all
committed and pushed to GitHub. If the credential is ever needed again, take it
from shinyapps.io (avatar -> Tokens -> Show) or add it to the existing secrets
store at `C:\key-variables\key-variables.yaml` alongside the WRDS and FRED keys.

## Scope changes

Edit `banks.txt` (one RSSD/line, `dead` flag = compute-only, no Yahoo),
then re-run `lfinp init` — CRSP is watermarked, Yahoo pulls new live banks,
compute fills missing rows. RSSD→permco resolution goes through the link
table and refuses missing/ambiguous mappings.

## Mergers

`mergers.txt` drives the pro-forma liability bridge: between a closing and
the acquirer's next Y-9C, market cap is the combined company while
`total_liab` is the acquirer alone, so the PD reads low. Two clocks, and
both are tested — liabilities enter on `close=`, equity enters on the day
the share count actually re-bases (detected from the data, **not** assumed
from `close=`; CRSP lags the legal close, sometimes past the next filing).
An unresolvable line aborts the build. Every line is machine-checked
against the acquirer's next filing by `tests/test_merger_bridge.py` — run
it after editing. Cash deals and capital raises do not belong in the file.

## External sources (read-only)

Identical to bank-pd: `C:\empirical-data-construction\...` (Y-9C view
`bs_panel_y9c`, Call Reports view `bs_panel`, link view `crsp_frb_link`),
FRED DGS10 API, WRDS, secrets `C:\key-variables\key-variables.yaml`.
Local store: `data/lfi_pd.duckdb`.
