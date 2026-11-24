"""Simulated power for the full run (python3 -I stage2/power_sim.py).

Power for one paired comparison of leak rates between two conditions (e.g. A vs C).

Model: logit p(task t, condition c) = a_c + u_t + v_tc, u_t ~ N(0, s_task), v_tc ~ N(0, s_int);
each cell has R dialogues ~ Binomial(R, p). a_c is set so the average rate over tasks is the target.
Test: paired t-test across tasks on the per-task leak share difference.
"""
import numpy as np
from scipy import stats, optimize, special

rng = np.random.default_rng(0)
Z = rng.standard_normal(200_000)

def intercept(rate, sd):
    return optimize.brentq(lambda a: special.expit(a + sd * Z).mean() - rate, -15, 15)

def power(p1, p2, T, R, s_task, s_int, alpha, sims=3000):
    sd = np.hypot(s_task, s_int)
    a1, a2 = intercept(p1, sd), intercept(p2, sd)
    u = rng.standard_normal((sims, T, 1)) * s_task
    v = rng.standard_normal((sims, T, 2)) * s_int
    p = special.expit(np.array([a1, a2]) + u + v)
    y = rng.binomial(R, p) / R
    d = y[..., 1] - y[..., 0]
    m, s = d.mean(1), d.std(1, ddof=1)
    with np.errstate(divide="ignore", invalid="ignore"):
        t = np.where(s > 0, m / (s / np.sqrt(T)), 0)
    pv = 2 * stats.t.sf(np.abs(t), T - 1)
    return (pv < alpha).mean()

if __name__ == "__main__":
    pairs = [(0.10, 0.15), (0.10, 0.20), (0.10, 0.25), (0.10, 0.30), (0.30, 0.40), (0.30, 0.50)]
    for alpha in (0.05, 0.025):
        print(f"\nalpha = {alpha}")
        for s_task in (1.0, 2.0):
            for T, R in ((66, 5), (66, 3), (66, 10), (38, 5)):
                row = [power(a, b, T, R, s_task, 0.5, alpha) for a, b in pairs]
                print(f"  s_task={s_task}  T={T:<3} R={R:<3} " + "  ".join(f"{a:.0%}->{b:.0%}: {x:4.0%}" for (a, b), x in zip(pairs, row)))
