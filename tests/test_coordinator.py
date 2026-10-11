import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from agent_memguard.coordinator import Coordinator
from agent_memguard.memory import Memory
from agent_memguard.processes import Process


class CoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.alive = patch("agent_memguard.coordinator.matches", return_value=True)
        self.alive.start()
        self.addCleanup(self.alive.stop)
        self.parent = self.directory / "parent"
        self.first = self.parent / "first"
        self.second = self.parent / "second"

    def memory(self, path, current=100, maximum=1000):
        return Memory(str(path), maximum, current, 0, 0, 0)

    def coordinator(self, pid):
        return Coordinator(self.directory / "state", {"pid": pid, "started": 1})

    def test_peer_without_pressure_does_not_clear_request(self):
        small = self.coordinator(1)
        large = self.coordinator(2)
        for guard in (small, large):
            guard.register([self.memory(self.parent)], .8125, True)
        small.update(10, {"pid": 11, "started": 1}, None)
        large.update(50, {"pid": 12, "started": 1}, None)
        small.update(10, {"pid": 11, "started": 1}, "hard")
        own, _ = large.update(50, {"pid": 12, "started": 1}, None)
        self.assertIsNotNone(own)
        self.assertEqual(own["level"], "hard")
        # Cancellation requires the source to observe recovery as well.
        small.update(10, {"pid": 11, "started": 1}, None)
        own, _ = large.update(50, {"pid": 12, "started": 1}, None)
        self.assertIsNone(own)

    def test_admission_checks_actual_parent_pressure(self):
        first = self.coordinator(1)
        second = self.coordinator(2)
        first.register([self.memory(self.first), self.memory(self.parent)], .8125, True)
        with self.assertRaisesRegex(RuntimeError, "environment memory pressure"):
            second.register([self.memory(self.second), self.memory(self.parent, current=900)], .8125, True)

    def test_sibling_parent_pressure_elects_largest_task_once(self):
        first = self.coordinator(1)
        second = self.coordinator(2)
        first.register([self.memory(self.first), self.memory(self.parent)], .8125, True)
        second.register([self.memory(self.second), self.memory(self.parent)], .8125, True)
        first.update(10, {"pid": 11, "started": 1}, None)
        second.update(50, {"pid": 12, "started": 1}, None)
        own, _ = first.update(10, {"pid": 11, "started": 1}, "hard", [str(self.parent)])
        self.assertIsNone(own)
        own, _ = second.update(50, {"pid": 12, "started": 1}, "hard", [str(self.parent)])
        self.assertIsNotNone(own)
        data = json.loads(first.path.read_text())
        self.assertEqual(len(data["requests"]), 1)

    def test_leaf_pressure_does_not_select_sibling(self):
        first = self.coordinator(1)
        second = self.coordinator(2)
        first.register([self.memory(self.first), self.memory(self.parent)], .8125, True)
        second.register([self.memory(self.second), self.memory(self.parent)], .8125, True)
        first.update(10, {"pid": 11, "started": 1}, None)
        second.update(50, {"pid": 12, "started": 1}, None)
        own, _ = first.update(10, {"pid": 11, "started": 1}, "hard", [str(self.first)])
        self.assertEqual(own["victim"], first.identifier)
        own, _ = second.update(50, {"pid": 12, "started": 1}, "hard", [str(self.parent)])
        self.assertIsNone(own)
        self.assertEqual(len(json.loads(first.path.read_text())["requests"]), 1)

    def test_admission_does_not_reserve_per_task_budgets(self):
        first = self.coordinator(1)
        second = self.coordinator(2)
        first.register([self.memory(self.first), self.memory(self.parent, maximum=2000)], .8125, True)
        second.register([self.memory(self.second, maximum=700),
                         self.memory(self.parent, maximum=2000)], .8125, True)
        for task in json.loads(first.path.read_text())["tasks"].values():
            self.assertNotIn("limit", task)
        for pid in range(3, 23):
            self.coordinator(pid).register([self.memory(self.first), self.memory(self.parent, maximum=2000)],
                                            .8125, True)
        self.assertEqual(len(json.loads(first.path.read_text())["tasks"]), 22)

    def test_independent_scopes_have_independent_elections(self):
        first = self.coordinator(1)
        second = self.coordinator(2)
        first.register([self.memory(self.first)], .8125, True)
        second.register([self.memory(self.second)], .8125, True)
        first.update(10, {"pid": 11, "started": 1}, None)
        second.update(50, {"pid": 12, "started": 1}, None)
        self.assertIsNotNone(first.update(10, {"pid": 11, "started": 1}, "hard")[0])
        self.assertIsNotNone(second.update(50, {"pid": 12, "started": 1}, "hard")[0])

    def test_peer_kill_result_survives_recovery_until_owner_unregisters(self):
        small = self.coordinator(1)
        large = self.coordinator(2)
        for guard in (small, large):
            guard.register([self.memory(self.parent)], .8125, True)
        small.update(10, {"pid": 11, "started": 1}, None)
        large.update(50, {"pid": 12, "started": 1}, None)
        with patch("agent_memguard.coordinator.time.monotonic", return_value=10):
            small.update(10, {"pid": 11, "started": 1}, "hard")
        with patch("agent_memguard.coordinator.time.monotonic", return_value=11):
            _, peer = small.update(10, {"pid": 11, "started": 1}, "hard")
        self.assertIsNotNone(peer)
        self.assertEqual(small.commit(peer["request"], "SIGKILL")["reason"], "environment_pressure")
        small.update(10, {"pid": 11, "started": 1}, None)
        own, _ = large.update(0, {"pid": 12, "started": 1}, None)
        self.assertEqual(own["termination"]["reason"], "environment_pressure")
        self.assertEqual(own["termination"]["signal"], "SIGKILL")
        small.update(10, {"pid": 11, "started": 1}, None)
        self.assertIn(large.identifier, json.loads(small.path.read_text())["tasks"])
        large.unregister()
        self.assertNotIn(large.identifier, json.loads(small.path.read_text())["tasks"])
        self.assertIsNone(small.update(10, {"pid": 11, "started": 1}, "hard")[0])

    def test_canceled_request_cannot_be_committed(self):
        guard = self.coordinator(1)
        guard.register([self.memory(self.parent)], .8125, True)
        own, _ = guard.update(10, {"pid": 11, "started": 1}, "hard")
        guard.update(10, {"pid": 11, "started": 1}, None)
        self.assertIsNone(guard.commit(own, "SIGKILL"))

    def test_multiple_sources_must_all_observe_recovery(self):
        first = self.coordinator(1)
        second = self.coordinator(2)
        for guard in (first, second):
            guard.register([self.memory(self.parent)], .8125, True)
        first.update(10, {"pid": 11, "started": 1}, None)
        second.update(50, {"pid": 12, "started": 1}, None)
        first.update(10, {"pid": 11, "started": 1}, "soft")
        second.update(50, {"pid": 12, "started": 1}, "hard")
        first.update(10, {"pid": 11, "started": 1}, None)
        self.assertEqual(len(json.loads(first.path.read_text())["requests"]), 1)
        second.update(50, {"pid": 12, "started": 1}, None)
        self.assertEqual(len(json.loads(first.path.read_text())["requests"]), 0)

    def test_dead_pressure_source_does_not_leave_uncommitted_request(self):
        first = self.coordinator(1)
        second = self.coordinator(2)
        for guard in (first, second):
            guard.register([self.memory(self.parent)], .8125, True)
        first.update(10, {"pid": 11, "started": 1}, None)
        second.update(50, {"pid": 12, "started": 1}, None)
        first.update(10, {"pid": 11, "started": 1}, "hard")
        with patch("agent_memguard.coordinator.matches", side_effect=lambda owner: owner["pid"] == 2):
            self.assertIsNone(second.update(50, {"pid": 12, "started": 1}, None)[0])

    def test_committed_termination_cannot_be_canceled_by_recovery(self):
        guard = self.coordinator(1)
        guard.register([self.memory(self.parent)], .8125, True)
        own, _ = guard.update(10, {"pid": 11, "started": 1}, "soft")
        guard.commit(own, "SIGTERM")
        own, _ = guard.update(10, {"pid": 11, "started": 1}, None)
        self.assertEqual(own["termination"]["signal"], "SIGTERM")
        self.assertEqual(guard.commit(own, "SIGKILL")["signal"], "SIGKILL")

    def test_parent_hard_pressure_upgrades_committed_leaf_request(self):
        source = self.coordinator(1)
        victim = self.coordinator(2)
        memories = [self.memory(self.first), self.memory(self.parent)]
        for guard in (source, victim):
            guard.register(memories, .8125, True)
        source.update(10, {"pid": 11, "started": 1}, None)
        victim.update(50, {"pid": 12, "started": 1}, None)
        with patch("agent_memguard.coordinator.time.monotonic", return_value=10):
            source.update(10, {"pid": 11, "started": 1}, "soft", [str(self.first)])
            own, _ = victim.update(50, {"pid": 12, "started": 1}, None)
            victim.commit(own, "SIGTERM")
        # The victim stops sampling; the source sees only parent hard pressure.
        with patch("agent_memguard.coordinator.time.monotonic", return_value=11):
            _, peer = source.update(10, {"pid": 11, "started": 1}, "hard", [str(self.parent)])
        self.assertIsNotNone(peer)
        self.assertEqual(peer["owner"], victim.owner)
        self.assertEqual(peer["request"]["level"], "hard")
        self.assertEqual(len(json.loads(source.path.read_text())["requests"]), 1)
        source.commit(peer["request"], "SIGKILL")
        own, _ = victim.update(0, {"pid": 12, "started": 1}, None)
        self.assertEqual(own["termination"]["signal"], "SIGKILL")

    def test_new_finite_parent_upgrades_existing_leaf_request(self):
        source = self.coordinator(1)
        victim = self.coordinator(2)
        for guard in (source, victim):
            guard.register([self.memory(self.first)], .8125, True, paths=[self.first, self.parent])
        source.update(10, {"pid": 11, "started": 1}, None)
        victim.update(50, {"pid": 12, "started": 1}, None)
        with patch("agent_memguard.coordinator.time.monotonic", return_value=10):
            source.update(10, {"pid": 11, "started": 1}, "soft", [str(self.first)])
            own, _ = victim.update(50, {"pid": 12, "started": 1}, None)
            victim.commit(own, "SIGTERM")
        with patch("agent_memguard.coordinator.time.monotonic", return_value=11):
            _, peer = source.update(10, {"pid": 11, "started": 1}, "hard", [str(self.parent)],
                                    memories=[self.memory(self.first), self.memory(self.parent, current=900)])
        self.assertIsNotNone(peer)
        self.assertIn(str(self.parent), peer["request"]["domain"])
        self.assertEqual(peer["request"]["level"], "hard")

    def test_new_finite_scope_includes_peer_before_its_next_sample(self):
        source = self.coordinator(1)
        victim = self.coordinator(2)
        for guard in (source, victim):
            guard.register([], .8125, True, paths=[self.parent])
        source.update(10, {"pid": 11, "started": 1}, None, memories=[])
        victim.update(50, {"pid": 12, "started": 1}, None, memories=[])
        source.update(10, {"pid": 11, "started": 1}, "hard", [str(self.parent)],
                      memories=[self.memory(self.parent, current=900)])
        data = json.loads(source.path.read_text())
        self.assertEqual(data["tasks"][victim.identifier]["scopes"], [str(self.parent)])
        self.assertEqual(data["requests"][str(self.parent)]["victim"], victim.identifier)

    def test_new_finite_scope_refuses_start_only_when_pressured(self):
        existing = self.coordinator(1)
        newcomer = self.coordinator(2)
        existing.register([], .8125, True, paths=[self.parent])
        with self.assertRaisesRegex(RuntimeError, "environment memory pressure"):
            newcomer.register([self.memory(self.parent, current=900)], .8125, True, paths=[self.parent])
        newcomer.register([self.memory(self.parent)], .8125, True, paths=[self.parent])

    def test_removing_limit_clears_peer_scope_and_uncommitted_pressure(self):
        first = self.coordinator(1)
        second = self.coordinator(2)
        for guard in (first, second):
            guard.register([self.memory(self.parent)], .8125, True, paths=[self.parent])
        first.update(10, {"pid": 11, "started": 1}, "hard")
        second.update(10, {"pid": 12, "started": 1}, None, memories=[])
        data = json.loads(first.path.read_text())
        self.assertEqual(data["requests"], {})
        self.assertTrue(all(task["scopes"] == [] and task["pressure"] == {}
                            for task in data["tasks"].values()))

    def test_unlimited_parent_does_not_serialize_independent_leaf_requests(self):
        first = self.coordinator(1)
        second = self.coordinator(2)
        first.register([self.memory(self.first)], .8125, True, paths=[self.first, self.parent])
        second.register([self.memory(self.second)], .8125, True, paths=[self.second, self.parent])
        first.update(10, {"pid": 11, "started": 1}, None)
        second.update(50, {"pid": 12, "started": 1}, None)
        self.assertIsNotNone(first.update(10, {"pid": 11, "started": 1}, "hard")[0])
        self.assertIsNotNone(second.update(50, {"pid": 12, "started": 1}, "hard")[0])
        self.assertEqual(len(json.loads(first.path.read_text())["requests"]), 2)

    def committed_pair(self):
        source = self.coordinator(1)
        victim = self.coordinator(2)
        for guard in (source, victim):
            guard.register([self.memory(self.parent)], .8125, True)
        source.update(10, {"pid": 11, "started": 1}, None)
        victim.update(50, {"pid": 12, "started": 1}, None)
        source.update(10, {"pid": 11, "started": 1}, "hard")
        own, _ = victim.update(50, {"pid": 12, "started": 1}, None)
        victim.commit(own, "SIGKILL")
        return source, victim

    def stopped_owner_table(self, root_state="Z", worker=False):
        table = {
            1: Process(1, 100, 1, 1, 10, "S"),
            11: Process(11, 1, 11, 1, 10, "S"),
            2: Process(2, 100, 2, 1, 10, "T"),
            12: Process(12, 2, 12, 1, 0, root_state),
        }
        if worker:
            # A new-session orphan remains adopted by the stopped subreaper.
            table[22] = Process(22, 2, 22, 1, 50, "S")
        return table

    def test_exited_tree_releases_election_without_owner_acknowledgment(self):
        source, victim = self.committed_pair()
        with patch("agent_memguard.coordinator.time.monotonic", return_value=10):
            source.update(10, {"pid": 11, "started": 1}, "hard", table=self.stopped_owner_table())
        data = json.loads(source.path.read_text())
        self.assertEqual(data["requests"], {})
        self.assertTrue(data["tasks"][victim.identifier]["released"])
        self.assertEqual(data["tasks"][victim.identifier]["rss"], 0)
        self.assertEqual(data["tasks"][victim.identifier]["pressure"], {})
        with patch("agent_memguard.coordinator.time.monotonic", return_value=11):
            own, _ = source.update(10, {"pid": 11, "started": 1}, "hard", table=self.stopped_owner_table())
        self.assertEqual(own["victim"], source.identifier)
        # The stopped owner can recover the original result after the request
        # has been removed and another victim has already been elected.
        own, _ = victim.update(0, {"pid": 12, "started": 1}, None)
        self.assertEqual(own["termination"]["signal"], "SIGKILL")
        self.assertEqual(own["termination"]["reason"], "environment_pressure")

    def test_kill_intent_or_dead_root_does_not_release_live_task_tree(self):
        for index, (root_state, worker) in enumerate((("D", False), ("Z", True))):
            with self.subTest(root_state=root_state, worker=worker), \
                    patch("agent_memguard.coordinator.time.monotonic", return_value=10 + 2 * index):
                source, victim = self.committed_pair()
                source.update(10, {"pid": 11, "started": 1}, None,
                              table=self.stopped_owner_table(root_state, worker))
                data = json.loads(source.path.read_text())
                self.assertFalse(data["tasks"][victim.identifier]["released"])
                self.assertEqual(len(data["requests"]), 1)
                source.unregister()
                victim.unregister()

    def test_admission_finishes_exited_request_while_owner_is_stopped(self):
        source, victim = self.committed_pair()
        newcomer = self.coordinator(3)
        newcomer.register([self.memory(self.parent)], .8125, True, table=self.stopped_owner_table())
        data = json.loads(source.path.read_text())
        self.assertTrue(data["tasks"][victim.identifier]["released"])
        self.assertIn(newcomer.identifier, data["tasks"])
        self.assertEqual(data["requests"], {})

    def test_sibling_hard_pressure_does_not_wait_for_soft_request(self):
        first = self.coordinator(1)
        second = self.coordinator(2)
        first.register([self.memory(self.first), self.memory(self.parent, maximum=4000)], .8125, True)
        second.register([self.memory(self.second), self.memory(self.parent, maximum=4000)], .8125, True)
        first.update(50, {"pid": 11, "started": 1}, None)
        second.update(10, {"pid": 12, "started": 1}, None)
        own, _ = first.update(50, {"pid": 11, "started": 1}, "soft", [str(self.first)])
        first.commit(own, "SIGTERM")
        own, _ = second.update(10, {"pid": 12, "started": 1}, "hard", [str(self.second)])
        self.assertEqual(own["victim"], second.identifier)
        data = json.loads(first.path.read_text())
        self.assertEqual(len(data["requests"]), 2)
        self.assertEqual(data["requests"][str(self.first)]["level"], "soft")
        self.assertEqual(data["requests"][str(self.second)]["level"], "hard")

    def test_sibling_cooldown_does_not_delay_leaf_hard_protection(self):
        first = self.coordinator(1)
        second = self.coordinator(2)
        for guard, leaf in ((first, self.first), (second, self.second)):
            guard.register([self.memory(leaf), self.memory(self.parent)], .8125, True)
        first.update(50, {"pid": 11, "started": 1}, None)
        second.update(10, {"pid": 12, "started": 1}, None)
        own, _ = first.update(50, {"pid": 11, "started": 1}, "soft", [str(self.first)])
        first.commit(own, "SIGTERM")
        with patch("agent_memguard.coordinator.time.monotonic", return_value=10):
            first.unregister()
            own, _ = second.update(10, {"pid": 12, "started": 1}, "hard", [str(self.second)])
        self.assertEqual(own["victim"], second.identifier)

    def test_parent_victim_does_not_block_unrelated_leaf_hard_protection(self):
        first = self.coordinator(1)
        second = self.coordinator(2)
        for guard, leaf in ((first, self.first), (second, self.second)):
            guard.register([self.memory(leaf), self.memory(self.parent)], .8125, True)
        first.update(10, {"pid": 11, "started": 1}, None)
        second.update(50, {"pid": 12, "started": 1}, None)
        first.update(10, {"pid": 11, "started": 1}, "hard", [str(self.parent)])
        second.update(50, {"pid": 12, "started": 1}, "hard", [str(self.parent)])
        own, _ = first.update(10, {"pid": 11, "started": 1}, "hard", [str(self.first)])
        self.assertEqual(own["victim"], first.identifier)
        self.assertEqual(len(json.loads(first.path.read_text())["requests"]), 2)


if __name__ == "__main__":
    unittest.main()
