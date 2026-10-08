"""Finite, exact graph checks; no asymptotic theorem is trusted at runtime."""

from fractions import Fraction
from itertools import combinations
from random import Random
from typing import Any, Dict, Iterable, Set

Edge = tuple[int, int]


def validate_graph(n: int, edges: Iterable[Edge]) -> frozenset[Edge]:
    if type(n) is not int or not 4 <= n <= 32:
        raise ValueError("research prototype supports 4..32 vertices")
    normalized = []
    for edge in edges:
        if len(edge) != 2:
            raise ValueError("an edge must have two vertices")
        a, b = edge
        if type(a) is not int or type(b) is not int or not 0 <= a < b < n:
            raise ValueError("edges must be ordered, loopless integer pairs")
        normalized.append((a, b))
    result = frozenset(normalized)
    if len(result) != len(normalized):
        raise ValueError("duplicate edge")
    return result


def degrees(n: int, edges: Iterable[Edge]) -> tuple[int, ...]:
    values = [0] * n
    for a, b in validate_graph(n, edges):
        values[a] += 1
        values[b] += 1
    return tuple(values)


def switch_step(n: int, edges: frozenset[Edge], rng: Random) -> frozenset[Edge]:
    """Family 131 kernel: 1/2 lazy, uniform 4-set, uniform ordered matchings.

    Callers validate the graph once. Disconnected intermediate graphs are NOT
    rejected: doing that would change the chain in the paper.
    """
    if rng.randrange(2) == 0:
        return edges
    a, b, c, d = sorted(rng.sample(range(n), 4))
    matchings = (
        frozenset(((a, b), (c, d))),
        frozenset(((a, c), (b, d))),
        frozenset(((a, d), (b, c))),
    )
    i = rng.randrange(3)
    j = (i + 1 + rng.randrange(2)) % 3
    remove, add = matchings[i], matchings[j]
    if remove <= edges and not (add & edges):
        return (edges - remove) | add
    return edges


def positive_pivots(matrix: list[list[int]]) -> list[Fraction]:
    """Exact LDL^T pivots. Positive pivots iff symmetric matrix is positive definite."""
    size = len(matrix)
    if any(len(row) != size for row in matrix):
        raise ValueError("square matrix required")
    if any(matrix[i][j] != matrix[j][i] for i in range(size) for j in range(size)):
        raise ValueError("symmetric matrix required")
    work = [[Fraction(x) for x in row] for row in matrix]
    pivots = []
    for k in range(size):
        pivot = work[k][k]
        if pivot <= 0:
            raise ValueError("strict spectral certificate failed")
        pivots.append(pivot)
        for i in range(k + 1, size):
            for j in range(i, size):
                value = work[i][j] - work[i][k] * work[j][k] / pivot
                work[i][j] = work[j][i] = value
    return pivots


def spectral_certificate(n: int, edges: Iterable[Edge]) -> Dict[str, Any]:
    """Family 178, algorithm.tex: B^T(4(d-1)I-A^2)B > 0, B_i=e_i-e_n.

    This tests a concrete graph, NOT the paper's full polynomial construction.
    Fractions avoid floating-point tolerance at the strict spectral boundary.
    """
    graph = validate_graph(n, edges)
    ds = degrees(n, graph)
    d = ds[0]
    if d < 3 or any(x != d for x in ds):
        raise ValueError("a regular graph of degree at least 3 is required")
    adjacency = [[0] * n for _ in range(n)]
    for a, b in graph:
        adjacency[a][b] = adjacency[b][a] = 1
    ab = [[row[i] - row[-1] for i in range(n - 1)] for row in adjacency]
    gram = [
        [4 * (d - 1) * (1 + int(i == j)) - sum(row[i] * row[j] for row in ab)
         for j in range(n - 1)]
        for i in range(n - 1)
    ]
    pivots = positive_pivots(gram)
    return {
        "n": n, "degree": d, "edges": len(graph),
        "criterion": "B^T(4(d-1)I-A^2)B strictly positive definite",
        "ldlt_pivots": [str(p) for p in pivots],
        "strict_ramanujan": True,
    }


def connected(n: int, edges: Iterable[Edge], removed: Iterable[int] = ()) -> bool:
    remaining = set(range(n)) - set(removed)
    if not remaining:
        return False
    neighbors: Dict[int, Set[int]] = {v: set() for v in remaining}
    for a, b in edges:
        if a in remaining and b in remaining:
            neighbors[a].add(b)
            neighbors[b].add(a)
    seen = {min(remaining)}
    todo = list(seen)
    while todo:
        for v in neighbors[todo.pop()] - seen:
            seen.add(v)
            todo.append(v)
    return seen == remaining


def resilience_certificate(n: int, edges: Iterable[Edge], faults: int) -> Dict[str, Any]:
    """Exhaustive finite crash-connectivity check, independent of spectral claims."""
    graph = validate_graph(n, edges)
    if type(faults) is not int or not 0 <= faults <= min(3, n - 2):
        raise ValueError("exhaustive check supports at most 3 removed nodes")
    checked = 0
    for k in range(faults + 1):
        for removed in combinations(range(n), k):
            checked += 1
            if not connected(n, graph, removed):
                raise ValueError(f"removing {removed} disconnects the graph")
    return {"removed_nodes": faults, "subsets_checked": checked, "connected": True}


def make_topology(n: int = 8, degree: int = 4, seed: int = 131178,
                  attempts: int = 100, steps: int = 1000) -> Dict[str, Any]:
    """Bounded seeded candidate search, not a uniform or secure random sampler."""
    validate_graph(n, [])
    if type(degree) is not int or not 3 <= degree < n:
        raise ValueError("degree must lie in [3,n)")
    if n % 2 or type(attempts) is not int or not 1 <= attempts <= 1000:
        raise ValueError("even n and 1..1000 attempts required")
    if type(steps) is not int or not 1 <= steps <= 100000:
        raise ValueError("1..100000 steps required")
    edges: Set[Edge] = set()
    for i in range(n):
        for offset in range(1, degree // 2 + 1):
            first, second = sorted((i, (i + offset) % n))
            edges.add((first, second))
        if degree % 2:
            first, second = sorted((i, (i + n // 2) % n))
            edges.add((first, second))
    graph = validate_graph(n, edges)
    rng = Random(seed)
    faults = min(3, (n - 1) // 3)
    for attempt in range(1, attempts + 1):
        for _ in range(steps):
            graph = switch_step(n, graph, rng)
        try:
            spectral = spectral_certificate(n, graph)
            resilience = resilience_certificate(n, graph, faults)
        except ValueError:
            continue
        return {
            "edges": [list(edge) for edge in sorted(graph)],
            "spectral": spectral, "resilience": resilience,
            "search": {"seed": seed, "attempts_used": attempt,
                       "steps_per_attempt": steps, "uniform_sample": False,
                       "claimed_quarter_tv_bound_steps": 2 * n ** 8,
                       "bound_independently_verified": False},
        }
    raise ValueError("bounded search exhausted; no unverified fallback graph accepted")
