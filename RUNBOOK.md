# Runbook

What to do when the weekly run stops. Written for someone who did not build
this. If you are unsure, the safe answer is always: **stop and ask. Do not
re-run with a `--skip` or `--allow` flag to make an error go away.** Those
flags exist for one situation each, listed below, and every other use puts a
wrong number in front of someone.

Background and rationale live in `README.md`. This file is only the response.

---

## The weekly run

Every week, one command, from the repo root:

```bash
PYTHONPATH=. "C:/envs/bank-pd-venv/Scripts/python.exe" -m lfinp.cli update
```

Takes 15-20 minutes. Most of that is the PD computation, not the download.

**It worked if** the last line is `update complete` and the exit code is 0.
Check the code with `echo $?` (bash) or `$LASTEXITCODE` (PowerShell).

**Then confirm:**

```bash
PYTHONPATH=. "C:/envs/bank-pd-venv/Scripts/python.exe" -m lfinp.cli status
```

Five things to look at:

| line | expected |
|---|---|
| `yf_daily latest` | last Friday, or later |
| `pd_panel latest` | last Friday |
| `group_panel latest` | last Friday |
| `snapshots latest` | today, files count went up by 1 |
| check results | all `OK`, or flags you have reviewed |

If `update` printed `FLAG` lines, read the next section. If it exited
non-zero, find your error message under "When the run stops".

---

## Two kinds of problem

This matters more than any individual error. The pipeline separates them
deliberately.

**ABORT** — bad data tried to get in. The run stops, **nothing is saved**,
exit code is non-zero. The stored numbers are untouched and still fine. You
have time. Nothing is broken until you force it in.

**FLAG** — the data went in, and the computed result looks unusual. The run
completes and saves normally. A flag is not automatically an error: every
real crisis (2008, COVID, SVB) sets these off, correctly. Your job is to
decide which it is.

---

## FLAGS: how to respond

A flag stays on the report forever until someone reviews it and records a
verdict. That is on purpose. If you ignore flags, you will ignore the real
one when it comes.

**Step 1.** Look at the detail rows the run printed. They name the bank, the
date, and the cause.

**Step 2.** Decide: real market event, or data problem?

The single most useful signal is `peer_divergence`. A crash moves every bank
at once. A data error moves one bank while the others sit still. `se_steps`
prints exactly this — the culprit day, its return, and how far it was from
the other banks that same day.

- Peers moved too -> real event.
- One bank alone, no news -> data problem.

Cross-check anything you are unsure about against a public price chart for
that bank on that date. Two minutes, and it settles most cases.

**Step 3.** Record the verdict. This suppresses that specific flag from
future runs.

```bash
# one flag
PYTHONPATH=. "C:/envs/bank-pd-venv/Scripts/python.exe" -m lfinp.cli ack \
  --check se_steps --rssd 1199611 --date 2026-07-24 \
  --verdict real --note "Q2 earnings miss, peers also down"

# a whole period (re-runs the check, only acks flags that really exist)
PYTHONPATH=. "C:/envs/bank-pd-venv/Scripts/python.exe" -m lfinp.cli ack \
  --check se_steps --from 2020-02-15 --to 2020-05-01 \
  --verdict real --note "COVID crash"
```

`--verdict` is `real` (genuine market event), `artifact` (data problem — see
below), or `accepted` (known, tolerated).

**Write a real note.** In a year, the note is the only reason anyone will
trust the number. "checked" is not a note.

If you conclude it is a data problem, that is not something to ack and move
on from — escalate. Nothing else in the pipeline will catch it a second time.

### What each flag means

| flag | plain meaning | usually |
|---|---|---|
| `se_steps` | volatility jumped in one week | real if peers moved; data problem if not |
| `peer_divergence` | one bank moved far from the others | earnings, M&A, or a bad price |
| `pd_plausibility` | PD tripled or fell to a third in a week | follows whatever `se_steps` found |
| `bs_jumps` | liabilities moved >30% in a quarter | usually a merger, sometimes a bad filing |
| `pd_decoupling` | the two PD measures stopped agreeing | not a data issue — the model, not the input |
| `fallback_rate` | the model is extrapolating a lot | the bank is far outside normal ranges |
| `panel_revisions` | past PDs changed since last run | expected after a vendor correction; check `pull_diff` for the cause |
| `issuance_bs_lag` | bank issued shares; its liabilities are still last quarter's | not a data problem. See below |
| `group_composition` | a sector index gained or lost a bank | not a data problem. See below |

### About `issuance_bs_lag`

A bank bought something, or raised capital, by issuing shares. Its market value
jumped. Its reported liabilities will not include the new business until the
next quarterly filing, which can be up to a quarter away.

For those weeks the PD is computed from **new equity over old liabilities**, so
it comes out **too low**. The bank looks safer than it is. That is the direction
that matters — nobody double-checks a number that looks fine.

Nothing is broken and nothing needs fixing. The correct interim liability figure
does not exist anywhere the pipeline can reach. What you do:

- Do not cite that bank's PD for those weeks without saying it is understated.
- The `equity_ratio` column tells you how much the equity base grew (1.67 = up
  67%). Bigger ratio, bigger the understatement.
- The flag clears by itself when the next quarterly filing lands. You do not
  have to do anything to make that happen.
- Ack it with the deal name once you have confirmed what it was:

```bash
PYTHONPATH=. "C:/envs/bank-pd-venv/Scripts/python.exe" -m lfinp.cli ack \
  --check issuance_bs_lag --rssd 2277860 --from 2025-05-30 --to 2025-06-27 \
  --verdict accepted --note "Discover close 2025-05-18; Q2 Y-9C lands 2025-07-04"
```

Rows with `scope = group` are the same event seen in a sector index; their
`rssd` is the group's negative id and `cap_share` is how much of that sector the
issuing bank is.

**If you cannot find any deal, merger or capital raise behind it, escalate.** An
equity base that moves with no corporate action is a share-count error.

### About `group_composition`

The pipeline also computes a PD for each business-model group, treating the
whole group as one merged bank (`group_panel`). That number is a cap-weighted
index, so it re-levels when a bank joins or leaves the group — Synchrony listing
in 2014, SVB failing in 2023.

This flag says a level shift at that date is **composition, not risk**. Ack it
with the group's negative id and say which bank moved:

```bash
PYTHONPATH=. "C:/envs/bank-pd-venv/Scripts/python.exe" -m lfinp.cli ack \
  --check group_composition --rssd -4 --date 2023-03-17 \
  --verdict real --note "SVB fails, leaves the lender index"
```

Ids: `-1` moneycenter, `-2` dealer, `-3` feebased, `-4` lender.

If the count changed and **no** bank was added, removed, delisted or failed,
that is a real problem — a member stopped reporting. Escalate.

---

## When the run stops (ABORT)

Match the message. Each one below explains the real cause, because these
errors are usually right.

### `X live bank(s) lack equity data within 5 business days`

The named bank stopped updating. Almost always a **ticker rename** — the old
symbol still exists at Yahoo but returns nothing.

Check what the bank's ticker is today. If it changed, add it to
`CRSP_TO_YF_ALIASES` in `lfinp/config.py`:

```python
"BK": "BNY",   # old CRSP ticker : current Yahoo ticker
```

Then re-run `update`. If the ticker did **not** change, escalate — either
Yahoo is broken or the bank is being delisted.

This gate exists because the predecessor repo silently lost Bank of New York
Mellon for 10 weeks after exactly this rename. Do not work around it.

### `Yahoo returned no price data for <dates>`

Same cause as above, caught earlier. Same fix.

### `pull returns do not reproduce CRSP retx - wrong symbol / share class`

The downloaded prices do not match the known history for that company. You
are pulling **the wrong company**. Usually a reused ticker, or the wrong
share class.

Fix the alias in `config.py`. Never bypass this one.

### `market-cap level break vs stored data - share-basis problem`

Prices are right, share count is wrong, so the company's size jumped. Yahoo
sometimes reports a permanently wrong share count.

Escalate. `--skip-level-check` exists only for a confirmed, understood,
genuinely permanent change in the share count — and confirming that is not a
5-minute job.

### `unexplained share jump within last 5 trading days - refusing`

The share count moved a lot and it is too recent to tell whether it is a real
event (a merger) or a vendor glitch that will correct itself.

**Wait a week and re-run.** By then the pipeline can classify it on its own:
a glitch reverts, a merger persists. That is the whole reason it refuses
rather than guessing.

Only use `--allow-share-jump` if you have independently confirmed the share
count is genuinely correct — a real merger that closed that week.

### `Yahoo share count never comes within 15% of the CRSP anchor`

Yahoo merged two companies' share histories, usually after a rename. The
prices are fine; the share counts are not. Escalate.

### `ticker ... last valid <date>, Nd before CRSP edge - reuse risk`

The ticker stopped being that company's ticker a while ago and may now belong
to someone unrelated. Confirm who owns the ticker today, then add an explicit
alias. Do not assume.

### `Missing group=` / `Unknown group '<x>'`

Someone edited `banks.txt` and left a line without a valid business-model group.
Add one of `group=moneycenter`, `group=dealer`, `group=feebased`, `group=lender`
to that line. Nothing was downloaded; the run stops at parse time.

### `RSSDs not found in link table` / `map to multiple permcos`

A bank in `banks.txt` cannot be matched to market data. Happens after you add
a new bank. The RSSD is wrong, or the company needs a manual decision.
Escalate.

### `WARN WRDS unavailable, continuing on Yahoo alone`

Not an abort. The run continues correctly on Yahoo. Ignore unless it repeats
for weeks — CRSP is what eventually replaces and corrects the Yahoo era.

### Anything else / it crashed

Do not re-run repeatedly. Save the full output and escalate. Nothing was
saved, so nothing is getting worse while you wait.

---

## Missed a week?

Nothing to do. Just run `update`. It re-downloads the last 15 months every
time and fills in whatever is missing. Missing several weeks is fine.

---

## Numbers changed since last time

Expected, within limits, and not a bug.

Recent weeks (last ~15 months) can change between runs. If the data vendor
corrects a price, the pipeline absorbs the correction and the affected PDs
move. Weeks older than that are frozen.

**So: never cite live `pd_panel`.** Cite a dated file:

```
data/snapshots/pd_panel_2026-08-16.parquet
```

To see what a number used to be, and what it is now:

```sql
SELECT snapshot_date, np_PD
FROM read_parquet('data/snapshots/pd_panel_*.parquet')
WHERE rssd = 1073757 AND week_date = DATE '2026-05-15'
ORDER BY snapshot_date;
```

To find out *why* it changed, look in `pull_diff` for that bank around that
date — it records every input value the re-pull changed, per run.

---

## Someone asks "where does this number come from?"

Everything needed is in one row of the input snapshot:

```sql
SELECT * FROM read_parquet('data/snapshots/pd_input_2026-08-16.parquet')
WHERE rssd = 1073757 AND week_date = DATE '2026-05-15';
```

That row has the liabilities, the interest rate, the market value, the
volatility, and `retx_csv` — the 252 daily returns the volatility was
computed from. They can check it themselves:

```sql
SELECT sE, (SELECT stddev_samp(CAST(v AS DOUBLE)) * sqrt(252)
              FROM unnest(str_split(retx_csv, ',')) t(v) WHERE v <> '')
FROM read_parquet('data/snapshots/pd_input_2026-08-16.parquet')
WHERE rssd = 1073757 AND week_date = DATE '2026-05-15';
```

Those two values must be equal.

---

## Do not do these

- **Do not delete `data/snapshots/`.** Everything else in `data/` can be
  rebuilt by `lfinp init`. These files cannot. They are the only record of
  what was published.
- **Do not run `lfinp init` to fix a problem.** It rebuilds from scratch
  (~45 min) and re-downloads all Yahoo history, which replaces the frozen
  data behind old PDs. It is for first setup and for adding banks. Nothing
  else.
- **Do not add a `--skip` or `--allow` flag to a scheduled job.** It disables
  a gate permanently and nobody will remember it is there.
- **Do not edit `data/lfi_pd.duckdb` by hand.**

---

## Adding or removing a bank

Edit `banks.txt`. One RSSD per line. Every line needs a business-model group —
`group=moneycenter`, `group=dealer`, `group=feebased`, or `group=lender` (README
says what each means). Mark failed banks `dead` so no price download is
attempted. A missing or misspelled group stops the run immediately, before
anything is downloaded.

Then:

```bash
PYTHONPATH=. "C:/envs/bank-pd-venv/Scripts/python.exe" -m lfinp.cli init
```

~45 minutes. Existing banks are not re-downloaded from CRSP, but the Yahoo
era is re-pulled — so take a snapshot first if anyone has cited recent
numbers.

---

## Health check any time (read-only, safe)

```bash
PYTHONPATH=. "C:/envs/bank-pd-venv/Scripts/python.exe" -m lfinp.cli status
PYTHONPATH=. "C:/envs/bank-pd-venv/Scripts/python.exe" -m lfinp.cli checks
PYTHONPATH=. "C:/envs/bank-pd-venv/Scripts/python.exe" -m lfinp.cli checks --all
PYTHONPATH=. "C:/envs/bank-pd-venv/Scripts/python.exe" -m pytest tests -q
```

`checks` covers the recent period; `--all` sweeps all history and will be
noisy until historical crises are acked. The tests must always pass — if they
fail, a core safeguard has been broken and no run should be trusted.

---

## Escalation

Owner: Dimuthu Ratnadiwakara.

Include: the full console output, the output of `lfinp status`, and the date
of the last successful run. Nothing was saved on an abort, so the stored data
is still good and there is no rush to force it.
