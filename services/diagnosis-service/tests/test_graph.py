"""Phase 3: the dependency graph is transcribed from the contract and traverses correctly.
No database, no LLM."""

import pytest

from app.fixtures import load_fixtures
from app.graph import DependencyGraph, UnknownService, load_graph

GRAPH = load_graph()

# CONTRACTS.md, "Service dependency graph", as [caller, callee]. Duplicated here on purpose:
# an edit to config/dependency_graph.yaml that drifts from the contract must fail a test.
CONTRACT_EDGES = {
    ("edge-router", "front-end"),
    ("front-end", "catalogue"),
    ("front-end", "carts"),
    ("front-end", "orders"),
    ("front-end", "user"),
    ("catalogue", "catalogue-db"),
    ("carts", "carts-db"),
    ("orders", "orders-db"),
    ("orders", "payment"),
    ("orders", "shipping"),
    ("orders", "user"),
    ("user", "user-db"),
    ("shipping", "rabbitmq"),
    ("queue-master", "rabbitmq"),
}


def test_yaml_matches_the_contract():
    assert GRAPH.edges == CONTRACT_EDGES
    assert GRAPH.nodes == {node for edge in CONTRACT_EDGES for node in edge}


# ------------------------------------------------- definition of done (PLAN.md)


def test_front_end_reaches_catalogue_db_in_two_hops():
    assert GRAPH.downstream("front-end")["catalogue-db"] == 2


def test_edge_router_is_three_hops_upstream_of_catalogue_db():
    assert GRAPH.upstream("catalogue-db")["edge-router"] == 3


def test_rabbitmq_is_not_reachable_from_catalogue():
    assert "rabbitmq" not in GRAPH.downstream("catalogue", max_hops=len(GRAPH.nodes))
    assert GRAPH.distance("catalogue", "rabbitmq") is None


# ------------------------------------------------------------------ traversal


def test_downstream_of_front_end_within_three_hops():
    assert GRAPH.downstream("front-end") == {
        "carts": 1, "catalogue": 1, "orders": 1, "user": 1,
        "carts-db": 2, "catalogue-db": 2, "orders-db": 2, "payment": 2, "shipping": 2, "user-db": 2,
        "rabbitmq": 3,
    }


def test_shortest_path_wins():
    # front-end calls user directly and also via orders.
    assert GRAPH.downstream("front-end")["user"] == 1
    assert GRAPH.distance("front-end", "user") == 1


def test_max_hops_limits_the_walk():
    assert GRAPH.downstream("front-end", max_hops=1) == {"carts": 1, "catalogue": 1, "orders": 1, "user": 1}
    assert GRAPH.downstream("front-end", max_hops=0) == {}
    # edge-router is 4 hops above rabbitmq, one past the default.
    assert "edge-router" not in GRAPH.upstream("rabbitmq")
    assert GRAPH.upstream("rabbitmq", max_hops=4)["edge-router"] == 4


def test_negative_max_hops_is_rejected():
    with pytest.raises(ValueError, match="max_hops"):
        GRAPH.downstream("front-end", max_hops=-1)


def test_upstream_is_blast_radius():
    assert GRAPH.upstream("user") == {"front-end": 1, "orders": 1, "edge-router": 2}


def test_upstream_crosses_the_async_path():
    assert GRAPH.upstream("rabbitmq") == {"queue-master": 1, "shipping": 1, "orders": 2, "front-end": 3}


def test_distance_is_directed():
    assert GRAPH.distance("catalogue", "front-end") is None
    assert GRAPH.distance("edge-router", "rabbitmq") == 4
    assert GRAPH.distance("user", "user") == 0


def test_leaves_and_roots():
    assert GRAPH.downstream("payment") == {}
    assert GRAPH.upstream("edge-router") == {}


def test_traversal_order_is_deterministic():
    assert list(GRAPH.downstream("orders")) == ["orders-db", "payment", "shipping", "user", "rabbitmq", "user-db"]


@pytest.mark.parametrize(
    "call",
    [
        lambda g: g.downstream("checkout"),
        lambda g: g.upstream("checkout"),
        lambda g: g.distance("checkout", "user"),
        lambda g: g.distance("user", "checkout"),
        lambda g: g.distance("checkout", "checkout"),
        lambda g: g.kind("checkout"),
    ],
)
def test_unknown_service_raises(call):
    with pytest.raises(UnknownService):
        call(GRAPH)


def test_node_kinds():
    assert GRAPH.kind("edge-router") == "gateway"
    assert GRAPH.kind("front-end") == "service"
    assert GRAPH.kind("catalogue-db") == "datastore"
    assert GRAPH.kind("rabbitmq") == "broker"


def test_every_service_with_metrics_is_a_node():
    # The seven services metrics-bridge scraped, and M2 raised anomalies on, as of 2026-09-13.
    observed = {"carts", "catalogue", "front-end", "orders", "payment", "shipping", "user"}
    assert observed <= GRAPH.nodes
    assert "front-end" in GRAPH and "checkout" not in GRAPH


# ------------------------------------------------------------------ validation


@pytest.mark.parametrize(
    "kinds, edges, message",
    [
        ({"a": "service"}, [("a", "b")], "not a declared node"),
        ({"a": "service"}, [("a", "a")], "self-loop"),
        ({"a": "service", "b": "service"}, [("a", "b"), ("a", "b")], "duplicate"),
        ({"a": "database"}, [], "kind"),
    ],
)
def test_invalid_graphs_are_rejected(kinds, edges, message):
    with pytest.raises(ValueError, match=message):
        DependencyGraph(kinds, edges)


@pytest.mark.parametrize(
    "text, message",
    [
        ("nodes: {a: service}\nedges:\n  - [a]\n", r"\[caller, callee\]"),
        ("nodes: {a: service}\n", "top-level"),
        ("nodes: [a]\nedges: []\n", "mapping"),
    ],
)
def test_load_graph_rejects_malformed_yaml(tmp_path, text, message):
    path = tmp_path / "graph.yaml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError, match=message):
        load_graph(path)


# ------------------------------------------------------------------ fixtures

FIXTURES_WITH_TRUTH = [f for f in load_fixtures() if f.meta.ground_truth_service is not None]


@pytest.mark.parametrize("fixture", FIXTURES_WITH_TRUTH, ids=[f.event.anomaly_id for f in FIXTURES_WITH_TRUTH])
def test_fixture_root_cause_is_a_candidate(fixture):
    """Phase 4 scores only the anomalous services and what they call within the default hop
    limit. A true cause outside that set could never be ranked first, whatever the weights."""
    candidates = set(fixture.event.services)
    for service in fixture.event.services:
        candidates |= set(GRAPH.downstream(service))
    assert fixture.meta.ground_truth_service in candidates
