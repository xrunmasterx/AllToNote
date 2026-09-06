from concurrent.futures import ThreadPoolExecutor
import json
import queue
import threading
import time

import pytest

from app.gpt.codex_app_server_client import CodexAppServerClient, CodexAppServerError


class _PersistentProcess:
    """Interactive JSON-RPC peer: stdout stays open across successive turns."""
    def __init__(self, number, on_turn=None):
        self.pid = number
        self.stdin = self
        self.stdout = self._lines()
        self.stderr = iter(())
        self.messages = []
        self.output = queue.Queue()
        self.terminated = False
        self.thread_ids = []
        self.on_turn = on_turn

    def _lines(self):
        while True:
            message = self.output.get()
            if message is None:
                return
            yield json.dumps(message) + "\n"

    def write(self, text):
        message = json.loads(text)
        self.messages.append(message)
        method = message["method"]
        if method == "initialized":
            return len(text)
        result = {}
        if method == "thread/start":
            thread_id = f"thread-{self.pid}-{len(self.thread_ids)}"
            self.thread_ids.append(thread_id)
            result = {"thread": {"id": thread_id}}
        self.output.put({"id": message["id"], "result": result})
        if method == "turn/start":
            if self.on_turn is not None:
                self.on_turn(self, message["params"])
            else:
                self.complete(message["params"])
        if method == "thread/unsubscribe":
            # Late notifications from the previous job must not enter the next answer.
            self.output.put({"method": "item/agentMessage/delta", "params": {
                "threadId": message["params"]["threadId"], "delta": "STALE JOB TEXT"}})
        return len(text)

    def complete(self, params):
        self.output.put({"method": "item/agentMessage/delta", "params": {
            "threadId": params["threadId"], "delta": params["input"][0]["text"]}})
        self.output.put({"method": "turn/completed", "params": {
            "threadId": params["threadId"], "turn": {"status": "completed"}}})

    def flush(self):
        pass

    def poll(self):
        return 0 if self.terminated else None

    def terminate(self):
        self.terminated = True
        self.output.put(None)

    kill = terminate

    def wait(self, timeout=None):
        return 0


@pytest.fixture
def peers(monkeypatch):
    processes = []
    lock = threading.Lock()

    def install(on_turn=None):
        def popen(*args, **kwargs):
            with lock:
                peer = _PersistentProcess(len(processes), on_turn)
                processes.append(peer)
                return peer
        monkeypatch.setattr("app.gpt.codex_app_server_client.subprocess.Popen", popen)
        return processes

    monkeypatch.setattr("app.gpt.codex_app_server_client.CodexAppServerStatusService.assert_ready", lambda: None)
    monkeypatch.setattr(CodexAppServerClient, "_is_windows", staticmethod(lambda: False))
    return install


def test_reuses_transport_but_never_reuses_thread_or_previous_text(peers):
    processes = peers()
    client = CodexAppServerClient(codex_bin="codex", pool_size=1)
    try:
        for index in range(12):
            assert client.run_markdown_turn(f"video-{index}", "gpt-5.6-terra",
                output_schema={"type": "object"}, reasoning_effort="high",
                image_webp=(b"frame",)) == f"video-{index}"
        assert len(processes) == 1
        messages = processes[0].messages
        assert sum(m["method"] == "initialize" for m in messages) == 1
        assert len(set(processes[0].thread_ids)) == 12
        assert all(m["params"]["ephemeral"] for m in messages if m["method"] == "thread/start")
        turns = [m["params"] for m in messages if m["method"] == "turn/start"]
        assert all(t["effort"] == "high" and t["outputSchema"] == {"type": "object"}
                   and t["input"][1]["type"] == "image" for t in turns)
        ids = [m["id"] for m in messages if "id" in m]
        assert len(ids) == len(set(ids))
    finally:
        client.close()
    assert all(p.terminated for p in processes)


def test_four_videos_share_eight_call_limit_and_isolate_64_answers(peers, tmp_path):
    lock = threading.Lock()
    release = threading.Event()
    eight_active = threading.Event()
    active = peak = 0

    def on_turn(peer, params):
        nonlocal active, peak
        def finish():
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
                if active == 8:
                    eight_active.set()
            assert release.wait(5)
            time.sleep(0.01)
            with lock:
                active -= 1
            peer.complete(params)
        threading.Thread(target=finish, daemon=True).start()

    processes = peers(on_turn)
    clients = [CodexAppServerClient(codex_bin="codex", pool_size=4,
                                   machine_slot_root=tmp_path) for _ in range(4)]
    try:
        with ThreadPoolExecutor(max_workers=32) as executor:
            futures = [executor.submit(clients[i % 4].run_markdown_turn, f"video-{i % 4}/section-{i}",
                                       "gpt-5.6-terra", timeout_seconds=10) for i in range(64)]
            try:
                assert eight_active.wait(5)
                assert peak == 8
            finally:
                release.set()
            assert [f.result() for f in futures] == [f"video-{i % 4}/section-{i}" for i in range(64)]
        assert peak == 8
        assert len(processes) <= 16
    finally:
        release.set()
        for client in clients:
            client.close()
    assert all(p.terminated for p in processes)


def test_waiting_cancellation_and_active_timeout_do_not_poison_pool(peers):
    entered = threading.Event()
    cancel = threading.Event()

    def on_turn(peer, params):
        if params["input"][0]["text"] == "blocked":
            entered.set()
        else:
            peer.complete(params)

    processes = peers(on_turn)
    client = CodexAppServerClient(codex_bin="codex", pool_size=1)

    def check_cancelled():
        if cancel.is_set():
            raise RuntimeError("cancel queued job")

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            blocked = executor.submit(client.run_markdown_turn, "blocked", "model", timeout_seconds=0.5)
            assert entered.wait(1)
            waiting = executor.submit(client.run_markdown_turn, "queued", "model", check_cancelled=check_cancelled)
            cancel.set()
            with pytest.raises(RuntimeError, match="cancel queued job"):
                waiting.result(timeout=1)
            with pytest.raises(CodexAppServerError, match="Timed out"):
                blocked.result(timeout=1)
        assert processes[0].terminated
        assert client.run_markdown_turn("next video", "model") == "next video"
        assert len(processes) == 2
    finally:
        client.close()


def test_failed_thread_cleanup_discards_transport_without_losing_completed_answer(peers, monkeypatch):
    original = _PersistentProcess.write

    def write(self, text):
        message = json.loads(text)
        if message["method"] == "thread/unsubscribe":
            self.output.put({"id": message["id"], "error": {"message": "cleanup failed"}})
            return len(text)
        return original(self, text)

    monkeypatch.setattr(_PersistentProcess, "write", write)
    processes = peers()
    client = CodexAppServerClient(codex_bin="codex", pool_size=1)
    try:
        assert client.run_markdown_turn("first", "model") == "first"
        assert processes[0].terminated
        assert client.run_markdown_turn("second", "model") == "second"
        assert len(processes) == 2
    finally:
        client.close()


def test_close_wakes_waiters_and_terminates_active_connection(peers):
    entered = threading.Event()
    processes = peers(lambda peer, params: entered.set())
    client = CodexAppServerClient(codex_bin="codex", pool_size=1)
    with ThreadPoolExecutor(max_workers=2) as executor:
        active = executor.submit(client.run_markdown_turn, "active", "model")
        assert entered.wait(1)
        waiting = executor.submit(client.run_markdown_turn, "waiting", "model")
        client.close()
        for future in (active, waiting):
            with pytest.raises(CodexAppServerError):
                future.result(timeout=2)
    assert len(processes) == 1 and processes[0].terminated
