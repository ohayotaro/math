from fractions import Fraction
from itertools import combinations, permutations
from random import Random

import pytest

from spectra.topology import (
    degrees,
    make_topology,
    positive_pivots,
    resilience_certificate,
    spectral_certificate,
    switch_step,
    validate_graph,
)


def determinant(matrix):
    total = 0
    for perm in permutations(range(len(matrix))):
        inversions = sum(perm[i] > perm[j] for i in range(len(perm)) for j in range(i + 1, len(perm)))
        product = (-1) ** inversions
        for i, j in enumerate(perm):
            product *= matrix[i][j]
        total += product
    return total


def test_exact_ldlt_against_independent_determinants():
    matrix = [[6, 2, 1], [2, 5, 2], [1, 2, 4]]
    previous, product = 1, Fraction(1)
    for k, pivot in enumerate(positive_pivots(matrix), 1):
        minor = determinant([row[:k] for row in matrix[:k]])
        assert pivot == Fraction(minor, previous)
        product *= pivot
        assert product == minor
        previous = minor
    for bad in ([[1, 1], [1, 1]], [[1, 2], [2, 1]], [[0, 1], [1, 0]]):
        with pytest.raises(ValueError):
            positive_pivots(bad)


def test_known_graphs_strict_spectral_boundary():
    complete = list(combinations(range(4), 2))
    assert spectral_certificate(4, complete)["strict_ramanujan"]
    bipartite = [(i, j) for i in range(3) for j in range(3, 6)]
    disconnected = complete + list(combinations(range(4, 8), 2))
    for n, graph in ((6, bipartite), (8, disconnected)):
        with pytest.raises(ValueError, match="certificate failed"):
            spectral_certificate(n, graph)
    with pytest.raises(ValueError, match="regular"):
        spectral_certificate(4, complete[:-1])


class ProposalRandom(Random):
    def __init__(self, i, j):
        self.choices = iter([1, i, (j - i - 1) % 3])

    def randrange(self, *args, **kwargs):
        return next(self.choices)

    def sample(self, population, k, **kwargs):
        return [0, 1, 2, 3]


def test_four_vertex_kernel_exact_transition_probabilities():
    states = [frozenset(p) for p in (
        ((0, 1), (2, 3)), ((0, 2), (1, 3)), ((0, 3), (1, 2)),
    )]
    for graph in states:
        probabilities = {state: Fraction(int(state == graph), 2) for state in states}
        for i, j in permutations(range(3), 2):
            result = switch_step(4, graph, ProposalRandom(i, j))
            probabilities[result] += Fraction(1, 12)
        assert probabilities[graph] == Fraction(5, 6)
        assert all(value == Fraction(1, 12) for state, value in probabilities.items() if state != graph)
        assert sum(probabilities.values()) == 1


def test_switches_preserve_degrees_without_connectedness_restriction():
    rng = Random(131)
    graph = validate_graph(8, [(i, i + 4) for i in range(4)])
    before = degrees(8, graph)
    original = graph
    for _ in range(2000):
        graph = switch_step(8, graph, rng)
        assert degrees(8, graph) == before
    assert graph != original


def test_generated_graph_is_certified_and_reproducible():
    generated = make_topology()
    assert generated == make_topology()
    assert generated["spectral"] == spectral_certificate(8, generated["edges"])
    assert generated["resilience"] == resilience_certificate(8, generated["edges"], 2)
    assert generated["resilience"]["subsets_checked"] == 37
    assert generated["search"]["uniform_sample"] is False
    assert generated["search"]["claimed_quarter_tv_bound_steps"] == 33554432


def test_invalid_inputs_and_failed_search_fail_closed():
    for n, edges in [(True, []), (1000000, []), (4, [(0, 1), (0, 1)]), (4, [(0, 4)]), (4, [(1, 1)]), (4, [(False, 1)])]:
        with pytest.raises(ValueError):
            validate_graph(n, edges)
    with pytest.raises(ValueError):
        make_topology(n=1000000)
    with pytest.raises(ValueError, match="exhausted"):
        make_topology(n=32, degree=4, attempts=1, steps=1, seed=0)
