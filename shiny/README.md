# LFI PD Dashboard - R Shiny

By **Dimuthu Ratnadiwakara**.

Weekly NP (Nagel-Purnanandam 2019) and classic Merton probability of default
for the 25 large financial institutions in `banks.txt`. Friday-anchored,
5-year horizon. Hosted free on shinyapps.io.

The app reads four parquet files under `data/` and nothing else. It never
opens `data/lfi_pd.duckdb`: DuckDB is single-writer, so a reader here could
block the weekly run, and the store is not present on shinyapps.io anyway.

| file | rows | what |
|---|---|---|
| `data/pd_panel.parquet` | ~41k | `week_date, permco, np_PD, merton_PD, bs_bridged` |
| `data/banks.parquet` | 25 | `permco, rssd, name, ticker, grp, dead` |
| `data/mean_pd.parquet` | ~2.1k | equal-weight cross-sectional mean + `n_banks` |
| `data/meta.parquet` | 1 | build stamp and coverage, printed in the footer |

`permco` is the series key, not `rssd`. Three banks (Regions, BNY Mellon,
KeyCorp) changed RSSD mid-history, and keying on rssd splits them into
part-series with a gap and no error.

## Run locally

```r
install.packages(c("shiny", "nanoparquet", "dplyr", "plotly", "DT", "rsconnect"))
shiny::runApp("shiny", port = 4500, launch.browser = TRUE)
```

Run it from the repo root - `app.R` resolves `data/` relative to the app
directory, which is what `runApp("shiny")` sets.

## Refresh the data

After a weekly `lfinp update` that passed its checks:

```bash
PYTHONPATH=. "C:/envs/bank-pd-venv/Scripts/python.exe" \
  -m lfinp.cli export-dashboard
```

Writes all four files into `shiny/data/`. `lfinp status` prints a `dashboard`
line showing the feed's build date and marks it `STALE` when the panel has
moved past it.

The export is deliberately **not** wired into `lfinp update`: publishing is a
human decision, and nothing new should be able to fail a 20-minute run.

## Deploy to shinyapps.io

One-time:

1. Create a free account at https://www.shinyapps.io.
2. Dashboard -> avatar -> **Tokens** -> **Show** -> copy the
   `rsconnect::setAccountInfo(...)` snippet -> paste into R. Credentials land
   in the rsconnect user config; `rsconnect/` here is gitignored.

Every publish:

```bash
python -m lfinp.cli export-dashboard    # refresh
```
```r
shiny::runApp("shiny", port = 4500)     # look at it first
source("shiny/deploy.R")                # then publish
```

Commit `shiny/data/` when you deploy - rsconnect uploads the app directory
from disk, so a clone without the parquets deploys an app with no data.

App URL: `https://<account>.shinyapps.io/lfi-np-pd/`.

## Notes on reading the chart

- **All-bank average** is equal-weighted across whichever banks report that
  week. Membership changes (the panel starts thin in 1986; SVB stops
  contributing in 2023), so hover shows the bank count behind each point.
  Weeks with fewer than 5 banks are dropped from the file entirely.
- **Pro-forma weeks** (`bs_bridged`, off by default) are the weeks between an
  acquisition closing and the acquirer's next Y-9C, where `total_liab` is the
  combined firm rather than as filed. Without that bridge the PD reads
  artificially low; the marker says the number is corrected, not reported.
- Inactive banks (SVB) stay selectable; their series simply ends.
