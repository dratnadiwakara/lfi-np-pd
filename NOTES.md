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
