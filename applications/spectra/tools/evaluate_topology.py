"""Deterministic finite graph experiment, not a throughput or security benchmark."""

import argparse
import json
from collections import deque
from fractions import Fraction
from itertools import combinations, permutations
from pathlib import Path

from spectra.topology import make_topology, spectral_certificate

OUT = Path(__file__).resolve().parents[1] / "research" / "topology-results.json"


def distances(n, edges):
    neighbors = [set() for _ in range(n)]
    for a, b in edges:
        neighbors[a].add(b)
        neighbors[b].add(a)
    lengths = []
    for source in range(n):
        found = {source: 0}
        queue = deque([source])
        while queue:
            node = queue.popleft()
            for peer in neighbors[node] - found.keys():
                found[peer] = found[node] + 1
                queue.append(peer)
        if len(found) != n:
            raise ValueError("disconnected evaluation graph")
        lengths.extend(found[target] for target in range(source + 1, n))
    return {"diameter": max(lengths), "mean_pair_hops": str(Fraction(sum(lengths), len(lengths)))}


def independent_default_certificate(edges, reported_pivots):
    # Leibniz determinants on every leading minor: independent of LDL elimination.
    n = 8
    adjacency = [[int(tuple(sorted((i, j))) in {tuple(e) for e in edges}) for j in range(n)] for i in range(n)]
    square = [[sum(adjacency[i][k] * adjacency[k][j] for k in range(n)) for j in range(n)] for i in range(n)]
    form = [[12 * int(i == j) - square[i][j] for j in range(n)] for i in range(n)]
    gram = [[form[i][j] - form[i][-1] - form[-1][j] + form[-1][-1] for j in range(n - 1)] for i in range(n - 1)]
    minors, previous = [], 1
    for size in range(1, n):
        total = 0
        for perm in permutations(range(size)):
            inversions = sum(perm[i] > perm[j] for i in range(size) for j in range(i + 1, size))
            value = (-1) ** inversions
            for i, j in enumerate(perm):
                value *= gram[i][j]
            total += value
        if total <= 0 or Fraction(total, previous) != Fraction(reported_pivots[size - 1]):
            raise ValueError("independent determinant check failed")
        minors.append(total)
        previous = total
    return minors


def evaluate():
    rows = []
    for n in (8, 16, 32):
        result = make_topology(n=n, degree=4, seed=131178, attempts=100, steps=1000)
        full = list(combinations(range(n), 2))
        ring = sorted({tuple(sorted((i, (i + offset) % n))) for i in range(n) for offset in (1, 2)})
        try:
            spectral_certificate(n, ring)
            ring_passes = True
        except ValueError:
            ring_passes = False
        rows.append({
            "n": n, "selected": result, "selected_hops": distances(n, result["edges"]),
            "complete": {"edges": len(full), **distances(n, full)},
            "degree_four_ring": {"edges": len(ring), "strict_certificate_passes": ring_passes, **distances(n, ring)},
            "edge_reduction_vs_complete": str(Fraction(len(full) - len(result["edges"]), len(full))),
        })
    minors = independent_default_certificate(rows[0]["selected"]["edges"], rows[0]["selected"]["spectral"]["ldlt_pivots"])
    return {"scope": "Three fixed graphs; no random-population, real-network throughput, or Byzantine security claim.",
            "default_independent_leading_minors": minors, "results": rows}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    result = evaluate()
    encoded = json.dumps(result, indent=2) + "\n"
    if args.check:
        if OUT.read_text() != encoded:
            raise SystemExit("finite evaluation differs from recorded result")
    else:
        OUT.write_text(encoded)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
