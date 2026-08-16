# lfi-np-pd development notes

Chronological log. Newest on top.

---

## 2026-08-15 — repo created

Motivation: bank-pd's Yahoo overlay derived `retx` from market-cap
pct-change; share-count events printed fake returns into the 252-day vol
window (COF +65.8% Discover close, FITB +66.9%/-15.1% share overshoot,
NTRS -33.9%/+49.0% pure vendor glitch), tripling sE and corrupting np_PD
for a year per event. Forward-only pulls froze vendor errors in forever
(BK->BNY rename silently stalled BNY Mellon for 10 weeks). Full analysis
in bank-pd/NOTES.md 2026-08-15 entry.

Decisions:

- **New minimal repo** over retrofitting bank-pd: 25 banks (banks.txt),
  9 modules, fresh schema, no migrations. bank-pd keeps running for the
  1,450-bank cross-section.
- **Price-based within-pull retx + rolling 15-month wholesale re-pull** —
  see CLAUDE.md invariants.
- **CRSP through 2025-12-31 via crsp.dsf_v2** (CIZ format). Legacy
  crsp.dsf is frozen at 2024-12-31 (annual updates ended with the format
  transition). Column mapping verified on the 2024 overlap: dlyretx ==
  retx exactly except v2 rounds at 1e-6 (validation tolerance 5e-6);
  dlycap == shrout * |dlyprc| to the digit; same thousands units.
- Kernels (`compute_merton_dtd.py`, `merton_pd_from_paper.py`) and
  `ValueSurface.mat` copied verbatim from bank-pd (which had them verbatim
  from the NP paper authors).

### Lessons already banked during the build

- **Bare `wrds.Connection()` hangs forever** in non-interactive shells
  (stdin prompt). Three "probes" wasted an hour on this before the
  credentialed constructor answered in 8 seconds. Always
  `connect_wrds(user, pass)` from secrets.
- **`MAX(dlycaldt)` over dsf_v2 unfiltered is a full scan** (killed at 45
  min). Probe via one permno + ORDER BY DESC LIMIT 1: seconds.
- **Yahoo merges share histories across ticker renames.** BNY prices match
  CRSP BK to the penny, but `get_shares_full("BNY")` returns a different
  entity's 24.1M shares before the 2026 rename, switching to the bank's
  ~713M after. The level-continuity gate caught it on the first init run
  (median mcap diff 96.7%, whole-import abort — exactly as designed).
  Fix: `ground_shares_to_crsp` — CRSP shrout on CRSP-covered dates, anchor
  hold after the edge until Yahoo's count converges, abort if it never
  does (that case = genuine basis mismatch, e.g. bank-pd's CLBK MHC
  problem).
- **Real moves can be big.** April 2025 tariff days hit |14.8%| on COF —
  a legitimate daily return. Test/check thresholds must not treat >10% as
  automatically fake; the COF fixture test originally did and failed on
  real data.
- **v2 retx is rounded at 1e-6.** An equality tolerance of 1e-8 "failed"
  80 rows of pure rounding on the first init; 5e-6 passes rounding and
  still catches any real difference.

### First full run — gates + acceptance (2026-08-15 evening)

- `init` end-to-end clean, exit 0. CRSP edge **2025-12-31** via dsf_v2
  (+6,000 rows, retx identical to legacy on all 3,072 overlap rows; 3
  boundary-day mcap diffs <= 2.09% accepted as shrout revisions).
- Import gates on the live run: crsp_xval validated all 24 ticker
  mappings; NTRS vendor glitch auto-repaired (7d segment held at 185.0M);
  FITB x1.644 and HBAN x1.288 share steps kept as persistent M&A; BNY
  shares held at the CRSP anchor (697.3M) for 97 days until Yahoo's
  history switched to the bank's basis on 2026-05-22 (x28.46 jump).
- **Parity gate vs bank-pd** (pure-CRSP era, week_date <= 2024-12-27):
  39,392 shared rows, np_PD/merton_PD/sE max abs diff ~1e-14, zero rows
  present on one side only. Port is exact.
- **Acceptance** (2026-08-14, vs bank-pd's contaminated values):
  NTRS np_PD 0.125 (was 0.564 - vendor-glitch artifact), FITB 0.142 (was
  0.589), HBAN 0.202 (was 0.268), BAC 0.185 (was 0.190), COF 0.128.
  NTRS sE flat ~0.247 straight through the glitch window. SVB series
  1988-04-29..2023-03-17. Panel 1986-10-03..2026-08-14, 41,432 rows.
- pd_plausibility flags 29 week-over-week jumps, all pre-2025 CRSP era or
  real events (1987 crash, 1998 LTCM, 2008-09, 2020-03 COVID weeks, SVB
  failure week np_PD 0.85, FCNCA jump on buying SVB). Several are the
  known NP value-surface fallback flapping at sE ~0.6-0.7 - present
  identically in bank-pd (parity confirms), a kernel property, not a
  data defect.

### 2026-08-15 (late) — output-side anomaly detection added

The six import gates stop bad data entering, but nothing described the
computed panel. Added seven output-side checks (`checks.py`), a
suppression table, and an `ack` CLI.

Design premise, learned from the COF/NTRS/FITB episodes: **an artifact
enters as a step and exits as a cliff exactly 252 trading days later; real
risk ramps and decays.** `se_steps` encodes that directly — it flags a
one-week sE change >25% and then attributes it, walking the daily series
between the two effective dates to name the day that entered (or the day
that rolled off the far end), its return, its provenance, and its gap to
that day's peer median. Output on the real panel reads e.g.
`1987-10-23 sE 0.193->0.402, culprit 1987-10-21 retx +0.256, entered,
peer_median +0.073` and then the matching cliff at `1988-10-21 ... exited`.

`peer_divergence` is the discriminator that makes the rest usable: a
market crash moves all 24 banks (COVID 2020-03-13 shows peer gaps of
~0.03 and is correctly not idiosyncratic), while a share-count artifact
moves one (COF +65.8% against a flat peer median). Validated on real
history: SVB -60% (2023-03), MS +87% (2008-10-13 Mitsubishi/TARP), FITB
+60% (2009-02) all surface as genuinely idiosyncratic-but-real.

`flag_ack` + `lfinp ack` exist because without them every historical
crisis re-fires on every run, and an operator trained to scroll past
flags will scroll past the real one. Bulk form re-runs the named check and
only acks flags that actually exist, so a typo suppresses nothing.
Verified: acking the 1987-10..1988-11 window took se_steps 62 -> 55.

Also added: `pd_decoupling` (np/merton ratio moves while sE stable ->
kernel, not data), `bs_jumps` (total_liab QoQ >30%; first live run caught
Fifth Third 2026-Q1 +36.5%, the Comerica acquisition, consistent with its
x1.644 share step), `fallback_rate` (value-surface extrapolation share),
and `panel_revisions` (pd_panel snapshot diff per run — pull_diff covers
inputs, this covers the cited outputs).

Gotcha: DuckDB parses a bare `CURRENT_TIMESTAMP` in `DO UPDATE SET` as a
column reference — use `now()` in VALUES and `EXCLUDED.col` in the update.
Read-only `checks` must tolerate tables that predate the store (helper
`_table_exists`), since it cannot create them.

Rolling-era status after the change: everything OK except the Fifth Third
balance-sheet jump. Full-history sweep (`checks --all`) surfaces the
expected crisis-era flags awaiting triage.

### Fixture provenance

tests/fixtures/*.csv captured live from yfinance on 2026-08-15:
NTRS 2026-05..08 (contains the bogus 122,831,734 share segment),
FITB 2025-11..2026-03 (contains the 1,087,022,011 overshoot),
COF 2025-02..08 (contains the 383M->640M Discover issuance).
If Yahoo later fixes its history, the "old method reproduces the defect"
tests keep documenting what happened — refresh the note, don't delete.

## 2026-08-16 — dated pd_panel snapshots

Traceability question: in two years, can a PD be tied back to the data that
produced it? Inputs, yes — `yf_daily`'s delete is scoped `date >= window_start`,
so days freeze permanently once they leave the rolling window, and
`pd_panel` -> `pd_input` -> 252 dailies reconstructs the number. Outputs, no:
`pd_panel` holds only the current value and `pd_panel_prev` is wiped and
rewritten every run, so a week revised by a vendor correction loses whatever it
previously published. `pull_diff` explains why a number moved but not what it
was.

`db.export_panel_snapshot` now writes the whole panel to
`data/snapshots/pd_panel_<rundate>.parquet` at the end of `init` and `update`,
after the checks, with a `snapshot_date` column so the files union via
`read_parquet('...*.parquet')`. Full panel per run, not a delta — deltas would
need the whole chain intact to reconstruct any single week, which is exactly
the fragility being fixed. Same-day re-run overwrites. Real store: 41,432 rows
x 25 banks, 1986-10-03..2026-08-14, 3.0 MB ZSTD per run (~160 MB/yr).

`data/` is gitignored and these files are not rebuildable by `init` — the only
such artifact in the repo. The path lives under OneDrive, so they are synced;
`status` now prints the snapshot count and latest date so a stalled archive is
visible.

Extended the same run with `pd_input_<rundate>.parquet`: all pd_input columns
plus `retx_csv`, the VOL_WINDOW daily returns ending at date_eff. Makes a row
self-verifying — `stddev_samp(fields) * sqrt(252)` must equal the stored sE.
Checked against the live store: 45,825 rows, zero mismatches, max abs diff
1.3e-15, n_obs_252 equal to the emitted non-null count everywhere.

Two encoding choices are load-bearing. Returns go out as `CAST(retx AS
VARCHAR)` (DuckDB's shortest round-tripping double text) rather than
`printf('%.8f')`, because a re-derived sE that disagrees in the 9th decimal
cannot be dismissed quickly. And a NULL retx is emitted as an empty field, not
skipped: build_pd_input's window is 252 *rows* of equity_daily, so dropping a
gap would silently pull an extra day in and the string would describe a
different window than the one sE saw.

Cost: 3.5 MB and ~23 s per run (window join over 226k daily rows), against a
15-20 min Delaunay-bound compute. Storing the series per week rather than
dumping equity_daily once (2.8 MB) duplicates ~250x, but ZSTD absorbs it and
each row stays independently checkable, which is the point.

## 2026-08-16 - business-model groups in banks.txt

Added a mandatory `group=` flag per line: `moneycenter` (4), `dealer` (2),
`feebased` (4), `lender` (15). Cut by funding source, which is what a PD is
about. Started at three groups with GS/MS inside a combined `universal`; that
was wrong - GS/MS take no meaningful deposits - and the 4th group was added
the same day.

Checked the merge against the panel before splitting (weekly d(sE), 2021-08+):
GS-MS correlate 0.88, their top pair each; JPM/BAC/WFC/C follow at 0.68-0.77;
no custodian appears in either top 8. So the original merge was the least-wrong
of the two available, but the distinction it hid is the one that matters, and
a 2-bank group is honest about being a pair rather than a peer median.

Placed in banks.txt rather than a new file or a hardcoded dict because scope
and its metadata drifting apart is the failure mode - one file to edit when a
bank is added, and the parser aborts on a missing or unknown group instead of
letting an unclassified bank drop out of every group-wise cut.

`db.sync_bank_groups` mirrors it into `bank_group` on every init/update as a
full DELETE+INSERT, so SQL can join without re-parsing and a bank removed from
banks.txt cannot linger with a stale label. Table is derived, safe to drop.

Judgement calls worth recording: SCHW is `feebased` - it holds securities not
loans, and its 2023 episode was a duration/deposit event like the custodians',
not a credit event. AXP/COF/SYF/ALLY sit with the regionals: different asset
mix, same PD-driven-by-credit-losses logic. FCNCA is a regional that half
became a tech lender after acquiring SVB - worth watching if it separates from
its group.

Not wired into peer_divergence yet. That gate still uses one pooled median
across all 25 banks, which permanently biases card lenders high and custodians
low. Grouping the median is the obvious next tightening; deliberately left
alone here so this change adds no gate behaviour.

## 2026-08-16 - group-level NP PD (each business-model group as one bank)

group_daily / group_input / group_panel. The group is a merged entity, not an
average of member PDs: liabilities summed, equity summed, sE = vol of the
cap-weighted portfolio of members. Averaging PDs answers "how risky is a
typical dealer"; merging answers "how risky is the dealer sector as one balance
sheet". The gap is diversification, and it lands where theory says (group sE /
cap-weighted member sE, 2015+): lender 0.86 (15 members), feebased 0.90,
moneycenter 0.93, dealer 0.96 (2 members).

Three decisions worth keeping:

Lagged weights. w_i(t) = mcap_i(t-1). Same-day caps let a bank that rose 50%
set its own weight - a look-ahead that biases the index up. Pinned by a test
that would pass under either convention if it only checked the sign.

Composition is implicit. A member leaves the index by running out of
equity_daily rows (SVB, March 2023), not by a rule. Cheap and self-maintaining,
but it means the index re-levels on membership as well as risk, so n_members is
stored on every panel row and check_group_composition flags every change. That
check exists because it is the one anomaly the member-level checks cannot see -
everything else the group could show is already caught on the banks it sums.

Partial balance sheets refused, not summed. If members holding >5% of group
market cap lack a Y-9C row, total_liab is NULL and the week does not compute.
A partial sum understates liabilities -> understates PD, i.e. fails in the
reassuring direction. Consequence: moneycenter and feebased run from 1986,
dealer and lender only from 2009-04-03 (GS/MS became BHCs in late 2008; lender
still has scattered failing weeks to 2015). So the four series are not
comparable before 2009. Correct, and visible in group_input rather than silent.

Backfill: 4,725 group weeks, 948s wall (one Delaunay build). Levels check out -
moneycenter peaks 0.85 on 2009-03-06 and 0.61 in Oct 2008, all four peak
together in March 2020, and each group's latest PD sits just below the
cap-weighted mean of its own members. check_group_composition returns 6 real
membership changes (SVB exit 2023-03-24, Synchrony/Ally entering 2014-2016,
Schwab into feebased 2007, and two older ones); left unacked deliberately - the
verdict notes are the owner's to write.

Group rows ride in the same run_compute call as the banks (synthetic negative
permco, split on sign afterwards). The Delaunay build is a fixed per-call cost,
so a separate group pass would have doubled the weekly runtime for 4.7k extra
rows; folded in, it costs seconds.

## 2026-08-16 - issuance_bs_lag: new equity over old liabilities

Found by asking what the COF/Discover close did to the group index. The
returns were clean - retx is a price return, so the share issuance could not
fabricate one, and sE never moved (0.405 -> 0.406). That is invariant 1 doing
its job.

The damage was on the balance-sheet side. Close 2025-05-18: market cap
70.9bn -> 121.1bn on 2025-05-30, total_liab stuck at Q1 430.1bn until Q2's
548.0bn on 2025-07-04. Five weeks of E_scaled 0.28 against a true ~0.22, and
np_PD 0.164 against 0.231 the week before. Same event, diluted, in the lender
group: mcap 641 -> 703bn on flat liabilities, np_PD 0.185 -> 0.170.

Nothing caught it. bs_jumps needs 30% and the Q1->Q2 move was +27.4%.
pd_plausibility needs 3x and the PD moved 0.71x. peer_divergence and se_steps
look at returns, which were correct. So it passed every gate while being wrong
in the reassuring direction - the PD understated, the bank looking better
capitalised than it was.

Detector: (mcap_t / mcap_t-1) / (1 + retx_t). ~1.0 whenever the change in
market cap is the price change; deviates only when the share count moved.
Split-immune by construction - market cap is split-invariant and retx is
split-adjusted, so both terms are 1.0 through a split. That matters because the
CRSP era has no equivalent of the Yahoo-side share_jumps gate, so a share-count
based detector would have to be written twice and would fire on every split.

Flag is defined against bs_quarter_end, so it clears itself when the filing
lands - no manual expiry, no stale suppression. Group rows carry the sector's
negative id and a cap_share, gated at 2%: a 1% member issuing shares does not
move a sector PD, and flagging it would train the operator to scroll past.

No correction attempted. The true interim liability figure does not exist in
Y-9C, Call Reports or anywhere else the pipeline reads. The check names the
weeks and the size of the equity move; the judgement stays with the reader.

Live store, 2024+: 4 events - COF/Discover (1.67x), FITB 2026-02-04 (1.64x),
HBAN 2026-02-03 (1.29x), FCNCA 2026-01-02 (1.12x), KEY 2025-01-31 (1.12x).
All plausible corporate actions; none acked yet.
