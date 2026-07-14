import importlib.util
import pathlib
import threading
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "app" / "services" / "task_serial_executor.py"
spec = importlib.util.spec_from_file_location("task_serial_executor", MODULE_PATH)
if spec is None or spec.loader is None:
    raise ImportError("task_serial_executor module spec not found")
task_serial_executor = importlib.util.module_from_spec(spec)
spec.loader.exec_module(task_serial_executor)
ConcurrentTaskExecutor = task_serial_executor.ConcurrentTaskExecutor


class TestConcurrentTaskExecutor(unittest.TestCase):
    def test_executor_runs_two_tasks_concurrently(self):
        executor = ConcurrentTaskExecutor(max_workers=2)
        state_lock = threading.Lock()
        both_active = threading.Event()
        release_tasks = threading.Event()
        active = 0
        results = []
        errors = []

        def concurrent_work(call_id):
            nonlocal active
            with state_lock:
                active += 1
                if active == 2:
                    both_active.set()
            try:
                if not release_tasks.wait(timeout=1):
                    raise TimeoutError("test did not release concurrent tasks")
                return call_id
            finally:
                with state_lock:
                    active -= 1

        def run_task(call_id):
            try:
                result = executor.run(concurrent_work, call_id)
                with state_lock:
                    results.append(result)
            except Exception as exc:
                with state_lock:
                    errors.append(exc)

        threads = [threading.Thread(target=run_task, args=(call_id,)) for call_id in (1, 2)]
        try:
            for thread in threads:
                thread.start()

            self.assertTrue(both_active.wait(timeout=1), "two tasks were not active concurrently")
            release_tasks.set()
            for thread in threads:
                thread.join(timeout=1)

            self.assertFalse(any(thread.is_alive() for thread in threads))
            self.assertEqual(errors, [])
            self.assertCountEqual(results, [1, 2])
        finally:
            release_tasks.set()
            for thread in threads:
                if thread.ident is not None:
                    thread.join(timeout=1)
            executor.shutdown()


if __name__ == "__main__":
    unittest.main()
