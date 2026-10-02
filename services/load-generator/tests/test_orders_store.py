import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import orders_store  # noqa: E402


class FakeCursor:
    def __init__(self, docs):
        self._docs = docs

    def sort(self, key, direction):
        return FakeCursor(sorted(self._docs, key=lambda d: d[key], reverse=direction < 0))

    def limit(self, n):
        return FakeCursor(self._docs[:n])

    def __iter__(self):
        return iter(self._docs)


class FakeCollection:
    """Just enough of a pymongo collection for orders_store."""

    def __init__(self, ids):
        self.docs = [{"_id": i} for i in ids]

    def count_documents(self, query):
        assert query == {}
        return len(self.docs)

    def find(self, query, projection):
        return FakeCursor(list(self.docs))

    def delete_many(self, query):
        before = len(self.docs)
        if query:
            doomed = set(query["_id"]["$in"])
            self.docs = [d for d in self.docs if d["_id"] not in doomed]
        else:
            self.docs = []
        return SimpleNamespace(deleted_count=before - len(self.docs))


def ids(collection):
    return sorted(d["_id"] for d in collection.docs)


def test_prune_removes_the_oldest_above_the_ceiling():
    collection = FakeCollection([5, 1, 4, 2, 3])
    assert orders_store.prune(collection, 3) == 2
    assert ids(collection) == [3, 4, 5]


def test_prune_leaves_a_history_under_the_ceiling_alone():
    collection = FakeCollection([1, 2])
    assert orders_store.prune(collection, 3) == 0
    assert ids(collection) == [1, 2]


def test_zero_ceiling_disables_pruning_rather_than_deleting_everything():
    collection = FakeCollection([1, 2, 3])
    assert orders_store.prune(collection, 0) == 0
    assert ids(collection) == [1, 2, 3]


def test_reset_clears_every_order():
    collection = FakeCollection(range(2152))
    assert orders_store.reset(collection) == 2152
    assert orders_store.count(collection) == 0


def samples(text):
    """{name: value} for every sample line in Prometheus text format."""
    return {line.split()[0]: float(line.split()[1])
            for line in text.splitlines() if line and not line.startswith("#")}


def test_metrics_report_the_history_size_ceiling_and_pruning():
    text = orders_store.prometheus_text(documents=47, max_documents=50, pruned_total=312)
    assert samples(text) == {
        "orders_db_documents": 47,
        "orders_db_max_documents": 50,
        "orders_db_pruned_total": 312,
    }
    assert "# TYPE orders_db_pruned_total counter" in text
    assert text.endswith("\n")


def test_unreachable_orders_db_omits_the_count_rather_than_reporting_zero():
    text = orders_store.prometheus_text(documents=None, max_documents=50, pruned_total=0)
    assert "orders_db_documents" not in samples(text)
    assert samples(text)["orders_db_max_documents"] == 50


def test_an_empty_history_is_still_reported():
    assert samples(orders_store.prometheus_text(0, 50, 0))["orders_db_documents"] == 0
