# lfi-np-pd development notes

Chronological log. Newest on top.

---

## 2026-08-17 — pro-forma merger bridge (mergers.txt)

`check_issuance_bs_lag` (added 2026-08-15) detected the post-merger window but
ended with "No correction is attempted." It now corrects it for acquisitions.

The distortion, sized: for COF/Discover the published np_PD was **0.1644** for
five weeks. Recomputing on COF's own inputs with the Q2 liabilities that were
not yet available gives **0.1873**; the week before the close read 0.2311. So of
the 0.067 drop, **0.044 was real** (Discover did add more equity than
liabilities) and **0.023 was the stale denominator** — a third of it. The kernel
reproduced both stored values exactly (0.231139, 0.164441), so the
counterfactual is sound rather than indicative.

Approach: `mergers.txt` (banks.txt pattern — strict parse, mirrored into
`merger_event` by full replace) lists completed deals; `build_pd_input` adds the
target's own last-filed liabilities for the weeks where the target is in the
numerator but not the denominator. `total_liab` carries the bridged figure,
`total_liab_reported` keeps what was filed, `bs_bridged` marks the week.
Accuracy on COF: 559.01bn bridged vs 548.01bn filed = **+2.0%**, against −27.4%
uncorrected. It errs high (summed liabilities exceed fair-valued ones), which is
the right direction to be wrong in.

### Three things the real data corrected during the build

- **The legal close is not the day equity re-bases.** First cut keyed the bridge
  off `close=`. COF closed 2025-05-18; CRSP carried the combined share count
  from 2025-05-30. That put Discover's liabilities into the 2025-05-23 week,
  which still held only Capital One's equity, and drove `E_scaled` 0.165 → 0.127
  — the same error with the sign flipped. Fix: detect the re-base day from the
  data, the same `(mcap_t/mcap_t-1)/(1+retx_t)` ratio `issuance_bs_lag` uses.
- **Equity and liabilities are on different clocks, and testing one is not
  enough.** Huntington closed Veritex 2025-10-20 and CRSP *never* picked up the
  new count — it first appears 2026-01-02 via Yahoo, by which time the
  2025-12-31 Y-9C already consolidates Veritex. Keying only off the effective
  date would have double-counted. Final predicate: equity in
  (`week_date >= effective_date`) **and** liabilities out
  (`bs_quarter_end < close_date`). Consequence worth knowing: most deals bridge
  *no* weeks, because CRSP usually updates after the next filing. 18 deals
  produce 93 bridged weeks across 10 banks.
- **A target can keep its name while losing its charter.** HBAN bought TCF
  Financial in 2021, but TCF's original RSSD 2389941 stops filing at
  2019-06-30: the 2019 TCF/Chemical Financial merger of equals put the TCF name
  on Chemical's charter (1201934). The lookup *succeeds* on 2389941 and returns
  a two-year-stale balance sheet, so a missing-value check cannot catch it.
  Added `MERGER_TARGET_STALE_DAYS` (200): a target's last filing more than two
  quarters before the close aborts the build as a probable wrong RSSD.
  `liab=` rows are exempt, since they carry their provenance in the comment.
- **The blind-scan threshold is the wrong bar for a known event.**
  `ISSUANCE_RATIO_TOL` is 10% because a false positive costs operator attention.
  With a merger row asserting the deal, the job is timing a known event, so
  `MERGER_REBASE_TOL` is 2% — at 10% the bridge silently missed
  BB&T/Susquehanna (6.4%) and BB&T/National Penn (5.8%). Verified: at 2% every
  merger window holds exactly one candidate day bar two, where the earliest is
  correct.

### Backfill to 2012, and why it is safe to have gone wide

19 deals. Every line is machine-checked against what the acquirer actually filed
the next quarter (`tests/test_merger_bridge.py`), which is what made a wide
backfill safer than a narrow one — it also *settled* an identity I could not
confirm from any source: First Citizens Bancorporation of South Carolina is
RSSD 1075911 (+0.2%, vs −0.5% for the bank-level figure).

The first live run then caught its own omission: HBAN/TCF had been dropped while
assembling the file, and `issuance_bs_lag` still flagged those 2021 weeks. That
is the intended loop — the flag is the to-do list, and the residual after
bridging is what has no merger behind it.

Errors run −11.9% to +8.1%, most within 3%. The tolerance is 15% because the
check mixes bridge accuracy with whatever the acquirer's own balance sheet did
that quarter — Schwab and Morgan Stanley both grew heavily in Q4 2020
independent of their deals, which is why they sit furthest out.

Deliberately excluded, and these should stay excluded: cash-only deals
(PNC/BBVA USA, JPM/First Republic, USB/Union Bank) have no equity re-base and so
no mismatch; capital raises (BAC's 2017 Berkshire warrants, RF 2012, MS 2011
MUFG conversion, STT 2021, KEY's Scotiabank investment) have no target to bridge
and stay `issuance_bs_lag` flags.

PD effect where it fires: always upward, removing understatement. FCNCA +0.105
average (CIT was 87.6% of its liabilities), KEY +0.072, M&T +0.057, COF +0.050,
FITB +0.049; Morgan Stanley and Schwab under 0.005.

### A pre-existing bug this surfaced: pd_panel could silently go stale

`assemble_inputs` skipped any `(week_date, permco)` already in `pd_panel`. That
is only safe if "already computed" means "computed from the inputs present
now" — and it did not. `update` recomputes a rolling window driven by the Yahoo
re-pull span; any input revision older than that window was skipped forever.

Editing `mergers.txt` rewrites `total_liab` for weeks going back to 2012, so the
first live backfill left **75 of 96 bridged weeks carrying pre-bridge PDs**.
Nothing caught it: no check compares `pd_panel` against `pd_input`, so the two
simply disagreed. Both `update` runs exited 0. It was found by querying the
published panel rather than the input table the bridge writes to — the lesson
being that verifying a write at its source proves nothing about what is served.

Fixed generally, not for mergers: a stored row is excluded only when its five
kernel inputs (`total_liab`, `market_cap_raw`, `sE`, `r`, `E_scaled`) still match
`pd_input` within `PANEL_REVISION_TOL`. Same rule on
`group_input`/`group_panel`, since groups sum member history. Any input revision
now pulls its week back into the compute set regardless of age. Four tests pin
it, including that ordinary float round-tripping must *not* trigger a recompute
— otherwise every week recomputes forever and the incremental path is dead code.

Worth considering as a follow-up: a `panel_staleness` check that fails when
`pd_panel` disagrees with `pd_input`. The fix closes the hole, but nothing yet
*detects* the condition, and this class of defect is invisible from exit codes.

### Unrelated finding: a share-count discontinuity at the CRSP/Yahoo seam

While hunting merger candidates: **twelve of the 25 banks jump on 2026-01-02**,
all `source='yfinance'`, ratios 1.010 to 1.120. That is the first Yahoo day
after the CRSP edge (2025-12-31), so it is the share-count basis changing, not
twelve simultaneous corporate actions. Two crossed the 5% screen: HBAN 1.079
(which is genuinely Veritex, arriving 74 days late because CRSP never updated
shrout) and **FCNCA 1.120, which has no corporate action behind it at all** —
First Citizens was *buying back* stock through 2025, so the count should have
fallen. CRSP shrout 11,430k at 2025-12-31 vs Yahoo 12,804k.

This feeds `market_cap` and therefore `E_scaled` and the PD for every affected
bank from 2026-01-02 onward. It is a separate defect from anything in this
entry, it is not fixed here, and it wants its own investigation — the
`ground_shares_to_crsp` anchor logic (invariant 3) is the obvious place to look,
since the ten sub-5% cases slip under `ISSUANCE_RATIO_TOL` and never flag.

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

## 2026-09-29 - next-10 regionals added as live nogroup

Scope widened from 37 to 48 lines: the ten largest listed US banks below the
LFI set by 2026-06-30 Y-9C assets (Western Alliance, Zions, East West, First
Horizon, Popular, Wintrust, Old National, UMB, South State, Valley National),
plus Webster as `dead nogroup`. All live, all `nogroup`, so `group_panel` and
the dashboard mean are byte-for-byte what they were; `group=lender` on those
lines is a label only. `nogroup` had been dead-only until now and the tests
pinned that; they now pin the exact live-nogroup set instead.

### What the candidate list turned up

- **Webster ranked 5th but no longer exists.** Banco Santander closed its
  acquisition on 2026-08-20; Yahoo's WBS history is a single row. It stays as
  `dead nogroup` (CRSP history through 2025-12-31) and Valley, 11th at 66bn,
  takes the tenth live slot. Found by the Yahoo pull, not by the asset ranking:
  the 2026-06-30 Y-9C is the last thing a bank files before it disappears.
- **Pinnacle/Synovus (129bn) is not addable yet.** The 2026 merger created a
  new top-tier RSSD 6073350 (Y-9C from 2026-03-31); both legacy RSSDs stop at
  2025-12-31 and `crsp_frb_link` has no row for the new one, so
  `permcos_for_rssds` refuses it. Needs a link override plus a Synovus bridge.
- **Flagstar/NYCB (88bn) would go quietly stale.** The holding company stopped
  filing Y-9C at 2025-09-30 (folded into Flagstar Bank NA, Call Report RSSD
  694904) but the link still maps permco 12608 to 2132932, so the Y-9C ASOF
  join would forward-fill the 2025-09-30 liabilities forever and the Call
  Report fallback would never trigger. `bs_stale` flags it, nothing aborts.
  Deferred until there is a link override.
- **Zions is the case the link already handles.** Holding company 1027004
  stopped filing at 2018-06-30; the link switches to bank RSSD 276579 at
  2018-09-30, after which `bs_source` is `call_report`. banks.txt carries
  276579 (the latest linked RSSD) and the ASOF link gives 1027004 for earlier
  weeks, same mechanism as RF/BK/KEY.
- Comerica (acquired by FITB 2026-02-01), the non-bank Y-9C filers (Ameriprise,
  Raymond James, Stifel, SoFi) and the foreign IHCs are out.

### Two init defects the wider scope exposed

Both would have been invisible with 25 banks and both failed loudly.

- **`validate_v2_against_legacy` did not scale.** It rejected v2 when more
  than five (permco, date) rows disagreed on market cap. Every disagreement
  ever seen sits on the legacy file's last day (2024-12-31: a shrout the
  annual cut carried and v2 later revised), one row per affected bank, so the
  count grows with the scope: 3 rows at 25 banks, 7 at 48. Valley's edge row
  was also 9.7% out (a late-2024 issuance the legacy shrout missed), tripping
  the 5% ceiling. Both tests now apply to interior rows only; retx must still
  match on every row. First run rejected v2 for this reason and the new banks
  got CRSP only to 2024-12-31.
- **`init` started the v2 fetch at the store's overall edge.** On a re-run the
  old banks already have v2 rows to 2025-12-31, so `use_v2_after` was set to
  2025-12-31 and a bank added since got no 2025 CRSP rows at all: a year-long
  gap between the legacy cut and the Yahoo window. Symptom: Old National, UMB
  and South State (all with 2025 stock deals) failed `ground_shares_to_crsp`
  because the anchor was a 2024-12-31 shrout. Now keyed off
  `MAX(date) WHERE src_table='dsf'`; the per-permco watermark keeps the old
  banks' rows untouched. Second run then fetched 2,750 v2 rows (11 x 250).

### Mergers

29 lines added to mergers.txt (WAL 1, EWBC 1, FHN 2, ONB 9, UMBF 2, SSB 7,
VLY 5, plus Atlantic Capital), every one within the 15% backtest before the
run; worst are WAL/Bridge Capital +10.3% (WAL grew ~10% organically that
quarter) and SSB/CenterState -9.6%. Three RSSD traps worth recording: Sterling
Bancorp NY is 3083291 (the former Provident New York Bancorp), not 3852031
(a Michigan bank of the same name); IBERIABANK is 2291914, whose raw name
column still reads ISB Financial in old rows; Heartland Financial USA is
1206546, filed as Heartland Bancorp. Atlantic Capital stopped filing Y-9C in
2018 (below threshold) so its 2022 line carries `liab=3446062` from its Q4
2021 results 8-K. Macatawa (Wintrust 2024, 3.1%, also a lapsed filer) left out.

### Verification

Pre-init store copied to `.scratchpad/extend_next10/pre_init/`; compared after
the final run (`verify_unchanged.py` there):

- `pd_panel`, 37 pre-existing banks: 49,200 rows on both sides, zero one-sided
  rows, zero np_PD / merton_PD / sE differences (max abs diff 2e-16), including
  the Yahoo-era weeks that runs 3 and 4 recomputed from scratch.
  `panel_revisions` read "history unchanged" on both runs.
- `group_panel`: 4,749 rows, identical values and `n_members` throughout.
- Dashboard mean recomputed under the export's rule: 2,087 weeks, identical
  `n_banks` and mean on every one.
- New series: nine regionals run to 2026-09-25 (UMB, First Horizon, Old
  National, Popular and Valley from 1986-10-03, the rest from their listing),
  Zions switches from `y9c` under 1027004 to `call_report` under 276579 at the
  2018-10-05 week with E_scaled continuous across the seam, Webster ends
  2026-01-02. 157 bridged weeks across the seven new acquirers (ONB 63, SSB 35,
  VLY 30, UMBF 11, FHN 9, EWBC 9; WAL/Bridge Capital bridged none, CRSP
  re-based after the next filing).
- Rolling-era `checks` (430 days) clean for all 48 banks. The full-history
  sweep at the end of init lists the new banks' 1986+ crisis-era flags
  (se_steps 100, pd_plausibility 56, bs_jumps 64, issuance_bs_lag 1,402 bank
  weeks); those await triage like the original 25 did, and `update` does not
  re-scan them.
- Export: 70,808 panel rows, 48 banks, 1.1 MB. The "All non-failed banks"
  download now filters on `!dead` alone, so it includes the regionals.
- Four `init` runs were needed (v2 rejected; `edge` unbound after the first
  fix; window-only compute; full). Three of the four were pipeline defects
  fixed above, not data problems.

## 2026-08-19 - 12 failed banks added as dead+nogroup

Supervisor-flag backtest needed failed banks beyond SVB. Added Wachovia,
National City, Colonial, Corus, UCBH, W Holding, Doral, First NBC,
Silvergate, Signature, First Republic, Republic First - all `dead nogroup`.
New `nogroup` flag (config/db/groups/checks/dashboard_export): bank-level
PDs only, excluded from group indices and the dashboard mean, so published
group_panel and mean_pd history is unchanged (verified: n_banks identical
on all 2,081 mean weeks after init). Signature and First Republic have no
BHC and exercise the Call Report fallback (bs_source='call_report') for the
first time in production. OTS thrifts (WaMu, IndyMac, Downey, Guaranty,
BankUnited, Countrywide) are NOT addable: no Y-9C, no crsp_frb_link rows.
Pre-1986 failures (Continental Illinois) out of reach (START_DATE).
fallback_rate acked once per dead bank (final-year sE is permanent by
construction). Pre-init store snapshot kept in .scratchpad temporarily.
SVB deliberately stays plain `dead` (in groups/mean, as before).
