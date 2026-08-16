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

## Business-model groups

Each line in `banks.txt` carries a mandatory `group=` flag. A pooled cross-bank
median mixes a card monoline with a custodian, so the panel needs a cut. The cut
is by **funding source**, which is what a PD is ultimately about:

| group | n | banks | funded by |
|---|---|---|---|
| `moneycenter` | 4 | JPM, BAC, C, WFC | retail + corporate deposits; large dealer book on top |
| `dealer` | 2 | GS, MS | wholesale markets; deposits are recent and small |
| `feebased` | 4 | BK, STT, NTRS, SCHW | fees on assets serviced; credit risk incidental, rate/deposit risk is not (2023) |
| `lender` | 15 | USB, PNC, TFC, MTB, FITB, KEY, RF, HBAN, CFG, FCNCA, AXP, COF, SYF, ALLY, SVB | deposits, lent out; credit losses drive the PD |

Judgement calls: Schwab is `feebased` not `lender` — it holds securities, not
loans, and its 2023 stress looked like a custodian's, not a bank's. Consumer
credit monolines (AXP/COF/SYF/ALLY) sit with the regionals: different assets,
same "PD is driven by credit losses" logic. FCNCA is a regional that half-became
a tech lender post-SVB. `dealer` is deliberately two banks — GS/MS are
wholesale-funded in a way nothing else here is, and folding them into
`moneycenter` would hide exactly the distinction the grouping exists to make.
Treat it as a pair, not a median.

What the panel says about the cut (weekly change in sE, 2021-08 on): GS and MS
correlate 0.88 with each other, their highest pair; next come JPM/BAC/WFC/C at
0.68–0.77; no custodian is in either bank's top 8. So `dealer` is a real cluster,
and if it ever has to be merged, `moneycenter` is the right destination.
`lender` is the loose group — Synchrony tracks GS/MS at 0.79 and AXP runs
liabilities/market cap of 1.4 against 7–10 for everything else.

`banks.txt` is the source of truth; every run mirrors it into `bank_group`
(`rssd, grp, dead, name`) as a full replace, so SQL can cut the panel without
re-parsing the file and a removed bank cannot linger:

```sql
SELECT g.grp, week_date, median(np_PD) FROM pd_panel p
JOIN bank_group g USING (rssd) GROUP BY 1, 2;
```

Unknown or missing `group=` aborts the parse. `lfinp status` prints the panel
tail grouped.

### Group-level PD

Each group is also run through the kernels as **one merged bank** (`group_panel`,
keyed `week_date, grp`). Liabilities and equity are summed; the volatility is the
volatility of the *combined* equity, i.e. of the cap-weighted portfolio of the
members. That is not the average of the members' PDs, and the difference is the
point — averaging answers "how risky is a typical dealer", merging answers "how
risky is the dealer sector as one balance sheet". The gap between them is
diversification:

| group | group sE | cap-wtd member sE | ratio |
|---|---|---|---|
| lender | 0.264 | 0.303 | 0.86 |
| feebased | 0.267 | 0.296 | 0.90 |
| moneycenter | 0.257 | 0.274 | 0.93 |
| dealer | 0.276 | 0.287 | 0.96 |

(weekly, 2015 on.) Ordered exactly as it should be: 15 loosely-correlated lenders
diversify most, 2 broker-dealers least.

Three rules make the number honest:

- **Weights are lagged.** `w_i(t) = mcap_i(t-1)`. Same-day caps would let a bank
  that rose 50% grade its own weight.
- **Membership is whoever traded that day.** SVB leaves `lender` in March 2023 by
  running out of rows, not by rule. So the index level moves on composition as
  well as risk: `n_members` is stored on every panel row, and the
  `group_composition` check flags every week the count changes.
- **Partial balance sheets are refused.** If members holding >5% of the group's
  market cap have no Y-9C row that week, `total_liab` is NULL and the week does
  not compute. A partial sum understates liabilities, which understates the PD —
  wrong in the reassuring direction.

That last rule sets where each series can start, and the four are **not
comparable before 2009**:

| group | panel starts | why |
|---|---|---|
| moneycenter | 1986-10-03 | 1,560 weeks |
| feebased | 1986-10-03 | 1,416 weeks |
| dealer | 2009-04-03 | GS/MS only became BHCs, and so only entered Y-9C, in late 2008 |
| lender | 2009-04-03 | 842 weeks; scattered pre-2016 weeks still fail coverage |

Sanity: `moneycenter` peaks at np_PD **0.85** the week of 2009-03-06 and **0.61**
in October 2008; every group peaks together in March 2020. In the latest week
each group's PD sits just below the cap-weighted average of its own members —
which is what diversification looks like on the output side.

Group rows ride in the same kernel call as the banks (synthetic negative
`permco`, split out afterwards) because the Delaunay build is a fixed cost per
call — the sector panel adds seconds, not a second 20-minute run.

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
| issuance_bs_lag | market cap changed without a matching return, and the balance sheet still predates it | Y-9C is quarterly, so between an issuance and the next filing the kernel divides post-issuance equity by pre-issuance liabilities. E is overstated, so the PD is **understated** — it fails in the reassuring direction. Detected as `(mcap_t/mcap_t-1)/(1+retx_t)`, which is ~1.0 unless the share count moved and is immune to splits. Clears itself when `bs_quarter_end` passes the issuance |
| group_composition | a group index gained or lost a member | a cap-weighted index re-levels when membership changes; without this a reader takes that step for risk. The only check the group panel needs — every other anomaly is already caught on the member banks it sums |

**The artifact fingerprint:** a bad return enters → sE steps → PD is wrong
for exactly one year → cliffs when the day rolls out. Real risk ramps and
decays. `se_steps` encodes precisely that.

**The M&A fingerprint is different, and slower.** Capital One closed Discover on
2025-05-18. The returns were clean — `retx` is a price return, so the share
issuance could not fabricate one, and sE never moved. But market cap went
70.9bn → 121.1bn while `total_liab` sat at Q1's 430.1bn until Q2's 548.0bn
landed on 2025-07-04. For five weeks `E_scaled` read 0.28 against a true ~0.22
and np_PD read 0.164 against 0.231 the week before. Every other gate passed: the
quarter-over-quarter balance-sheet jump was +27.4% (`bs_jumps` needs 30%) and
the PD ratio was 0.71 (`pd_plausibility` needs 3×). `issuance_bs_lag` exists for
that window. It cannot correct the number — the true interim liability figure
does not exist in any source here — but it names the weeks where the PD is
optimistic and by how much the equity base moved.

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

## Audit trail

Nothing is deleted. The 15 months is the *re-pull* window, not a retention
window: `yf_daily` only ever replaces rows at or after `window_start`, so a
day freezes permanently once it rolls out and the daily inputs behind an old
PD stay on disk. Tracing a published PD runs `pd_panel` -> `pd_input` (same
permco/week: sE, r, total_liab, E_scaled) -> the 252 dailies in
`equity_daily` before `date_eff`.

What that chain cannot give you is the *previously published* value of a
week that has since been revised — `pd_panel` holds the current number and
`pd_panel_prev` is only one run deep. So every `init`/`update` archives the
full panel after the checks run:

```
data/snapshots/pd_panel_YYYY-MM-DD.parquet     # published PDs      ~3.0 MB/run
data/snapshots/pd_input_YYYY-MM-DD.parquet     # what produced them ~3.5 MB/run
data/snapshots/group_panel_YYYY-MM-DD.parquet  # sector PDs + their inputs
```

The input file carries every `pd_input` column plus `retx_csv` — the 252
daily returns ending at `date_eff`, in date order, comma separated — so a
row needs nothing else to be checked:

```sql
SELECT sE, (SELECT stddev_samp(CAST(v AS DOUBLE)) * sqrt(252)
              FROM unnest(str_split(retx_csv, ',')) t(v) WHERE v <> '')
FROM read_parquet('data/snapshots/pd_input_2026-08-16.parquet');
-- verified equal on all 45,825 rows of the live store (max diff 1.3e-15)
```

Returns are stored as DuckDB's round-tripping double text, not fixed
decimals, and a missing return is an empty field rather than a dropped one
(the window is 252 *rows*) — otherwise the re-derived sE differs in the last
digits and the check stops being conclusive.

Each file is a complete table, not a delta, and carries a `snapshot_date`
column, so the set reads as one:

```sql
SELECT snapshot_date, np_PD
FROM read_parquet('data/snapshots/pd_panel_*.parquet')
WHERE rssd = 1073757 AND week_date = DATE '2026-05-15'
ORDER BY snapshot_date;      -- every value this week has ever reported
```

Same-day re-runs overwrite. These files are the one thing in `data/` that
`init` cannot rebuild — `pull_diff` explains *why* a number changed, the
archive is *what it used to say*. Keep them backed up.

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

**Operators: see [RUNBOOK.md](RUNBOOK.md)** — what to do when a run stops or
flags something, without needing to understand this file.

`update` exits non-zero on any gate failure and inserts nothing for the
failing scope. Compute is Delaunay-bound: ~15–20 min wall regardless of row
count.

## Widening the scope

Add RSSDs to `banks.txt` (each needs a `group=`; flag failed banks `dead` so
Yahoo is never queried for them), then run `lfinp init` again — CRSP backfills only the
new permcos (watermarked), Yahoo pulls the new live banks, compute fills
the missing panel rows.

## Caveats

- Values inside the rolling 15-month window change retroactively between
  runs **by design** (vendor revisions are absorbed). Cite from a dated
  file in `data/snapshots/`, not from live `pd_panel`.
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
