"""Power of the pre-registered paired test at the gate's size (16 tasks x 2 repeats per arm).

Test: exact two-sided sign-flip permutation test on the 16 per-task differences
d_i = mean(D runs of task i) - mean(S runs of task i), statistic = sum(d), alpha 0.05.
Model: each run passes independently with its task's probability; D adds `delta` to every task's
probability (capped at 1). "hetero" draws each task's baseline from a Beta distribution with that mean.

    python -m evals.gate.power        # about two minutes; seed fixed, so the table is reproducible
"""
import random

N_TASKS, REPEATS, ALPHA, SIMS = 16, 2, 0.05, 4000


def p_value(d):
    """Exact sign-flip p-value by dynamic programming over integer sums (d in halves)."""
    ints = [round(2 * x) for x in d if x != 0]
    if not ints:
        return 1.0
    obs = abs(sum(ints))
    dist = {0: 1}
    for v in ints:
        nxt = {}
        for s, c in dist.items():
            nxt[s + v] = nxt.get(s + v, 0) + c
            nxt[s - v] = nxt.get(s - v, 0) + c
        dist = nxt
    total = 2 ** len(ints)
    return sum(c for s, c in dist.items() if abs(s) >= obs) / total


def run(task_ps, delta, rng):
    d = []
    for p in task_ps:
        q = min(1.0, max(0.0, p + delta))
        s = sum(rng.random() < p for _ in range(REPEATS)) / REPEATS
        dd = sum(rng.random() < q for _ in range(REPEATS)) / REPEATS
        d.append(dd - s)
    return d


def power(base, delta, hetero, rng):
    hits = 0
    for _ in range(SIMS):
        if hetero:   # tasks differ: baseline drawn from Beta with mean `base`
            ps = [rng.betavariate(2 * base / (1 - base) if base < 1 else 2, 2) for _ in range(N_TASKS)]
        else:
            ps = [base] * N_TASKS
        if p_value(run(ps, delta, rng)) < ALPHA:
            hits += 1
    return hits / SIMS


if __name__ == "__main__":
    rng = random.Random(20261126)
    print("base  hetero  " + "  ".join(f"d={d:.2f}" for d in (0.15, 0.20, 0.25, 0.30, 0.35, 0.40)))
    for base in (0.2, 0.3, 0.5):
        for hetero in (False, True):
            row = [power(base, d, hetero, rng) for d in (0.15, 0.20, 0.25, 0.30, 0.35, 0.40)]
            print(f"{base:.1f}   {str(hetero):6}  " + "  ".join(f"{x:6.2f}" for x in row))
