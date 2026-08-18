# Shiny dashboard for the lfi-np-pd weekly NP / Merton PD panel.
#
# Reads four parquets shipped under data/, and nothing else. The DuckDB store
# is never opened: it is 64 MB, single-writer (a reader here could block the
# weekly run), and absent on shinyapps.io.
#
#   data/pd_panel.parquet  week_date, permco, np_PD, merton_PD, bs_bridged
#   data/banks.parquet     permco, rssd, name, ticker, grp, dead
#   data/mean_pd.parquet   week_date, n_banks, np_PD, merton_PD
#   data/meta.parquet      built_at, week_min, week_max, n_panel_rows, ...
#
# Rebuild them with:  python -m lfinp.cli export-dashboard
#
# permco is the series key, not rssd: three banks changed RSSD mid-history and
# an rssd key would split them into part-series.

suppressPackageStartupMessages({
  library(shiny)
  library(nanoparquet)
  library(dplyr)
  library(plotly)
  library(DT)
})

# -- Data --------------------------------------------------------------------

DATA_DIR <- file.path(getwd(), "data")
PANEL_PQ <- file.path(DATA_DIR, "pd_panel.parquet")
BANKS_PQ <- file.path(DATA_DIR, "banks.parquet")
MEAN_PQ  <- file.path(DATA_DIR, "mean_pd.parquet")
META_PQ  <- file.path(DATA_DIR, "meta.parquet")

stopifnot(file.exists(PANEL_PQ), file.exists(BANKS_PQ),
          file.exists(MEAN_PQ), file.exists(META_PQ))

PD_PANEL <- read_parquet(PANEL_PQ) |>
  mutate(week_date = as.Date(week_date),
         bs_bridged = as.logical(bs_bridged))

MEAN_PD <- read_parquet(MEAN_PQ) |>
  mutate(week_date = as.Date(week_date))

META <- read_parquet(META_PQ)

BANKS <- read_parquet(BANKS_PQ) |>
  mutate(
    label = sprintf(
      "%s%s%s",
      name,
      ifelse(!is.na(ticker) & ticker != "", sprintf(" (%s)", ticker), ""),
      ifelse(dead, " [inactive]", "")
    )
  ) |>
  arrange(name)

WK_MIN <- min(PD_PANEL$week_date)
WK_MAX <- max(PD_PANEL$week_date)

# Same 10 colors as the sibling bank-pd dashboard, so the two read alike.
PALETTE <- c("#1f77b4", "#d62728", "#2ca02c", "#9467bd", "#ff7f0e",
             "#8c564b", "#e377c2", "#17becf", "#bcbd22", "#7f7f7f")

# Bank of America: the largest name with full history back to 1986.
DEFAULT_PERMCO <- if (3151 %in% BANKS$permco) 3151 else BANKS$permco[1]

pd_label <- function(col) if (col == "np_PD") "NP PD" else "Merton PD"

# -- UI ----------------------------------------------------------------------

ui <- fluidPage(
  titlePanel("Large Financial Institutions - Probability of Default"),
  tags$h4("by Dimuthu Ratnadiwakara",
          style = "margin-top:-10px;color:#444;font-weight:500;"),
  tags$p(
    style = "color:#666;",
    "Modified-Merton PD (Nagel & Purnanandam 2019) and classic Merton PD for ",
    "25 large US bank holding companies. Friday-anchored weekly, 5-year horizon."
  ),
  tags$div(
    style = paste("background:#fdf6e3;border-left:4px solid #d9a441;",
                  "padding:10px 14px;margin:0 0 16px 0;color:#5a4a2a;",
                  "font-size:13px;"),
    tags$b("Illustrative only."),
    " These figures are a research replication of a published academic model, ",
    "produced from public regulatory filings and market data. They are model ",
    "estimates, not forecasts, credit ratings, or any assessment of an ",
    "institution's actual solvency, and they are not investment advice. ",
    "The underlying data revise between runs. Do not rely on these numbers ",
    "for any financial, regulatory, or commercial decision."
  ),
  sidebarLayout(
    sidebarPanel(
      width = 3,
      selectizeInput(
        "banks", "Banks (search by name or ticker)",
        choices  = setNames(BANKS$permco, BANKS$label),
        selected = DEFAULT_PERMCO,
        multiple = TRUE,
        options  = list(plugins = list("remove_button"))
      ),
      tags$hr(),
      radioButtons("pd_type", "PD measure",
                   choices = c("NP PD" = "np_PD", "Merton PD" = "merton_PD"),
                   selected = "np_PD", inline = TRUE),
      checkboxInput("show_mean_all", "Show all-bank average", value = TRUE),
      checkboxInput("show_bank_mean", "Show each bank's own mean", value = FALSE),
      checkboxInput("show_bridge", "Mark pro-forma (merger) weeks", value = FALSE),
      tags$hr(),
      dateRangeInput("date_range", "Date range",
                     start = WK_MIN, end = WK_MAX,
                     min = WK_MIN, max = WK_MAX, format = "yyyy-mm-dd"),
      tags$hr(),
      downloadButton("download", "Download CSV", class = "btn-primary")
    ),
    mainPanel(
      width = 9,
      plotlyOutput("plot", height = "520px"),
      tags$br(),
      h4(textOutput("summary_title")),
      DTOutput("summary"),
      tags$hr(),
      tags$small(textOutput("footer"), style = "color:#888;"),
      tags$small(
        style = "color:#888;display:block;margin-top:6px;",
        "The all-bank average is an equal-weighted mean across every bank ",
        "reporting that week; its membership changes over time, so hover to ",
        "see the bank count behind each point. Pro-forma weeks are those ",
        "between an acquisition closing and the acquirer's next regulatory ",
        "filing, where liabilities are the two firms combined rather than as ",
        "filed."
      )
    )
  )
)

# -- Server ------------------------------------------------------------------

server <- function(input, output, session) {

  selected_panel <- reactive({
    permcos <- as.integer(input$banks)
    if (length(permcos) == 0) return(NULL)
    dr <- input$date_range
    PD_PANEL |>
      filter(permco %in% permcos,
             week_date >= dr[1], week_date <= dr[2]) |>
      left_join(BANKS |> select(permco, label, name, ticker, rssd),
                by = "permco") |>
      arrange(permco, week_date)
  })

  selected_mean <- reactive({
    dr <- input$date_range
    MEAN_PD |> filter(week_date >= dr[1], week_date <= dr[2])
  })

  output$plot <- renderPlotly({
    df <- selected_panel()
    validate(need(!is.null(df) && nrow(df) > 0,
                  "Pick one or more banks to see PD time series."))
    pd_col <- input$pd_type
    lbl    <- pd_label(pd_col)

    fig <- plot_ly()
    labels <- unique(df$label)

    for (i in seq_along(labels)) {
      this  <- labels[i]
      color <- PALETTE[((i - 1) %% length(PALETTE)) + 1]
      sub   <- df |> filter(label == this) |> arrange(week_date)

      fig <- fig |> add_trace(
        x = sub$week_date, y = sub[[pd_col]],
        type = "scatter", mode = "lines", name = this,
        line = list(color = color, width = 1.4),
        hovertemplate = paste0("<b>", this, "</b><br>%{x|%Y-%m-%d}<br>",
                               lbl, ": %{y:.4f}<extra></extra>")
      )

      # Pro-forma weeks ride the bank's own line as open circles rather than a
      # separate legend series: they are the same number, differently sourced.
      if (isTRUE(input$show_bridge)) {
        br <- sub |> filter(bs_bridged, !is.na(.data[[pd_col]]))
        if (nrow(br) > 0) {
          fig <- fig |> add_trace(
            x = br$week_date, y = br[[pd_col]],
            type = "scatter", mode = "markers",
            marker = list(color = "rgba(0,0,0,0)", size = 9,
                          line = list(color = color, width = 1.8)),
            showlegend = FALSE, name = paste0(this, " (pro-forma)"),
            hovertemplate = paste0("<b>", this, "</b><br>%{x|%Y-%m-%d}<br>",
                                   lbl, ": %{y:.4f}<br>",
                                   "pro-forma liabilities<extra></extra>")
          )
        }
      }

      if (isTRUE(input$show_bank_mean)) {
        m <- mean(sub[[pd_col]], na.rm = TRUE)
        if (is.finite(m)) {
          xs <- range(sub$week_date)
          fig <- fig |> add_trace(
            x = xs, y = c(m, m), type = "scatter", mode = "lines",
            line = list(color = color, width = 1.0, dash = "dash"),
            opacity = 0.6, showlegend = FALSE, hoverinfo = "skip",
            name = paste0("mean ", this)
          )
        }
      }
    }

    # Black dashed, drawn last: a benchmark, never mistakable for a bank.
    if (isTRUE(input$show_mean_all)) {
      mn <- selected_mean()
      if (nrow(mn) > 0) {
        fig <- fig |> add_trace(
          x = mn$week_date, y = mn[[pd_col]],
          type = "scatter", mode = "lines", name = "All-bank average",
          line = list(color = "#111111", width = 1.6, dash = "dash"),
          customdata = mn$n_banks,
          hovertemplate = paste0("<b>All-bank average</b><br>%{x|%Y-%m-%d}<br>",
                                 lbl, ": %{y:.4f}<br>",
                                 "banks: %{customdata}<extra></extra>")
        )
      }
    }

    fig |> layout(
      xaxis = list(title = "Friday week"),
      yaxis = list(title = paste(lbl, "(5-year horizon)")),
      hovermode = "x unified",
      legend = list(orientation = "h", x = 0, y = 1.08),
      margin = list(l = 10, r = 10, t = 30, b = 10)
    )
  })

  output$summary_title <- renderText({
    paste(pd_label(input$pd_type), "summary over the selected window")
  })

  output$summary <- renderDT({
    df <- selected_panel()
    validate(need(!is.null(df) && nrow(df) > 0, ""))
    pd_col <- input$pd_type

    tbl <- df |>
      group_by(Bank = label) |>
      summarise(
        n_weeks = sum(!is.na(.data[[pd_col]])),
        mean    = mean(.data[[pd_col]], na.rm = TRUE),
        median  = median(.data[[pd_col]], na.rm = TRUE),
        max     = max(.data[[pd_col]], na.rm = TRUE),
        last    = dplyr::last(na.omit(.data[[pd_col]])),
        .groups = "drop"
      )

    mn <- selected_mean()
    if (nrow(mn) > 0) {
      tbl <- bind_rows(tbl, tibble(
        Bank = "All-bank average",
        n_weeks = sum(!is.na(mn[[pd_col]])),
        mean    = mean(mn[[pd_col]], na.rm = TRUE),
        median  = median(mn[[pd_col]], na.rm = TRUE),
        max     = max(mn[[pd_col]], na.rm = TRUE),
        last    = dplyr::last(na.omit(mn[[pd_col]]))
      ))
    }

    datatable(tbl, rownames = FALSE,
              options = list(dom = "t", pageLength = 50)) |>
      formatRound(c("mean", "median", "max", "last"), digits = 4)
  })

  output$footer <- renderText({
    df <- selected_panel()
    n  <- if (is.null(df)) 0 else nrow(df)
    sprintf(paste("Source: lfi-np-pd weekly panel - PD horizon 5 yr -",
                  "data covers %s to %s - selection returned %s rows -",
                  "built %s."),
            format(WK_MIN), format(WK_MAX), format(n, big.mark = ","),
            substr(META$built_at[1], 1, 10))
  })

  output$download <- downloadHandler(
    filename = function() {
      dr <- input$date_range
      sprintf("lfi_np_pd_%s_%s_%dbanks.csv",
              format(dr[1], "%Y%m%d"), format(dr[2], "%Y%m%d"),
              length(input$banks))
    },
    content = function(file) {
      df <- selected_panel()
      if (is.null(df)) df <- PD_PANEL[0, ]
      out <- df |>
        select(week_date, permco, rssd, name, ticker,
               np_PD, merton_PD, bs_bridged)
      write.csv(out, file, row.names = FALSE)
    }
  )
}

shinyApp(ui, server)
