"""Small-sample statistics, hand-rolled.

The project's dependencies are pandas / yfinance / requests / lxml /
matplotlib. Adding scipy for five functions would be the largest dependency in
the tree, so these are implemented directly. Each is pinned by
`tests/test_portfolio_sim.py` against a hand-computed value.

Two deliberate choices, both because signal returns are fat-tailed and the
sample is thin:

* **Mann-Whitney U is the primary two-group test.** A rank test does not
  assume normality and a single +40% winner cannot carry a cohort the way it
  carries a t-test.
* **p-values come from normal approximations.** For a rank test that is the
  standard treatment; for Welch's t it is an approximation that overstates
  significance at very small n. That is acceptable *only* because every
  finding also carries `sufficient_n`, and because significance is finally
  keyed off the Benjamini-Hochberg `q_value` rather than a raw p.
"""

import math

import numpy as np
import pandas as pd


def _clean(values) -> np.ndarray:
    arr = pd.Series(list(values), dtype="float64").to_numpy()
    return arr[np.isfinite(arr)]


def normal_sf(z: float) -> float:
    """Upper-tail probability of the standard normal."""
    return 0.5 * math.erfc(z / math.sqrt(2))


def two_sided_p(z: float) -> float:
    if not np.isfinite(z):
        return float("nan")
    return min(1.0, 2 * normal_sf(abs(z)))


def mann_whitney(a, b) -> dict:
    """Two-sided Mann-Whitney U with tie correction, normal approximation.

    Returns `u`, `z`, `p` and `prob_superior` -- P(a random draw from `a`
    exceeds one from `b`), which is the effect size worth reporting: 0.5 is no
    difference, 0.7 means the cohort beats the other seven times in ten. It is
    scale-free, so it is comparable across horizons in a way a difference in
    mean return is not.
    """
    a, b = _clean(a), _clean(b)
    na, nb = len(a), len(b)
    out = {"n_a": na, "n_b": nb, "u": float("nan"), "z": float("nan"),
           "p": float("nan"), "prob_superior": float("nan")}
    if na < 1 or nb < 1:
        return out

    combined = np.concatenate([a, b])
    ranks = pd.Series(combined).rank(method="average").to_numpy()
    ua = ranks[:na].sum() - na * (na + 1) / 2
    out["u"] = float(ua)
    out["prob_superior"] = float(ua / (na * nb))

    _, counts = np.unique(combined, return_counts=True)
    ties = float(((counts ** 3) - counts).sum())
    n = na + nb
    var = (na * nb / 12.0) * ((n + 1) - ties / (n * (n - 1))) if n > 1 else 0.0
    if var <= 0:
        return out
    # Continuity correction: U is discrete, the normal it is compared to is not.
    z = (ua - na * nb / 2.0)
    z = (z - math.copysign(0.5, z)) / math.sqrt(var) if z != 0 else 0.0
    out["z"], out["p"] = float(z), float(two_sided_p(z))
    return out


def welch_t(a, b) -> dict:
    """Welch's t on the difference in means (normal-approximated p)."""
    a, b = _clean(a), _clean(b)
    out = {"diff": float("nan"), "t": float("nan"), "p": float("nan"),
           "df": float("nan")}
    if len(a) < 2 or len(b) < 2:
        if len(a) and len(b):
            out["diff"] = float(a.mean() - b.mean())
        return out
    va, vb = a.var(ddof=1) / len(a), b.var(ddof=1) / len(b)
    out["diff"] = float(a.mean() - b.mean())
    if va + vb <= 0:
        return out
    t = out["diff"] / math.sqrt(va + vb)
    df = (va + vb) ** 2 / (va ** 2 / (len(a) - 1) + vb ** 2 / (len(b) - 1))
    out["t"], out["df"], out["p"] = float(t), float(df), float(two_sided_p(t))
    return out


def spearman(x, y) -> dict:
    """Rank correlation and its Fisher-z p-value, over the pairs where both
    values are present."""
    frame = pd.DataFrame({"x": pd.to_numeric(pd.Series(list(x)), errors="coerce"),
                          "y": pd.to_numeric(pd.Series(list(y)), errors="coerce")}).dropna()
    out = {"n": len(frame), "rho": float("nan"), "p": float("nan")}
    if len(frame) < 3 or frame["x"].nunique() < 2 or frame["y"].nunique() < 2:
        return out
    # Pearson on ranks *is* Spearman, and pandas' `.rank()` uses average ranks,
    # which is exactly the tie correction. Spelled out this way because
    # `.corr(method="spearman")` reaches for `scipy.stats.spearmanr`, and this
    # module hand-rolls its statistics precisely so scipy stays out of the
    # dependency list.
    rho = frame["x"].rank().corr(frame["y"].rank())
    out["rho"] = float(rho)
    if abs(rho) >= 1.0:
        out["p"] = 0.0
        return out
    z = math.atanh(rho) * math.sqrt((len(frame) - 3) / 1.06)
    out["p"] = float(two_sided_p(z))
    return out


def bootstrap_diff_ci(a, b, iters: int = 2000, alpha: float = 0.05,
                      seed: int = 12345) -> tuple[float, float]:
    """Percentile CI for `mean(a) - mean(b)`, resampled with replacement.

    The honest interval when n is small and the distribution is skewed -- and
    the one number in a finding row that makes "we cannot tell yet" visible
    rather than implied: a CI spanning zero says so at a glance.
    """
    a, b = _clean(a), _clean(b)
    if len(a) < 2 or len(b) < 2:
        return float("nan"), float("nan")
    rng = np.random.default_rng(seed)
    draws = (rng.choice(a, (iters, len(a)), replace=True).mean(axis=1)
             - rng.choice(b, (iters, len(b)), replace=True).mean(axis=1))
    return (float(np.quantile(draws, alpha / 2)),
            float(np.quantile(draws, 1 - alpha / 2)))


def benjamini_hochberg(p_values) -> list[float]:
    """BH-adjusted q-values, aligned to the input order (NaNs stay NaN).

    A findings file runs dozens of tests over one thin sample, where the
    largest raw effect is usually the luckiest rather than the truest. Keying
    `significant` off q instead of p is what stops this report confidently
    naming a noise cohort as the thing that drives returns.
    """
    values = list(p_values)
    idx = [i for i, p in enumerate(values) if p is not None and np.isfinite(p)]
    out = [float("nan")] * len(values)
    m = len(idx)
    if not m:
        return out
    order = sorted(idx, key=lambda i: values[i])
    running = 1.0
    for rank, i in enumerate(reversed(order), start=1):
        q = values[i] * m / (m - rank + 1)
        running = min(running, q)
        out[i] = float(min(1.0, running))
    return out
