import asyncio
import unittest

from app.core.orchestrator import AccountWorker, Orchestrator


class OkRunner:
    def __init__(self):
        self.calls = 0

    async def run_cycle(self):
        self.calls += 1
        return f"report{self.calls}"


class FailRunner:
    async def run_cycle(self):
        raise RuntimeError("boom")


class WorkerTests(unittest.TestCase):
    def test_run_once_success_sets_idle(self):
        states = []
        worker = AccountWorker(1, OkRunner(), on_state=lambda a, s, e: states.append((s, e)))
        report = asyncio.run(worker.run_once())
        self.assertEqual(report, "report1")
        self.assertEqual(worker.status, "idle")
        self.assertIn(("idle", None), states)

    def test_run_once_failure_is_isolated(self):
        worker = AccountWorker(2, FailRunner())
        result = asyncio.run(worker.run_once())  # must not raise
        self.assertIsNone(result)
        self.assertEqual(worker.status, "error")


class FakeWorker:
    def __init__(self, account_id):
        self.account_id = account_id
        self.status = "idle"

    async def run_forever(self, stop):
        self.status = "running"
        await stop.wait()
        self.status = "stopped"


class OrchestratorTests(unittest.TestCase):
    def test_start_stop_lifecycle(self):
        async def scenario():
            orch = Orchestrator(lambda aid: FakeWorker(aid))
            await orch.start(1)
            await orch.start(2)
            await asyncio.sleep(0)  # let the tasks begin
            self.assertEqual(set(orch.running), {1, 2})
            self.assertEqual(orch.status()[1], "running")

            await orch.stop(1)
            self.assertEqual(orch.running, [2])

            await orch.stop_all()
            self.assertEqual(orch.running, [])

        asyncio.run(scenario())

    def test_reload_reconciles_enabled_set(self):
        async def scenario():
            orch = Orchestrator(lambda aid: FakeWorker(aid))
            await orch.start(1)
            await asyncio.sleep(0)
            await orch.reload([2, 3])  # 1 should stop, 2 & 3 start
            await asyncio.sleep(0)
            self.assertEqual(set(orch.running), {2, 3})
            await orch.stop_all()

        asyncio.run(scenario())

    def test_start_is_idempotent(self):
        async def scenario():
            orch = Orchestrator(lambda aid: FakeWorker(aid))
            await orch.start(1)
            await orch.start(1)  # no duplicate
            self.assertEqual(orch.running, [1])
            await orch.stop_all()

        asyncio.run(scenario())

    def test_group_start_stop_and_guard(self):
        async def scenario():
            orch = Orchestrator(lambda aid: FakeWorker(aid),
                                group_factory=lambda ids: FakeWorker(0))
            await orch.start_group([1, 2])
            await asyncio.sleep(0)
            self.assertTrue(orch.group_running)
            self.assertEqual(orch.group_members, {1, 2})
            # a group member cannot be started solo
            with self.assertRaises(RuntimeError):
                await orch.start(1)
            # a non-member still can
            await orch.start(3)
            await asyncio.sleep(0)
            self.assertEqual(orch.running, [3])

            await orch.stop_group()
            self.assertFalse(orch.group_running)
            self.assertEqual(orch.group_members, set())
            # now the former member can run solo
            await orch.start(1)
            await orch.stop_all()

        asyncio.run(scenario())

    def test_group_start_stops_individual_members_first(self):
        async def scenario():
            orch = Orchestrator(lambda aid: FakeWorker(aid),
                                group_factory=lambda ids: FakeWorker(0))
            await orch.start(1)
            await asyncio.sleep(0)
            await orch.start_group([1, 2])  # 1 was running solo -> stopped, folded in
            await asyncio.sleep(0)
            self.assertEqual(orch.running, [])  # no solo workers
            self.assertEqual(orch.group_members, {1, 2})
            await orch.stop_all()

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
