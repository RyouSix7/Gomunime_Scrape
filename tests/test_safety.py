import tempfile
import threading
import unittest
from pathlib import Path

from scraper.models import Anime
from scraper.normalize import model_hash
from scraper.store import Store


class StoreSafetyTests(unittest.TestCase):
    def make_store(self):
        self.tmp = tempfile.TemporaryDirectory()
        return Store(Path(self.tmp.name) / "test.db")

    def tearDown(self):
        if hasattr(self, "tmp"):
            self.tmp.cleanup()

    def test_frontier_claim_is_exclusive(self):
        store = self.make_store()
        store.push_frontier([("https://example.test/a", "anime", 1, "test")])
        results = []
        barrier = threading.Barrier(2)
        def worker():
            barrier.wait()
            results.append(store.claim_frontier(1))
        threads = [threading.Thread(target=worker) for _ in range(2)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(sum(bool(x) for x in results), 1)
        store.close()

    def test_failed_claim_can_be_retried(self):
        store = self.make_store()
        store.push_frontier([("https://example.test/a", "anime", 1, "test")])
        item = store.claim_frontier(1)[0]
        store.complete_frontier(item["url"], item["lease_token"], success=False, retry_seconds=1)
        self.assertEqual(store.frontier_size(), 1)
        store.close()

    def test_genres_are_reconciled(self):
        store = self.make_store()
        a = Anime(id="a", slug="a", url="https://example.test/a", title="A", genres=["Action", "Drama"])
        a.content_hash = model_hash({"typed": a.__dict__, "raw": a.raw})
        store.upsert_anime(a)
        a.genres = ["Comedy"]
        a.content_hash = model_hash({"typed": a.__dict__, "raw": a.raw})
        store.upsert_anime(a)
        rows = store.db.execute("SELECT genre FROM anime_genre WHERE anime_id='a' ORDER BY genre").fetchall()
        self.assertEqual([r[0] for r in rows], ["Comedy"])
        store.close()


if __name__ == "__main__":
    unittest.main()
