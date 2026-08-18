# Publish the dashboard to shinyapps.io. Local only - never run from CI.
#
# One-time setup: shinyapps.io -> avatar -> Tokens -> Show -> paste the
# rsconnect::setAccountInfo(name, token, secret) snippet into an R session.
# The credentials land in the rsconnect user config, never in this repo
# (rsconnect/ is gitignored).
#
# Refresh the data first, and look at it before publishing:
#   python -m lfinp.cli export-dashboard
#   shiny::runApp("shiny", port = 4500, launch.browser = TRUE)

# account is pinned: more than one shinyapps.io account is registered on this
# machine (the sibling bank-pd repo deploys under a different one), and
# rsconnect refuses to guess.
rsconnect::deployApp(
  appDir      = "shiny",
  appName     = "lfi-np-pd",
  appTitle    = "LFI Probability of Default",
  account     = "dimuthu-r",
  server      = "shinyapps.io",
  forceUpdate = TRUE
)
