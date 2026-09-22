"""Sock Shop service dependency graph (config/dependency_graph.yaml) and traversal.

Edges point from caller to callee. Walking them forward (downstream) gives root-cause
candidates: front-end looking slow is usually something it calls being slow. Walking them
backward (upstream) gives blast radius: who is affected, and which other anomalies are likely
symptoms of the same cause. Plain dicts and breadth-first search; a graph library is not
warranted for 14 nodes.
"""

from collections import deque
from collections.abc import Iterable, Mapping
from pathlib import Path

import yaml

DEFAULT_GRAPH_PATH = Path(__file__).resolve().parent.parent / "config" / "dependency_graph.yaml"
DEFAULT_MAX_HOPS = 3
NODE_KINDS = ("gateway", "service", "datastore", "broker")


class UnknownService(ValueError):
    """A service name that is not a node in the dependency graph."""


class DependencyGraph:
    def __init__(self, kinds: Mapping[str, str], edges: Iterable[tuple[str, str]]):
        for name, kind in kinds.items():
            if kind not in NODE_KINDS:
                raise ValueError(f"node {name!r} has kind {kind!r}; expected one of {NODE_KINDS}")
        self._kinds = dict(kinds)
        self._calls: dict[str, set[str]] = {node: set() for node in self._kinds}
        self._called_by: dict[str, set[str]] = {node: set() for node in self._kinds}

        for caller, callee in edges:
            for node in (caller, callee):
                if node not in self._kinds:
                    raise ValueError(f"edge {caller} -> {callee}: {node!r} is not a declared node")
            if caller == callee:
                raise ValueError(f"edge {caller} -> {callee} is a self-loop")
            if callee in self._calls[caller]:
                raise ValueError(f"edge {caller} -> {callee} is a duplicate")
            self._calls[caller].add(callee)
            self._called_by[callee].add(caller)

    @property
    def nodes(self) -> frozenset[str]:
        return frozenset(self._kinds)

    @property
    def edges(self) -> frozenset[tuple[str, str]]:
        return frozenset((caller, callee) for caller, callees in self._calls.items() for callee in callees)

    def __contains__(self, service: object) -> bool:
        return service in self._kinds

    def kind(self, service: str) -> str:
        self._require(service)
        return self._kinds[service]

    def downstream(self, service: str, max_hops: int = DEFAULT_MAX_HOPS) -> dict[str, int]:
        """Services `service` calls, directly or transitively, mapped to hop count."""
        return self._walk(service, self._calls, max_hops)

    def upstream(self, service: str, max_hops: int = DEFAULT_MAX_HOPS) -> dict[str, int]:
        """Services that call `service`, directly or transitively, mapped to hop count."""
        return self._walk(service, self._called_by, max_hops)

    def distance(self, source: str, target: str) -> int | None:
        """Hops from `source` to `target` following calls, or None if `source` never reaches it."""
        self._require(target)
        if source == target:
            self._require(source)
            return 0
        return self._walk(source, self._calls, max_hops=None).get(target)

    def _walk(self, start: str, adjacency: dict[str, set[str]], max_hops: int | None) -> dict[str, int]:
        """Hop counts from `start`, excluding `start` itself.

        Breadth-first, so a node reachable along two paths keeps the shorter count (front-end
        calls user directly and via orders; user is 1 hop, not 2). Neighbours are visited in
        sorted order so the result's ordering is deterministic.
        """
        self._require(start)
        if max_hops is not None and max_hops < 0:
            raise ValueError(f"max_hops must be >= 0, got {max_hops}")
        hops = {start: 0}
        queue = deque([start])
        while queue:
            node = queue.popleft()
            if max_hops is not None and hops[node] >= max_hops:
                continue
            for neighbour in sorted(adjacency[node]):
                if neighbour not in hops:
                    hops[neighbour] = hops[node] + 1
                    queue.append(neighbour)
        del hops[start]
        return hops

    def _require(self, service: str) -> None:
        if service not in self._kinds:
            raise UnknownService(f"{service!r} is not in the dependency graph")


def load_graph(path: Path = DEFAULT_GRAPH_PATH) -> DependencyGraph:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or set(data) != {"nodes", "edges"}:
        raise ValueError(f"{path}: expected exactly the top-level keys 'nodes' and 'edges'")
    if not isinstance(data["nodes"], dict) or not isinstance(data["edges"], list):
        raise ValueError(f"{path}: 'nodes' must be a mapping and 'edges' a list")
    edges = []
    for edge in data["edges"]:
        if not (isinstance(edge, list) and len(edge) == 2 and all(isinstance(n, str) for n in edge)):
            raise ValueError(f"{path}: edge {edge!r} must be [caller, callee]")
        edges.append((edge[0], edge[1]))
    return DependencyGraph(data["nodes"], edges)
