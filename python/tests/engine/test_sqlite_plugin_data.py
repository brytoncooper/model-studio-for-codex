import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from model_deck.adapters.storage.sqlite_plugin_data import SQLitePluginDataRepository
from model_deck.engine.plugin_data.ports import (
    PluginDataNotFoundError,
    PluginDataQuota,
    PluginDataQuotaExceededError,
    PluginDataRevisionConflictError,
)

NS = "com.example.plugin"
NS2 = "com.other.plugin"


def _repo(d: str, name: str = "state.sqlite3", **kw) -> SQLitePluginDataRepository:
    return SQLitePluginDataRepository(Path(d) / name, **kw)


class SQLitePluginDataTests(unittest.TestCase):
    def test_put_get_restart_persistence(self) -> None:
        with TemporaryDirectory() as d:
            r = _repo(d)
            e = r.put(NS, "k1", {"a": 1})
            self.assertEqual(e.revision, 1)
            r2 = _repo(d)
            g = r2.get(NS, "k1")
            self.assertEqual((g.value, g.revision), ({"a": 1}, 1))

    def test_cas_unconditional_zero_and_mismatch(self) -> None:
        with TemporaryDirectory() as d:
            r = _repo(d)
            self.assertEqual(r.put(NS, "k", 1, expected_revision=0).revision, 1)
            with self.assertRaises(PluginDataRevisionConflictError):
                r.put(NS, "k", 2, expected_revision=0)
            with self.assertRaises(PluginDataRevisionConflictError):
                r.put(NS, "missing", 2, expected_revision=3)
            e = r.put(NS, "k", 2)
            self.assertEqual(e.revision, 2)
            with self.assertRaises(PluginDataRevisionConflictError):
                r.put(NS, "k", 3, expected_revision=1)
            self.assertEqual(r.put(NS, "k", 3, expected_revision=2).revision, 3)

    def test_concurrent_cas_one_winner(self) -> None:
        with TemporaryDirectory() as d:
            r = _repo(d)
            r.put(NS, "k", 0)
            wins: list = []
            def attempt(i: int) -> None:
                try:
                    rr = SQLitePluginDataRepository(Path(d) / "state.sqlite3")
                    e = rr.put(NS, "k", i + 1, expected_revision=1)
                    wins.append(e.revision)
                except PluginDataRevisionConflictError:
                    pass
            ts = [threading.Thread(target=attempt, args=(i,)) for i in range(8)]
            [t.start() for t in ts]
            [t.join() for t in ts]
            self.assertEqual(len(wins), 1)
            self.assertEqual(_repo(d).get(NS, "k").revision, 2)

    def test_quota_atomic(self) -> None:
        with TemporaryDirectory() as d:
            q = PluginDataQuota(max_bytes=10**9, max_keys=2, max_value_bytes=10**6)
            r = _repo(d, quota=q)
            r.put(NS, "a", 1)
            r.put(NS, "b", 2)
            with self.assertRaises(PluginDataQuotaExceededError):
                r.put(NS, "c", 3)
            self.assertEqual([i.key for i in r.list(NS)], ["a", "b"])
            with self.assertRaises(PluginDataQuotaExceededError):
                r.put(NS, "big", "x" * (10**6 + 1))

    def test_namespace_isolation(self) -> None:
        with TemporaryDirectory() as d:
            r = _repo(d)
            r.put(NS, "k", 1)
            with self.assertRaises(PluginDataNotFoundError):
                r.get(NS2, "k")
            self.assertEqual(r.list(NS2), [])
            self.assertEqual(len(r.list(NS)), 1)

    def test_tombstone_monotonic_no_aba(self) -> None:
        with TemporaryDirectory() as d:
            r = _repo(d)
            r.put(NS, "k", 1)
            self.assertTrue(r.delete(NS, "k"))
            with self.assertRaises(PluginDataNotFoundError):
                r.get(NS, "k")
            with self.assertRaises(PluginDataRevisionConflictError):
                r.put(NS, "k", 2, expected_revision=0)
            e = r.put(NS, "k", 2, expected_revision=2)
            self.assertEqual(e.revision, 3)
            self.assertFalse(r.delete(NS, "gone"))
            with self.assertRaises(PluginDataRevisionConflictError):
                r.delete(NS, "gone", expected_revision=5)

    def test_null_distinct_from_missing_and_validation(self) -> None:
        with TemporaryDirectory() as d:
            r = _repo(d)
            r.put(NS, "n", None)
            self.assertIsNone(r.get(NS, "n").value)
            with self.assertRaises(PluginDataNotFoundError):
                r.get(NS, "absent")
            with self.assertRaises(ValueError):
                r.put(NS, "bad", float("nan"))
            with self.assertRaises(ValueError):
                r.put(NS, "bad", float("inf"))
            with self.assertRaises(ValueError):
                r.put(NS, "k", 1, expected_revision=True)  # type: ignore[arg-type]
            with self.assertRaises(ValueError):
                r.list(NS, limit=0)

    def test_list_prefix_limit_deterministic(self) -> None:
        with TemporaryDirectory() as d:
            r = _repo(d)
            for k in ("b2", "a1", "a2", "c"):
                r.put(NS, k, 1)
            r.delete(NS, "c")
            items = r.list(NS, prefix="a", limit=200)
            self.assertEqual([(i.key, i.revision) for i in items], [("a1", 1), ("a2", 1)])
            limited = r.list(NS, limit=2)
            self.assertEqual([i.key for i in limited], ["a1", "a2"])

    def test_quota_per_namespace_max_keys_one(self) -> None:
        with TemporaryDirectory() as d:
            r = _repo(d, quota=PluginDataQuota(max_bytes=10 * 1024 * 1024, max_keys=1, max_value_bytes=1024 * 1024))
            r.put(NS, "only", 1)
            r.put(NS2, "only", 1)
            self.assertEqual(r.get(NS, "only").value, 1)
            self.assertEqual(r.get(NS2, "only").value, 1)

    def test_prefix_case_sensitive_and_nul_safe(self) -> None:
        with TemporaryDirectory() as d:
            r = _repo(d)
            r.put(NS, "Alpha", 1)
            r.put(NS, "alpha", 2)
            r.put(NS, "a\x00b", 3)
            r.put(NS, "a\x00c", 4)
            r.put(NS, "aXd", 5)
            upper = [i.key for i in r.list(NS, prefix="A")]
            lower = [i.key for i in r.list(NS, prefix="a")]
            self.assertEqual(upper, ["Alpha"])
            self.assertNotIn("Alpha", lower)
            self.assertIn("alpha", lower)
            nul = [i.key for i in r.list(NS, prefix="a\x00")]
            self.assertEqual(sorted(nul), ["a\x00b", "a\x00c"])
            self.assertNotIn("aXd", nul)

    def test_strict_json_types_rejected(self) -> None:
        with TemporaryDirectory() as d:
            r = _repo(d)
            with self.assertRaises(TypeError):
                r.put(NS, "t", (1, 2))  # type: ignore[arg-type]
            with self.assertRaises(TypeError):
                r.put(NS, "t", {1: "x"})  # type: ignore[dict-item]
            with self.assertRaises(TypeError):
                r.put(NS, "t", {"n": (float("nan"),)})  # type: ignore[dict-item]
            with self.assertRaises(ValueError):
                r.put(NS, "t", {"n": float("nan")})

    def test_prefix_includes_max_unicode_suffix(self) -> None:
        with TemporaryDirectory() as d:
            r = _repo(d)
            top = chr(0x10FFFF)
            r.put(NS, "pfx", 1)
            r.put(NS, "pfx" + top, 2)
            r.put(NS, "pfx" + top + "tail", 3)
            r.put(NS, "pfy", 4)
            got = [i.key for i in r.list(NS, prefix="pfx")]
            self.assertEqual(got, ["pfx", "pfx" + top, "pfx" + top + "tail"])

    def test_prefix_surrogate_boundary(self) -> None:
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as d:
            r = _repo(d)
            d7ff = chr(0xD7FF)
            r.put(NS, d7ff, 1)
            r.put(NS, d7ff + "tail", 2)
            r.put(NS, chr(0xE000), 3)
            got = [i.key for i in r.list(NS, prefix=d7ff)]
            self.assertEqual(got, [d7ff, d7ff + "tail"])

    def test_surrogate_input_rejected(self) -> None:
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as d:
            r = _repo(d)
            with self.assertRaises(ValueError):
                r.put(NS, "\ud800", 1)
            with self.assertRaises(ValueError):
                r.list(NS, prefix="\ud800")

    def test_prefix_all_max_unicode(self) -> None:
        with TemporaryDirectory() as d:
            r = _repo(d)
            top = chr(0x10FFFF)
            r.put(NS, top, 1)
            r.put(NS, top + "a", 2)
            r.put(NS, "a", 3)
            got = [i.key for i in r.list(NS, prefix=top)]
            self.assertEqual(got, [top, top + "a"])

    def test_empty_key_legal_and_newline_namespace_rejected(self) -> None:
        with TemporaryDirectory() as d:
            r = _repo(d)
            e = r.put(NS, "", {"e": True})
            self.assertEqual(e.revision, 1)
            self.assertEqual(r.get(NS, "").value, {"e": True})
            with self.assertRaises(ValueError):
                r.put("com.example.plugin\n", "k", 1)
            with self.assertRaises(ValueError):
                r.put("com.example.plugin\n.evil", "k", 1)

if __name__ == "__main__":
    unittest.main()
