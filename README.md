# lfi-np-pd

Minimal, accuracy-first weekly **Nagel–Purnanandam (2019) modified-Merton
PD** + classic Merton PD for a small, explicitly listed set of large US
financial institutions (`banks.txt`: 24 LFI bank holding companies + SVB).

Replaces the LFI use-case of the `bank-pd` repo (1,450-bank cross-section),
which remains untouched. Compute kernels (`compute_merton_dtd.py`,
`merton_pd_from_paper.py`) and `inputs/ValueSurface.mat` are reused verbatim
from the NP paper code.

Author: Dimuthu Ratnadiwakara.

## Why this repo exists

`bank-pd` derived Yahoo-overlay daily returns from **market-cap percent
change**. Market cap moves when the share count moves — M&A share issuance,
vendor share-count glitches — and those are not returns. Observed damage:
a fake +65.8% "return" on Capital One's Discover closing date, +66.9%/−15.1%
on a Fifth Third share-count overshoot, −33.9%/+49.0% on a pure Northern
Trust vendor glitch. Each fake return triples the 252-day volatility and
corrupts np_PD for a year. Forward-only incremental pulls then freeze such
errors in forever.

## Design

- **Equity data**: WRDS CRSP daily (immutable; legacy `crsp.dsf` +
  validated `crsp.dsf_v2`, currently through **2025-12-31**), then Yahoo
  Finance for the post-CRSP era.
- **Rolling re-pull**: every `lfinp update` re-pulls the **last 15 months**
  from Yahoo wholesale (delete window + insert). The whole vol window
  shares one split-adjustment basis; vendor revisions self-heal; the diff
  vs the previous pull is logged to `pull_diff` and drives targeted
  recompute.
- **Price-based returns**: `retx = close.pct_change()` **within a single
  pull**. Never differenced across pulls (splits re-base Yahoo's history);
  never derived from market cap.
- **Split-corrected market cap**: `get_shares_full()` is raw counts while
  closes are split-adjusted; shares are multiplied by the cumulative factor
  of later splits before forming `close × shares`.
- **Share-glitch repair**: a short share-count excursion that reverts to
  the prior level is a vendor glitch → repaired and logged. A persistent
  step (M&A) is kept. An unexplained jump too recent to classify **aborts
  the import** rather than guessing.

## Accuracy gates

| gate | what | on fail |
|---|---|---|
| share_jumps | day-over-day share ratio unexplained by a split | abort (recent) / repair+warn (glitch) / keep+warn (persistent) |
| crsp_xval | pull price-returns must reproduce CRSP `retx` on overlap (corr ≥ 0.995) | abort permco — wrong-symbol detector |
| pull_diff | new pull vs stored values | never blocks; logs revisions, drives recompute span (change + 372 d) |
| level_continuity | pull market-cap vs stored level over full overlap | abort import — share-basis detector |
| coverage | every live bank has equity data within 5 business days | abort naming the bank (catches silent ticker renames, e.g. BK→BNY) |
| pd_plausibility | week-over-week np_PD ratio outside [1/3, 3] or sE ratio > 1.5 | warn (`checks --strict` exits 1) |

### Output-side anomaly detection

The input gates above stop bad data entering. These describe the computed
panel and catch anything that slipped through — plus genuine events worth
knowing about. All warn-level (`checks --strict` exits 1).

| gate | what | why it works |
|---|---|---|
| se_steps | one-week sE change > 25%, **with attribution** | vol over a 252-day window moves one observation at a time, so a step means a single huge return **entered** (step up) or **exited** (cliff down). Reports the culprit day, its return, its source/synthetic flags, and its gap to that day's peer median |
| peer_divergence | daily return > 15% from the same-day median across the scoped banks | a crash moves every bank; a data error moves one. COVID-March passes (peer gap ~0.03); a share-issuance artifact fails (one bank +66%, peers flat) |
| pd_plausibility | week-over-week np_PD ratio outside [1/3, 3] or sE ratio > 1.5 | coarse net for anything the above miss |
| pd_decoupling | np_PD/merton_PD ratio moves > 2× while sE is stable | isolates value-surface/kernel behaviour from input problems |
| bs_jumps | quarter-over-quarter `total_liab` change > 30% | `total_liab` drives `E_scaled`; a bad Y-9C row distorts PD like a bad share count |
| fallback_rate | > 20% of the last 52 weeks used value-surface extrapolation | high rate means the PD is interpolation, not measurement |
| panel_revisions | historical `np_PD` values that changed since the previous run | `pull_diff` covers inputs; this covers the numbers people cite |

**The artifact fingerprint:** a bad return enters → sE steps → PD is wrong
for exactly one year → cliffs when the day rolls out. Real risk ramps and
decays. `se_steps` encodes precisely that.

**Acknowledgement is what keeps this usable.** Every real crisis (1987,
2008–09, COVID, SVB) trips these forever otherwise, and an operator who
scrolls past flags will scroll past the real one. Review, then suppress:

```bash
# one flag
lfinp ack --check se_steps --rssd 1199611 --date 2026-07-24 --verdict real --note "why"
# every flag of that check in a window (re-runs the check; only acks flags that exist)
lfinp ack --check se_steps --from 2020-02-15 --to 2020-05-01 --verdict real --note "COVID"
```

Anything still listed after suppression is, by construction, unexplained.

All results persist to `check_log`; acknowledgements to `flag_ack`.

## Setup

`inputs/ValueSurface.mat` (30MB, the NP paper's precomputed value surface)
and `data/lfi_pd.duckdb` are **not in git** — one is a large binary input,
the other is fully rebuildable. After cloning:

```bash
cp ../bank-pd/inputs/ValueSurface.mat inputs/          # or from the NP paper code
"C:/envs/bank-pd-venv/Scripts/python.exe" -m pip install -r requirements.txt
"C:/envs/bank-pd-venv/Scripts/python.exe" -m pip install --no-deps wrds
PYTHONPATH=. "C:/envs/bank-pd-venv/Scripts/python.exe" -m lfinp.cli init
```

`init` rebuilds the whole store from WRDS/FRED/Yahoo + the sibling
`empirical-data-construction` DuckDBs (~45 min, Delaunay-bound).

## Commands

```bash
PYTHONPATH=. C:/envs/bank-pd-venv/Scripts/python.exe -m lfinp.cli init     # once: schema + full backfill
PYTHONPATH=. C:/envs/bank-pd-venv/Scripts/python.exe -m lfinp.cli update   # weekly: the only command you need
PYTHONPATH=. C:/envs/bank-pd-venv/Scripts/python.exe -m lfinp.cli status   # read-only freshness + tails + last checks
PYTHONPATH=. C:/envs/bank-pd-venv/Scripts/python.exe -m lfinp.cli checks   # read-only diagnostics (--strict, --all)
PYTHONPATH=. C:/envs/bank-pd-venv/Scripts/python.exe -m lfinp.cli ack ...  # suppress a reviewed flag
PYTHONPATH=. C:/envs/bank-pd-venv/Scripts/python.exe -m pytest tests -q    # regression tests (frozen fixtures)
```

`update` exits non-zero on any gate failure and inserts nothing for the
failing scope. Compute is Delaunay-bound: ~15–20 min wall regardless of row
count.

## Widening the scope

Add RSSDs to `banks.txt` (flag failed banks `dead` so Yahoo is never
queried for them), then run `lfinp init` again — CRSP backfills only the
new permcos (watermarked), Yahoo pulls the new live banks, compute fills
the missing panel rows.

## Caveats

- Values inside the rolling 15-month window change retroactively between
  runs **by design** (vendor revisions are absorbed). Snapshot `pd_panel`
  when citing numbers.
- Yahoo data older than the rolling window (but after the CRSP edge) is
  refreshed only when `init` re-runs; a Yahoo revision there goes unseen
  until then.
- The `E` level depends on Yahoo's share count. The glitch-repair pass
  catches excursions; a *permanently* wrong basis (e.g. Yahoo halving an
  MHC's shares) is caught by `level_continuity` at import time instead.

## External sources (read-only)

Same as bank-pd: Y-9C DuckDB, FFIEC Call Reports DuckDB, permco↔RSSD link
DuckDB (sibling `empirical-data-construction` repo), FRED DGS10 API, WRDS,
secrets at `C:\key-variables\key-variables.yaml`.
