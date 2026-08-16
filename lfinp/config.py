"""
Central configuration for lfi-np-pd.

Minimal weekly NP/Merton PD pipeline for a small, explicitly listed set of
large financial institutions (banks.txt). Compute constants follow
Nagel-Purnanandam (2019), identical to the bank-pd repo this replaces for
LFI purposes.

Paths default to absolute Windows locations; override with env vars.
Secrets (FRED key, WRDS creds) come from a YAML at
C:\\key-variables\\key-variables.yaml.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import yaml

# -- Repo layout --------------------------------------------------------------

LFINP_ROOT = Path(__file__).resolve().parent.parent

DATA_DIR = LFINP_ROOT / "data"
INPUTS_DIR = LFINP_ROOT / "inputs"
BANKS_PATH = Path(os.getenv("LFINP_BANKS_PATH", str(LFINP_ROOT / "banks.txt")))

# -- External data (sibling repo) ---------------------------------------------

EMPIRICAL_ROOT = Path(
    os.getenv("FIN_DATA_ROOT", r"C:\empirical-data-construction")
)

# -- Secrets ------------------------------------------------------------------

SECRETS_PATH = Path(
    os.getenv("LFINP_SECRETS", r"C:\key-variables\key-variables.yaml")
)

# -- Compute constants (preserved from NP paper / version2) --------------------

VOL_VALUE = 0.2
T_PD = 5.0
GAMMA_PD = 0.002

# -- Pipeline constants ---------------------------------------------------------

START_DATE = "1986-01-01"
VOL_WINDOW = 252               # trading days
VOL_MIN_PERIODS = 126

# Yahoo rolling re-pull: every run replaces the last N months wholesale so the
# entire vol window shares one split-adjustment basis and vendor revisions
# self-heal. 15 months > 252 trading days with margin.
YF_REPULL_MONTHS = int(os.getenv("LFINP_YF_REPULL_MONTHS", "15"))
# Pre-window buffer so the first in-window day has a within-pull prev close.
YF_BUFFER_DAYS = 10

# -- Check thresholds (all env-overridable) -------------------------------------

# 1: share-count day-over-day jump unexplained by a split
SHARE_JUMP_THRESHOLD = float(os.getenv("LFINP_SHARE_JUMP_THRESHOLD", "0.20"))
SHARE_JUMP_PERSIST_DAYS = 10      # M&A auto-pass once new level persists this long
# 2: CRSP-overlap return cross-validation
XVAL_MIN_CORR = float(os.getenv("LFINP_XVAL_MIN_CORR", "0.995"))
XVAL_MAX_BIG_DIFF_SHARE = float(os.getenv("LFINP_XVAL_MAX_BIG_DIFF_SHARE", "0.02"))
XVAL_BIG_DIFF_BP = 50.0
XVAL_MIN_OVERLAP_DAYS = 60
# 3: re-pull diff sensitivities
DIFF_MCAP_REL = 0.001
DIFF_RETX_ABS = 0.0001
# 4: level continuity
LEVEL_WARN = 0.01
LEVEL_FAIL = 0.10
LEVEL_FAIL_MAX_DAYS = 3
# 5: coverage
COVERAGE_MAX_LAG_BDAYS = 5
# 6: PD plausibility
PD_JUMP_FACTOR = 3.0
SE_JUMP_FACTOR = 1.5

# -- Output-side anomaly detection ------------------------------------------------
#
# The failure mode these target: a single bad daily return enters the 252-day
# vol window, sE steps up overnight, PD stays wrong for exactly one year, then
# falls off a cliff when that day exits. Real risk builds and decays; artifacts
# step and cliff. 7: sE step size that cannot come from one ordinary day.
SE_STEP_THRESHOLD = float(os.getenv("LFINP_SE_STEP_THRESHOLD", "0.25"))
# 8: a daily return this far from the peer median is idiosyncratic - real
# market crashes move every bank together.
PEER_DIVERGENCE_THRESHOLD = float(os.getenv("LFINP_PEER_DIVERGENCE", "0.15"))
PEER_MIN_BANKS = 8          # below this the median is not a reliable reference
# 9: value-surface fallback rate (kernel extrapolating outside its hull)
FALLBACK_RATE_THRESHOLD = float(os.getenv("LFINP_FALLBACK_RATE", "0.20"))
FALLBACK_WINDOW_WEEKS = 52
# 10: np_PD vs merton_PD decoupling while inputs are stable -> kernel issue
DECOUPLE_FACTOR = 2.0
DECOUPLE_SE_STABLE = 0.10
# 11: quarter-over-quarter balance-sheet jump (Y-9C / Call Report side)
BS_JUMP_THRESHOLD = float(os.getenv("LFINP_BS_JUMP", "0.30"))
# 12: historical pd_panel values changing between runs
PANEL_REVISION_TOL = 1e-6
# 14: equity base changed by issuance while the balance sheet is still the
# pre-issuance quarter. Detected as market cap moving without a matching
# return: (mcap_t / mcap_t-1) / (1 + retx_t), which is ~1.0 unless the share
# count changed and is immune to splits (mcap is split-invariant, retx is
# split-adjusted). 10% is well clear of the rounding noise in a share count
# and well below any issuance large enough to matter (COF/Discover: 1.71).
ISSUANCE_RATIO_TOL = float(os.getenv("LFINP_ISSUANCE_RATIO", "0.10"))
# A group week is only flagged when the affected members are this much of its
# market cap; below that the distortion is smaller than the rounding on the
# number being read.
ISSUANCE_GROUP_CAP_SHARE = float(os.getenv("LFINP_ISSUANCE_GROUP_SHARE", "0.02"))

# Shares-anchor tolerance: after the CRSP edge, Yahoo's share count is
# trusted only once it is within this fraction of the CRSP shrout anchor.
# Yahoo merges share histories across ticker renames (observed: pre-rename
# "BNY" shares belonged to a different entity), so the leading span can be
# wrong while prices are perfectly right.
SHARES_ANCHOR_TOL = float(os.getenv("LFINP_SHARES_ANCHOR_TOL", "0.15"))

# retx_synthetic: gap to previous close beyond this many calendar days
# (3-day holiday weekend is the worst normal case)
SYNTHETIC_GAP_DAYS = 4

# Ticker-reuse guard: refuse Yahoo pull when the CRSP ticker history ended
# more than this many days before the CRSP edge (ticker may have been reused
# by an unrelated company) unless an explicit alias mapping exists.
TICKER_REUSE_GUARD_DAYS = 365

# CRSP -> Yahoo ticker aliases. Includes post-CRSP-edge renames, which look
# like delistings to Yahoo and would otherwise silently stall a bank.
CRSP_TO_YF_ALIASES: dict[str, str] = {
    "BRK.A": "BRK-A", "BRK.B": "BRK-B",
    "BK": "BNY",   # Bank of New York Mellon renamed BK -> BNY (2026)
}

# -- Staleness thresholds --------------------------------------------------------

Y9C_STALE_DAYS = int(os.getenv("LFINP_Y9C_STALE_DAYS", "120"))

# -- DuckDB tuning ----------------------------------------------------------------

DUCKDB_THREADS = 4
DUCKDB_MEMORY_LIMIT = "6GB"


# -- Path helpers -------------------------------------------------------------------


def data_db_path() -> Path:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return DATA_DIR / "lfi_pd.duckdb"


def snapshot_dir() -> Path:
    """Dated pd_panel snapshots. The ONLY non-rebuildable artifact in data/:
    once the rolling window moves past a week, a revision to that week's PD
    can be explained (pull_diff) but the previously-published value cannot be
    recovered from the store. These files are that record."""
    p = DATA_DIR / "snapshots"
    p.mkdir(parents=True, exist_ok=True)
    return p


def value_surface_path() -> Path:
    return INPUTS_DIR / "ValueSurface.mat"


def y9c_db_path() -> Path:
    return EMPIRICAL_ROOT / "y9c" / "y9c.duckdb"


def link_db_path() -> Path:
    return EMPIRICAL_ROOT / "permco-rssd-link" / "permco-rssd-link.duckdb"


def call_reports_db_path() -> Path:
    return EMPIRICAL_ROOT / "call-reports-FFIEC" / "call-reports-ffiec.duckdb"


# -- Bank list -----------------------------------------------------------------------


# Business-model groups. A pooled cross-bank median mixes a card monoline with
# a custodian, so peer comparisons and reporting cuts need this split. The cut
# is by funding source, because that is what a PD is about: deposits (moneycenter,
# lender), wholesale markets (dealer), or fees on assets never on the balance
# sheet (feebased). 'dealer' has only two members - GS and MS are wholesale-funded
# in a way nothing else here is, and merging them into moneycenter would hide
# exactly the distinction the group exists to make. Use it as a pair, not a median.
BANK_GROUPS = ("moneycenter", "dealer", "feebased", "lender")


@dataclass(frozen=True)
class Bank:
    rssd: int
    dead: bool          # True = compute historical PDs only, never pull Yahoo
    comment: str
    group: Optional[str] = None      # one of BANK_GROUPS; None only via env override


def load_banks(path: Optional[Path] = None) -> list[Bank]:
    """Parse banks.txt: one RSSD per line, flags 'dead' / 'group=<g>', '#' comments.

    group is mandatory in the file — an unclassified bank would silently fall
    out of every group-wise cut. Env LFINP_RSSDS (comma-separated) overrides the
    file entirely (all live, no group).
    """
    env = os.getenv("LFINP_RSSDS")
    if env:
        return [Bank(rssd=int(x), dead=False, comment="(env)")
                for x in env.split(",") if x.strip()]
    p = Path(path) if path else BANKS_PATH
    if not p.exists():
        raise FileNotFoundError(f"Bank list not found: {p}")
    banks: list[Bank] = []
    seen: set[int] = set()
    for raw in p.read_text(encoding="utf-8").splitlines():
        line, _, comment = raw.partition("#")
        tokens = line.split()
        if not tokens:
            continue
        rssd = int(tokens[0])
        flags = {t.lower() for t in tokens[1:]}
        group = None
        for f in list(flags):
            if f.startswith("group="):
                group = f.split("=", 1)[1]
                flags.discard(f)
        unknown = flags - {"dead"}
        if unknown:
            raise ValueError(f"Unknown flag(s) {unknown} on line: {raw!r}")
        if group is None:
            raise ValueError(f"Missing group= on line: {raw!r}")
        if group not in BANK_GROUPS:
            raise ValueError(
                f"Unknown group {group!r} on line: {raw!r} - expected one of "
                f"{', '.join(BANK_GROUPS)}")
        if rssd in seen:
            raise ValueError(f"Duplicate RSSD {rssd} in {p}")
        seen.add(rssd)
        banks.append(Bank(rssd=rssd, dead="dead" in flags,
                          comment=comment.strip(), group=group))
    if not banks:
        raise ValueError(f"No banks parsed from {p}")
    return banks


def active_rssds() -> list[int]:
    """All in-scope RSSDs (compute scope)."""
    return [b.rssd for b in load_banks()]


def yahoo_rssds() -> list[int]:
    """RSSDs that get Yahoo pulls (live banks only)."""
    return [b.rssd for b in load_banks() if not b.dead]


def bank_groups() -> dict[int, Optional[str]]:
    """rssd -> business-model group."""
    return {b.rssd: b.group for b in load_banks()}


# -- Secrets ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Secrets:
    fred_api_key: str
    wrds_username: str
    wrds_password: str


def load_secrets(path: Optional[Path] = None) -> Secrets:
    p = Path(path) if path else SECRETS_PATH
    if not p.exists():
        raise FileNotFoundError(f"Secrets file not found: {p}")
    with p.open("r", encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)
    try:
        return Secrets(
            fred_api_key=str(cfg["api_keys"]["fred"]).strip(),
            wrds_username=str(cfg["wrds"]["wrds_username"]).strip(),
            wrds_password=str(cfg["wrds"]["wrds_password"]).strip(),
        )
    except KeyError as exc:
        raise KeyError(f"Missing required key in {p}: {exc}") from exc
