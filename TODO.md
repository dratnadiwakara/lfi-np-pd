# TODO

## Make Merton PD solves reproducible

`compute_merton_dtd` runs `merton_pd_from_paper` (scipy `fsolve`, MINPACK) in a
`ThreadPoolExecutor` and turns any exception into NaN. MINPACK is not
thread-safe, so a hard-to-converge row can succeed or fail depending on what
runs beside it. The 2026-10-07 full recompute flipped 4 of 66,746 pre-existing
rows between NULL and a value with identical inputs (FITB 2009-04-10, C
2009-06-26, UMBF 2006-10-20, COF 2023-09-01; see NOTES.md 2026-10-07).

- Draft in `.scratchpad/`: solve serially or in a process pool, and confirm the
  full panel reproduces exactly across two runs.
- Make `verify_unchanged.py`-style comparisons null-aware (a value turning
  NULL is a change).
- Integrate only after review.

## Add Prosperity / Stellar Bancorp to mergers.txt

Closed 2026-07-01; Prosperity issued 19.4m shares (Yahoo step 2026-07-02,
x1.19). Stellar Bancorp's last Y-9C is 2026-06-30 (RSSD 3594872). PB's PD from
2026-07-03 reads low until the bridge is in, and issuance_bs_lag flags those
weeks.

- Wait for Prosperity's 2026-09-30 Y-9C (expected mid-November 2026); the
  backtest in `tests/test_merger_bridge.py` needs it.
- Then add `1109599  3594872  close=2026-07-01   # PB / Stellar Bancorp`, run
  the merger tests, and run `lfinp update` (the stale-row recompute picks up
  the bridged weeks).
