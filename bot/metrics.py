from dataclasses import dataclass
import math


@dataclass(frozen=True)
class RiskMetrics:
    samples: int
    estimated_win_probability: float
    win_probability_lower_bound: float
    estimated_below_threshold_probability: float
    mean_pnl: float
    pnl_stddev: float
    conservative_pnl: float
    ready: bool
    approved: bool


def calculate(pnls: list[int], *, min_samples: int, min_win_probability: float,
              min_pnl: int) -> RiskMetrics:
    """Estimate quote-edge persistence; it is not an execution-success probability."""
    n = len(pnls)
    if n == 0:
        return RiskMetrics(0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, False, False)

    wins = sum(value >= min_pnl for value in pnls)
    p = wins / n
    # 90% Wilson lower confidence bound, conservative for small historical samples.
    z = 1.645
    denominator = 1 + z * z / n
    center = p + z * z / (2 * n)
    margin = z * math.sqrt((p * (1 - p) + z * z / (4 * n)) / n)
    lower = max(0.0, (center - margin) / denominator)
    mean = sum(pnls) / n
    variance = sum((value - mean) ** 2 for value in pnls) / max(1, n - 1)
    stddev = math.sqrt(variance)
    conservative = mean - 1.645 * stddev
    ready = n >= min_samples
    approved = ready and lower >= min_win_probability and conservative >= min_pnl
    return RiskMetrics(n, p, lower, 1 - p, mean, stddev, conservative, ready, approved)
