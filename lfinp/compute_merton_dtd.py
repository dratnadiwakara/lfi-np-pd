"""
Compute Modified Merton Distance-to-Default and Merton PD from custom input data.

This is a cleaned version of `from_office/compute_np_merton_dtd.py` with:
- `preserve_columns` parameterized
- no CLI / __main__ entry point (the notebook orchestrates execution)
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional, Union
import os

import numpy as np
import pandas as pd
from scipy.io import loadmat
from scipy.spatial import Delaunay, cKDTree

from .merton_pd_from_paper import merton_pd_from_paper


def _load_input(input_csv: Path, preserve_columns: list[str]) -> tuple[pd.DataFrame, pd.DataFrame]:
    df = pd.read_csv(input_csv)
    lc = {c.lower(): c for c in df.columns}

    required = ["e", "permco", "year", "month", "r", "se"]
    missing = [c for c in required if c not in lc]
    if missing:
        raise ValueError(f"Missing required columns in {input_csv}: {missing}")

    normalized = pd.DataFrame(
        {
            "E": df[lc["e"]].astype(float).to_numpy(),
            "permco": df[lc["permco"]].astype(float).to_numpy(),
            "year": df[lc["year"]].astype(float).to_numpy(),
            "month": df[lc["month"]].astype(float).to_numpy(),
            "r": df[lc["r"]].astype(float).to_numpy(),
            "sE": df[lc["se"]].astype(float).to_numpy(),
        }
    )

    preserved = pd.DataFrame()
    for col in preserve_columns:
        c0 = col.lower()
        if c0 in lc:
            preserved[col] = df[lc[c0]]
    return normalized, preserved


def _triangular_rate_weights(r: np.ndarray, rate_grid: np.ndarray) -> np.ndarray:
    """
    Linear interpolation weights across rate slices.

    For r inside [minr, maxr]: weights at the two adjacent grid slices sum to
    1 (standard linear interp). When r equals a grid point exactly, that
    slice gets weight 1 and neighbours get 0.

    For r outside [minr, maxr]: clamped to the boundary slice (NN behavior).
    The original triangular form set w==0 at exact grid matches due to the
    `dr < 0` / `dr > 0` strict inequalities ignoring `dr == 0`; that produced
    artificial near-zero PDs whenever the input rate landed on a grid step.
    """
    minr = float(rate_grid.min())
    maxr = float(rate_grid.max())
    rs = rate_grid.size
    n = r.size
    if rs < 2:
        return np.ones((n, 1), dtype=float)

    rstep = (maxr - minr) / (rs - 1)
    pos = (r - minr) / rstep
    pos_clamped = np.clip(pos, 0.0, rs - 1)
    lo = np.floor(pos_clamped).astype(int)
    hi = np.minimum(lo + 1, rs - 1)
    frac = pos_clamped - lo

    w = np.zeros((n, rs), dtype=float)
    rows = np.arange(n)
    # When lo == hi (at upper boundary), this leaves w[lo]=1 since frac==0.
    w[rows, lo] += 1.0 - frac
    w[rows, hi] += frac
    # If lo == hi the two writes both target the same column; correct because
    # (1-frac) + frac = 1.
    return w


def _interp_many_delaunay(points: np.ndarray, qpoints: np.ndarray, values: np.ndarray) -> np.ndarray:
    tri = Delaunay(points)
    simplex = tri.find_simplex(qpoints)
    out = np.full((qpoints.shape[0], values.shape[1]), np.nan, dtype=float)
    inside = simplex >= 0
    if not np.any(inside):
        return out

    s = simplex[inside]
    X = qpoints[inside]
    T = tri.transform[s, :3, :]
    r = X - tri.transform[s, 3, :]
    bary = np.einsum("nij,nj->ni", T, r)
    w = np.hstack((bary, 1.0 - bary.sum(axis=1, keepdims=True)))
    verts = tri.simplices[s]
    out[inside] = np.einsum("nk,nkj->nj", w, values[verts])
    return out


def _interp_rate_slice(a: int, xEt, xsigEt, xsig, xLt, xBt, xmdef, xfs, xF, qpoints):
    sigEt = xsigEt[:, :, a, :].reshape(-1)
    Et = xEt[:, :, a, :].reshape(-1)
    sig = xsig[:, :, a, :].reshape(-1)
    points = np.column_stack((Et, np.where(np.isfinite(sigEt), sigEt, 99.0), sig))

    values = np.column_stack(
        (
            xLt[:, :, a, :].reshape(-1),
            xBt[:, :, a, :].reshape(-1),
            xmdef[:, :, a, :].reshape(-1),
            xfs[:, :, a, :].reshape(-1),
            xF[:, :, a, :].reshape(-1),
        )
    )
    interp = _interp_many_delaunay(points, qpoints, values)

    # Nearest-neighbor fallback vectors when Delaunay has no support.
    # MATLAB scatteredInterpolant defaults to linear extrapolation outside the
    # convex hull; we approximate with k=4 inverse-distance-weighted NN to get
    # a smooth value rather than the flat single-NN. This matches the authors'
    # behavior for points outside the surface (notably high-vol crisis weeks).
    nn_L = np.full(qpoints.shape[0], np.nan, dtype=float)
    nn_fs = np.full(qpoints.shape[0], np.nan, dtype=float)
    nn_bookF = np.full(qpoints.shape[0], np.nan, dtype=float)
    nn_B = np.full(qpoints.shape[0], np.nan, dtype=float)
    nn_mdef = np.full(qpoints.shape[0], np.nan, dtype=float)
    valid_points = np.isfinite(points).all(axis=1)
    valid_q = np.isfinite(qpoints).all(axis=1)
    if np.any(valid_points) and np.any(valid_q):
        tree = cKDTree(points[valid_points])
        k = min(4, int(valid_points.sum()))
        dists, idx = tree.query(qpoints[valid_q], k=k)
        vals = values[valid_points]
        if k == 1:
            dists = dists[:, None]
            idx = idx[:, None]
        # Inverse-distance weights with epsilon to avoid div-by-zero on exact hits.
        w = 1.0 / (dists + 1e-12)
        w_sum = w.sum(axis=1, keepdims=True)
        nn_L[valid_q] = (vals[idx, 0] * w).sum(axis=1) / w_sum[:, 0]
        nn_B[valid_q] = (vals[idx, 1] * w).sum(axis=1) / w_sum[:, 0]
        nn_mdef[valid_q] = (vals[idx, 2] * w).sum(axis=1) / w_sum[:, 0]
        nn_fs[valid_q] = (vals[idx, 3] * w).sum(axis=1) / w_sum[:, 0]
        nn_bookF[valid_q] = (vals[idx, 4] * w).sum(axis=1) / w_sum[:, 0]
    return a, interp, nn_L, nn_fs, nn_bookF, nn_B, nn_mdef


def _weighted_nansum_or_nan(values: np.ndarray, weights: np.ndarray) -> np.ndarray:
    """
    Weighted aggregation that returns NaN when there is no valid support.

    This avoids converting "no interpolation support" into artificial zeros.
    """
    valid = np.isfinite(values)
    weighted = np.where(valid, values * weights, np.nan)
    out = np.nansum(weighted, axis=1)
    support = np.nansum(np.where(valid, np.abs(weights), 0.0), axis=1)
    out[support <= 0] = np.nan
    return out


def _nearest_rate_fallback(
    nn_by_rate: np.ndarray, r: np.ndarray, rate_grid: np.ndarray
) -> np.ndarray:
    """Fallback to nearest rate-slice NN value for rows with zero rate support."""
    idx = np.argmin(np.abs(r[:, None] - rate_grid[None, :]), axis=1)
    rows = np.arange(r.shape[0])
    return nn_by_rate[rows, idx]


def _run_from_value_surface_fast_parallel(
    input_df: pd.DataFrame,
    value_surface_mat: Path,
    vol_value: float,
    max_workers: Optional[int] = None,
) -> pd.DataFrame:
    mat = loadmat(value_surface_mat)
    xLt = np.asarray(mat["xLt"], dtype=float)
    xBt = np.asarray(mat["xBt"], dtype=float)
    xEt = np.asarray(mat["xEt"], dtype=float)
    xmdef = np.asarray(mat["xmdef"], dtype=float)
    xsigEt = np.asarray(mat["xsigEt"], dtype=float)
    xsig = np.asarray(mat["xsig"], dtype=float)
    xfs = np.asarray(mat["xfs"], dtype=float)
    xF = np.asarray(mat["xF"], dtype=float)
    xr = np.asarray(mat["xr"], dtype=float)

    data_n = input_df.shape[0]
    rs = xr.shape[2]
    vol = np.full(data_n, vol_value, dtype=float)
    E = input_df["E"].to_numpy()
    sE = input_df["sE"].to_numpy()
    qpoints = np.column_stack((E, sE, vol))

    Lr = np.zeros((data_n, rs), dtype=float)
    Br = np.zeros((data_n, rs), dtype=float)
    mdefr = np.zeros((data_n, rs), dtype=float)
    fsr = np.zeros((data_n, rs), dtype=float)
    Lr_nn = np.zeros((data_n, rs), dtype=float)
    fsr_nn = np.zeros((data_n, rs), dtype=float)
    bookFr = np.zeros((data_n, rs), dtype=float)
    bookFr_nn = np.zeros((data_n, rs), dtype=float)
    Br_nn = np.zeros((data_n, rs), dtype=float)
    mdefr_nn = np.zeros((data_n, rs), dtype=float)

    if max_workers is None:
        max_workers = max(1, (os.cpu_count() or 2) - 1)

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = [
            ex.submit(_interp_rate_slice, a, xEt, xsigEt, xsig, xLt, xBt, xmdef, xfs, xF, qpoints)
            for a in range(rs)
        ]
        for fut in as_completed(futures):
            a, interp, nn_L, nn_fs, nn_bookF, nn_B, nn_mdef = fut.result()
            Lr[:, a] = interp[:, 0]
            Br[:, a] = interp[:, 1]
            mdefr[:, a] = interp[:, 2]
            fsr[:, a] = interp[:, 3]
            Lr_nn[:, a] = nn_L
            fsr_nn[:, a] = nn_fs
            bookFr[:, a] = interp[:, 4]
            bookFr_nn[:, a] = nn_bookF
            Br_nn[:, a] = nn_B
            mdefr_nn[:, a] = nn_mdef

    rate_grid = xr[0, 0, :, 0].reshape(-1)
    W = _triangular_rate_weights(input_df["r"].to_numpy(), rate_grid)

    L = _weighted_nansum_or_nan(Lr, W)
    fs = _weighted_nansum_or_nan(fsr, W)
    B = _weighted_nansum_or_nan(Br, W)
    mdef = _weighted_nansum_or_nan(mdefr, W)
    bookF = _weighted_nansum_or_nan(bookFr, W)

    # If support is zero, use nearest-rate NN fallback.
    rvals = input_df["r"].to_numpy()

    L_no_support = ~np.isfinite(L)
    L_fallback_used = L_no_support.copy()
    if np.any(L_no_support):
        nn_rate_L = _nearest_rate_fallback(Lr_nn, rvals, rate_grid)
        L[L_no_support] = nn_rate_L[L_no_support]

    fs_no_support = ~np.isfinite(fs)
    fs_fallback_used = fs_no_support.copy()
    if np.any(fs_no_support):
        nn_rate_fs = _nearest_rate_fallback(fsr_nn, rvals, rate_grid)
        fs[fs_no_support] = nn_rate_fs[fs_no_support]

    B_no_support = ~np.isfinite(B)
    B_fallback_used = B_no_support.copy()
    if np.any(B_no_support):
        nn_rate_B = _nearest_rate_fallback(Br_nn, rvals, rate_grid)
        B[B_no_support] = nn_rate_B[B_no_support]

    bookf_no_support = ~np.isfinite(bookF)
    bookf_fallback_used = bookf_no_support.copy()
    if np.any(bookf_no_support):
        nn_rate_bookF = _nearest_rate_fallback(bookFr_nn, rvals, rate_grid)
        bookF[bookf_no_support] = nn_rate_bookF[bookf_no_support]

    # mdef: apply NN fallback the same way as L/B/fs/bookF. Authors' MATLAB
    # scatteredInterpolant uses linear extrapolation outside the convex hull,
    # which we approximate with k=4 inverse-distance-weighted NN at each rate
    # slice, then the nearest-rate fallback handles any remaining gaps.
    mdef_no_support = ~np.isfinite(mdef)
    mdef_fallback_used = mdef_no_support.copy()
    if np.any(mdef_no_support):
        nn_rate_mdef = _nearest_rate_fallback(mdefr_nn, rvals, rate_grid)
        mdef[mdef_no_support] = nn_rate_mdef[mdef_no_support]
    mdef[~np.isfinite(mdef)] = np.nan
    # mdef must remain a probability after extrapolation.
    mdef = np.clip(mdef, 0.0, 1.0)

    out = pd.DataFrame(
        {
            "L": L,
            "B": B,
            "mdef": mdef,
            "fs": fs,
            "E": E,
            "bookF": bookF,
            "r": input_df["r"].to_numpy(),
            "permco": input_df["permco"].to_numpy(),
            "year": input_df["year"].to_numpy(),
            "month": input_df["month"].to_numpy(),
            "sE": sE,
            "vol": vol,
            "L_fallback_used": L_fallback_used.astype(int),
            "fs_fallback_used": fs_fallback_used.astype(int),
            "B_fallback_used": B_fallback_used.astype(int),
            "bookF_fallback_used": bookf_fallback_used.astype(int),
            "mdef_fallback_used": mdef_fallback_used.astype(int),
        }
    )
    return out


def _pd_from_row(i: int, E: float, r: float, sE: float, T: float, gamma: float):
    try:
        res = merton_pd_from_paper(E=float(E), r=float(r), sE=float(sE), T=T, gamma=gamma)
        return i, float(res.PD)
    except Exception:
        return i, np.nan


def compute_merton_dtd(
    *,
    input_csv_path: Union[str, Path],
    value_surface_path: Union[str, Path],
    vol_value: float = 0.2,
    T_pd: float = 5.0,
    gamma_pd: float = 0.002,
    max_workers: Optional[int] = None,
    preserve_columns: Optional[list[str]] = None,
) -> pd.DataFrame:
    input_csv_path = Path(input_csv_path)
    value_surface_path = Path(value_surface_path)

    if not input_csv_path.exists():
        raise FileNotFoundError(f"Input CSV not found: {input_csv_path}")
    if not value_surface_path.exists():
        raise FileNotFoundError(f"ValueSurface file not found: {value_surface_path}")

    preserve_columns = preserve_columns or []
    input_df, preserved_df = _load_input(input_csv_path, preserve_columns)

    results_df = _run_from_value_surface_fast_parallel(input_df, value_surface_path, vol_value, max_workers)

    rows = list(
        zip(
            range(len(results_df)),
            results_df["E"].to_numpy(),
            results_df["r"].to_numpy(),
            results_df["sE"].to_numpy(),
        )
    )
    pd_vals = np.full(len(rows), np.nan, dtype=float)

    if max_workers is None:
        max_workers = max(1, (os.cpu_count() or 2) - 1)

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = [ex.submit(_pd_from_row, i, E, r, sE, T_pd, gamma_pd) for i, E, r, sE in rows]
        for fut in as_completed(futures):
            i, pdv = fut.result()
            pd_vals[i] = pdv

    results_df = results_df.copy()
    results_df["merton_PD"] = pd_vals

    final_df = pd.concat([preserved_df.reset_index(drop=True), results_df.reset_index(drop=True)], axis=1)
    return final_df

