import math
from dataclasses import dataclass
from typing import Optional

import numpy as np

try:
    from scipy.optimize import fsolve
    from scipy.stats import norm
except ImportError as e:
    raise ImportError("This function requires scipy. Install with: pip install scipy") from e


@dataclass
class MertonResult:
    V: float          # implied asset value
    sigma_v: float    # implied asset volatility
    DD: float         # distance to default (risk-neutral, per paper)
    PD: float         # EDF / risk-neutral default probability


def merton_pd_from_paper(
    E: float,
    r: float,
    sE: float,
    T: float = 5.0,
    gamma: float = 0.002,
    D: Optional[float] = None,
) -> MertonResult:
    """
    Computes the paper's standard Merton-model implied default probability (EDF_BSM)
    by solving Appendix A.1 equations for (V, sigma_v), then computing EDF.
    """
    tau = float(T)
    if tau <= 0:
        raise ValueError("T must be > 0")

    if D is None:
        D = math.exp(r * tau)
    D = float(D)

    if E <= 0 or sE <= 0 or D <= 0:
        raise ValueError("E, sE, and D must be positive")

    sqrt_tau = math.sqrt(tau)
    exp_minus_gamma_tau = math.exp(-gamma * tau)
    exp_minus_r_tau = math.exp(-r * tau)

    def equations(x: np.ndarray) -> np.ndarray:
        V, sigma_v = float(x[0]), float(x[1])
        if V <= 1e-12 or sigma_v <= 1e-12:
            return np.array([1e6, 1e6], dtype=float)

        d1 = (math.log(V / D) + (r - gamma + 0.5 * sigma_v * sigma_v) * tau) / (sigma_v * sqrt_tau)
        d2 = d1 - sigma_v * sqrt_tau

        Nd1 = norm.cdf(d1)
        Nd2 = norm.cdf(d2)

        C = V * exp_minus_gamma_tau * Nd1 - D * exp_minus_r_tau * Nd2
        S_model = C + (1.0 - exp_minus_gamma_tau) * V

        leverage_term = exp_minus_gamma_tau * Nd1 + (1.0 - exp_minus_gamma_tau)
        sE_model = (V * leverage_term / S_model) * sigma_v

        return np.array([S_model - E, sE_model - sE], dtype=float)

    V0 = E + D * exp_minus_r_tau
    sigma_v0 = min(max(0.05, sE * E / max(V0, 1e-8)), 2.0)

    sol, info, ier, msg = fsolve(equations, x0=np.array([V0, sigma_v0]), full_output=True, xtol=1e-12)
    if ier != 1:
        raise RuntimeError(f"Root solve failed: {msg}")

    V_hat, sigma_v_hat = float(sol[0]), float(sol[1])

    DD = (math.log(V_hat / D) + (r - gamma - 0.5 * sigma_v_hat * sigma_v_hat) * tau) / (sigma_v_hat * sqrt_tau)
    PD = float(norm.cdf(-DD))

    return MertonResult(V=V_hat, sigma_v=sigma_v_hat, DD=DD, PD=PD)

