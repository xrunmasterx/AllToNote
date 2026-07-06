# Codex App Server GPT Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a Codex App Server model backend so BiliNote can generate notes through the local Codex CLI login without an OpenAI API key.

**Architecture:** Keep the BiliNote video pipeline unchanged and add a GPT-layer adapter. `GPTFactory` routes normal providers to `UniversalGPT` and routes `codex_app_server` to `CodexAppServerGPT`, which talks to `codex app-server --stdio` through a small JSON-RPC client.

**Tech Stack:** Python 3.11, FastAPI, SQLAlchemy, pytest, Codex CLI app-server stdio JSON-RPC, React 19, TypeScript, Vite, Zustand.

---

## File Structure

Backend files:

- Modify `backend/app/db/builtin_providers.json`: add the built-in `codex_app_server` provider.
- Modify `backend/app/db/provider_dao.py`: seed missing built-in providers even when the table already has rows.
- Create `backend/app/services/codex_app_server.py`: status checks for CLI, auth, config model, and a minimal runtime test hook.
- Create `backend/app/routers/codex_app_server.py`: status endpoint.
- Modify `backend/app/__init__.py`: register the new router.
- Modify `backend/app/services/model.py`: dispatch model listing and connection tests for `codex_app_server`.
- Create `backend/app/gpt/codex_app_server_client.py`: stdio JSON-RPC client and event parser.
- Create `backend/app/gpt/codex_app_server_gpt.py`: `GPT` implementation using Codex app-server turns.
- Modify `backend/app/gpt/gpt_factory.py`: route by provider id/type.
- Test `backend/tests/test_codex_app_server_status.py`.
- Test `backend/tests/test_codex_app_server_client.py`.
- Test `backend/tests/test_codex_app_server_gpt.py`.
- Test `backend/tests/test_gpt_factory.py`.

Frontend files:

- Modify `BillNote_frontend/src/types/index.d.ts`: add Codex status type.
- Modify `BillNote_frontend/src/services/model.ts`: add Codex status API call.
- Modify `BillNote_frontend/src/components/Form/modelForm/ModelSelector.tsx`: support manual model entry for Codex provider.
- Modify `BillNote_frontend/src/components/Form/modelForm/Form.tsx`: special-case Codex provider form and status card.

---

### Task 1: Backend Codex Provider And Status

**Files:**

- Modify: `backend/app/db/builtin_providers.json`
- Modify: `backend/app/db/provider_dao.py`
- Create: `backend/app/services/codex_app_server.py`
- Create: `backend/app/routers/codex_app_server.py`
- Modify: `backend/app/__init__.py`
- Modify: `backend/app/services/model.py`
- Test: `backend/tests/test_codex_app_server_status.py`

- [ ] **Step 1: Write failing status tests**

Create `backend/tests/test_codex_app_server_status.py`:

```python
from pathlib import Path

from app.services.codex_app_server import (
    CODEX_PROVIDER_ID,
    CodexAppServerStatus,
    CodexAppServerStatusService,
)


def test_read_default_model_from_config(tmp_path: Path):
    codex_home = tmp_path / ".codex"
    codex_home.mkdir()
    (codex_home / "config.toml").write_text('model = "gpt-5.5"\n', encoding="utf-8")

    assert CodexAppServerStatusService.read_default_model(codex_home) == "gpt-5.5"


def test_missing_config_returns_none(tmp_path: Path):
    assert CodexAppServerStatusService.read_default_model(tmp_path / ".codex") is None


def test_status_ready_requires_cli_and_auth(monkeypatch, tmp_path: Path):
    codex_home = tmp_path / ".codex"
    codex_home.mkdir()
    (codex_home / "auth.json").write_text("{}", encoding="utf-8")
    (codex_home / "config.toml").write_text('model = "gpt-5.5"\n', encoding="utf-8")

    monkeypatch.setattr(CodexAppServerStatusService, "find_codex_bin", staticmethod(lambda: "codex"))
    monkeypatch.setattr(
        CodexAppServerStatusService,
        "read_codex_version",
        staticmethod(lambda _bin: "codex-cli 0.137.0"),
    )

    status = CodexAppServerStatusService.get_status(codex_home=codex_home)

    assert status == CodexAppServerStatus(
        codex_cli_available=True,
        codex_version="codex-cli 0.137.0",
        auth_available=True,
        default_model="gpt-5.5",
        ready=True,
    )


def test_status_not_ready_without_auth(monkeypatch, tmp_path: Path):
    codex_home = tmp_path / ".codex"
    codex_home.mkdir()

    monkeypatch.setattr(CodexAppServerStatusService, "find_codex_bin", staticmethod(lambda: "codex"))
    monkeypatch.setattr(
        CodexAppServerStatusService,
        "read_codex_version",
        staticmethod(lambda _bin: "codex-cli 0.137.0"),
    )

    status = CodexAppServerStatusService.get_status(codex_home=codex_home)

    assert status.codex_cli_available is True
    assert status.auth_available is False
    assert status.ready is False


def test_codex_provider_id_is_stable():
    assert CODEX_PROVIDER_ID == "codex_app_server"
```

- [ ] **Step 2: Run the failing status tests**

Run:

```powershell
cd backend
pytest tests/test_codex_app_server_status.py -q
```

Expected: FAIL with `ModuleNotFoundError: No module named 'app.services.codex_app_server'`.

- [ ] **Step 3: Add built-in provider metadata**

Modify `backend/app/db/builtin_providers.json` by appending this object before the closing `]`:

```json
  {
    "id": "codex_app_server",
    "name": "Codex App Server",
    "type": "codex_app_server",
    "logo": "OpenAI",
    "api_key": "",
    "base_url": "codex_app_server://local"
  }
```

Keep the file as valid JSON.

- [ ] **Step 4: Update built-in provider seeding**

Modify `backend/app/db/provider_dao.py` so `seed_default_providers()` inserts missing built-ins instead of returning when any provider exists. Replace the current `seed_default_providers()` body with:

```python
def seed_default_providers():
    db = next(get_db())
    try:
        json_path = get_builtin_providers_path()
        try:
            with open(json_path, 'r', encoding='utf-8') as f:
                providers = json.load(f)
        except Exception as e:
            logger.error(f"Failed to read builtin_providers.json: {e}")
            return

        inserted = 0
        for p in providers:
            existing = db.query(Provider).filter_by(id=p['id']).first()
            if existing:
                continue
            db.add(Provider(
                id=p['id'],
                name=p['name'],
                api_key=p['api_key'],
                base_url=p['base_url'],
                logo=p['logo'],
                type=p['type'],
                enabled=p.get('enabled', 1)
            ))
            inserted += 1

        if inserted:
            db.commit()
            logger.info(f"Default providers seeded successfully. inserted={inserted}")
        else:
            logger.info("Default providers already exist, skipping seed.")
    except Exception as e:
        logger.error(f"Failed to seed default providers: {e}")
    finally:
        db.close()
```

- [ ] **Step 5: Add Codex status service**

Create `backend/app/services/codex_app_server.py`:

```python
from __future__ import annotations

import os
import shutil
import subprocess
import tomllib
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional


CODEX_PROVIDER_ID = "codex_app_server"
CODEX_PROVIDER_TYPE = "codex_app_server"
CODEX_LOCAL_BASE_URL = "codex_app_server://local"


@dataclass(frozen=True)
class CodexAppServerStatus:
    codex_cli_available: bool
    codex_version: Optional[str]
    auth_available: bool
    default_model: Optional[str]
    ready: bool

    def to_dict(self) -> dict:
        return asdict(self)


class CodexAppServerStatusService:
    @staticmethod
    def default_codex_home() -> Path:
        return Path(os.getenv("CODEX_HOME") or Path.home() / ".codex")

    @staticmethod
    def find_codex_bin() -> Optional[str]:
        return shutil.which("codex") or shutil.which("codex.cmd")

    @staticmethod
    def read_codex_version(codex_bin: str) -> Optional[str]:
        try:
            completed = subprocess.run(
                [codex_bin, "--version"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except Exception:
            return None
        version = (completed.stdout or completed.stderr).strip()
        return version or None

    @staticmethod
    def read_default_model(codex_home: Path | None = None) -> Optional[str]:
        home = codex_home or CodexAppServerStatusService.default_codex_home()
        config_path = home / "config.toml"
        if not config_path.exists():
            return None
        try:
            with config_path.open("rb") as fh:
                config = tomllib.load(fh)
        except Exception:
            return None
        model = config.get("model")
        return model if isinstance(model, str) and model.strip() else None

    @staticmethod
    def auth_available(codex_home: Path | None = None) -> bool:
        home = codex_home or CodexAppServerStatusService.default_codex_home()
        return (home / "auth.json").exists()

    @staticmethod
    def get_status(codex_home: Path | None = None) -> CodexAppServerStatus:
        home = codex_home or CodexAppServerStatusService.default_codex_home()
        codex_bin = CodexAppServerStatusService.find_codex_bin()
        version = CodexAppServerStatusService.read_codex_version(codex_bin) if codex_bin else None
        auth = CodexAppServerStatusService.auth_available(home)
        default_model = CodexAppServerStatusService.read_default_model(home)
        return CodexAppServerStatus(
            codex_cli_available=codex_bin is not None,
            codex_version=version,
            auth_available=auth,
            default_model=default_model,
            ready=codex_bin is not None and auth,
        )

    @staticmethod
    def get_model_suggestions() -> list[dict]:
        model = CodexAppServerStatusService.read_default_model()
        if not model:
            return []
        return [{
            "id": model,
            "object": "model",
            "created": 0,
            "owned_by": "codex_app_server",
            "permission": [],
            "root": model,
        }]

    @staticmethod
    def assert_ready() -> None:
        status = CodexAppServerStatusService.get_status()
        if not status.codex_cli_available:
            raise RuntimeError("Codex CLI is not installed. Install it with: npm i -g @openai/codex")
        if not status.auth_available:
            raise RuntimeError("Codex CLI is not logged in. Run: codex login")
```

- [ ] **Step 6: Add Codex status router**

Create `backend/app/routers/codex_app_server.py`:

```python
from fastapi import APIRouter

from app.services.codex_app_server import CodexAppServerStatusService
from app.utils.response import ResponseWrapper as R

router = APIRouter()


@router.get("/codex_app_server/status")
def codex_app_server_status():
    return R.success(data=CodexAppServerStatusService.get_status().to_dict())
```

Modify `backend/app/__init__.py`:

```python
from .routers import note, provider, model, config, chat, codex_app_server
```

and register it inside `create_app()`:

```python
    app.include_router(codex_app_server.router, prefix="/api")
```

- [ ] **Step 7: Dispatch model list and connection test**

Modify `backend/app/services/model.py`.

Add imports:

```python
from app.services.codex_app_server import CODEX_PROVIDER_ID, CODEX_PROVIDER_TYPE, CodexAppServerStatusService
```

In `get_model_list()`, after provider lookup and before building `ModelConfig`, add:

```python
        if provider.get("id") == CODEX_PROVIDER_ID or provider.get("type") == CODEX_PROVIDER_TYPE:
            class _ModelList:
                def __init__(self, data):
                    self.data = data

            class _ModelItem:
                def __init__(self, payload):
                    self.payload = payload

                def dict(self):
                    return self.payload

            return _ModelList([
                _ModelItem(item)
                for item in CodexAppServerStatusService.get_model_suggestions()
            ])
```

In `connect_test()`, after provider lookup and before the API key check, add:

```python
        if provider.get("id") == CODEX_PROVIDER_ID or provider.get("type") == CODEX_PROVIDER_TYPE:
            CodexAppServerStatusService.assert_ready()
            return True
```

- [ ] **Step 8: Run status tests**

Run:

```powershell
cd backend
pytest tests/test_codex_app_server_status.py -q
```

Expected: PASS.

- [ ] **Step 9: Commit Task 1**

Run:

```powershell
git add backend/app/db/builtin_providers.json backend/app/db/provider_dao.py backend/app/services/codex_app_server.py backend/app/routers/codex_app_server.py backend/app/__init__.py backend/app/services/model.py backend/tests/test_codex_app_server_status.py
git commit -m "feat: add codex app-server provider status"
```

---

### Task 2: Codex App Server JSON-RPC Client

**Files:**

- Create: `backend/app/gpt/codex_app_server_client.py`
- Test: `backend/tests/test_codex_app_server_client.py`

- [ ] **Step 1: Write failing client parser tests**

Create `backend/tests/test_codex_app_server_client.py`:

```python
import pytest

from app.gpt.codex_app_server_client import (
    CodexAppServerClient,
    CodexAppServerError,
    CodexTurnState,
)


def test_handle_agent_message_delta_accumulates_text():
    state = CodexTurnState()

    CodexAppServerClient.handle_notification(
        {"method": "item/agentMessage/delta", "params": {"delta": "# Title"}},
        state,
    )
    CodexAppServerClient.handle_notification(
        {"method": "item/agentMessage/delta", "params": {"delta": "\nBody"}},
        state,
    )

    assert state.text == "# Title\nBody"


def test_completed_agent_message_replaces_empty_delta_text():
    state = CodexTurnState()

    CodexAppServerClient.handle_notification(
        {
            "method": "item/completed",
            "params": {
                "item": {
                    "type": "agentMessage",
                    "text": "# Complete",
                }
            },
        },
        state,
    )

    assert state.text == "# Complete"


def test_completed_agent_message_does_not_duplicate_delta_text():
    state = CodexTurnState(text="# Streamed")

    CodexAppServerClient.handle_notification(
        {
            "method": "item/completed",
            "params": {
                "item": {
                    "type": "agentMessage",
                    "text": "# Streamed",
                }
            },
        },
        state,
    )

    assert state.text == "# Streamed"


def test_turn_completed_marks_state_done():
    state = CodexTurnState()

    CodexAppServerClient.handle_notification(
        {
            "method": "turn/completed",
            "params": {
                "turn": {
                    "status": "completed",
                    "error": None,
                    "items": [],
                }
            },
        },
        state,
    )

    assert state.done is True
    assert state.error is None


def test_turn_failed_records_error():
    state = CodexTurnState()

    CodexAppServerClient.handle_notification(
        {
            "method": "turn/completed",
            "params": {
                "turn": {
                    "status": "failed",
                    "error": {"message": "model failed"},
                    "items": [],
                }
            },
        },
        state,
    )

    assert state.done is True
    assert state.error == "model failed"


def test_error_notification_records_error():
    state = CodexTurnState()

    CodexAppServerClient.handle_notification(
        {"method": "error", "params": {"error": {"message": "runtime failed"}}},
        state,
    )

    assert state.done is True
    assert state.error == "runtime failed"


def test_clean_markdown_rejects_empty_output():
    with pytest.raises(CodexAppServerError, match="empty Markdown"):
        CodexAppServerClient.clean_markdown("  \n ")


def test_clean_markdown_strips_code_fence():
    assert CodexAppServerClient.clean_markdown("```markdown\n# Title\n```") == "# Title"
```

- [ ] **Step 2: Run failing client tests**

Run:

```powershell
cd backend
pytest tests/test_codex_app_server_client.py -q
```

Expected: FAIL with `ModuleNotFoundError: No module named 'app.gpt.codex_app_server_client'`.

- [ ] **Step 3: Implement client and event parser**

Create `backend/app/gpt/codex_app_server_client.py`:

```python
from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from app.services.codex_app_server import CodexAppServerStatusService


class CodexAppServerError(RuntimeError):
    pass


@dataclass
class CodexTurnState:
    text: str = ""
    done: bool = False
    error: Optional[str] = None


class CodexAppServerClient:
    def __init__(self, codex_bin: Optional[str] = None, timeout_seconds: int = 600):
        self.codex_bin = codex_bin or CodexAppServerStatusService.find_codex_bin()
        self.timeout_seconds = timeout_seconds
        if not self.codex_bin:
            raise CodexAppServerError(
                "Codex CLI is not installed. Install it with: npm i -g @openai/codex"
            )

    @staticmethod
    def clean_markdown(text: str) -> str:
        cleaned = text.strip()
        if cleaned.startswith("```markdown"):
            cleaned = cleaned.removeprefix("```markdown").strip()
            if cleaned.endswith("```"):
                cleaned = cleaned[:-3].strip()
        elif cleaned.startswith("```"):
            cleaned = cleaned.removeprefix("```").strip()
            if cleaned.endswith("```"):
                cleaned = cleaned[:-3].strip()
        if not cleaned:
            raise CodexAppServerError("Codex app-server returned empty Markdown output")
        return cleaned

    @staticmethod
    def _error_message(error_payload) -> str:
        if isinstance(error_payload, dict):
            message = error_payload.get("message")
            if isinstance(message, str) and message:
                return message
        return str(error_payload)

    @staticmethod
    def handle_notification(message: dict, state: CodexTurnState) -> None:
        method = message.get("method")
        params = message.get("params") or {}

        if method == "item/agentMessage/delta":
            delta = params.get("delta")
            if isinstance(delta, str):
                state.text += delta
            return

        if method == "item/completed":
            item = params.get("item") or {}
            if item.get("type") == "agentMessage" and not state.text:
                text = item.get("text")
                if isinstance(text, str):
                    state.text = text
            return

        if method == "turn/completed":
            turn = params.get("turn") or {}
            state.done = True
            if turn.get("status") == "failed":
                state.error = CodexAppServerClient._error_message(turn.get("error"))
            return

        if method == "error":
            state.done = True
            state.error = CodexAppServerClient._error_message(params.get("error"))

    def run_markdown_turn(self, prompt: str, model: str, cwd: Optional[str] = None) -> str:
        CodexAppServerStatusService.assert_ready()
        process = self._start_process()
        try:
            stdout_queue: queue.Queue[dict] = queue.Queue()
            stderr_lines: list[str] = []
            self._start_reader_threads(process, stdout_queue, stderr_lines)

            self._request(
                process,
                stdout_queue,
                1,
                "initialize",
                {
                    "clientInfo": {
                        "name": "bilinote",
                        "title": "BiliNote",
                        "version": "0.0.0",
                    },
                    "capabilities": {
                        "experimentalApi": True,
                        "requestAttestation": False,
                    },
                },
            )
            thread_response = self._request(
                process,
                stdout_queue,
                2,
                "thread/start",
                {
                    "model": model,
                    "cwd": cwd or str(Path.cwd()),
                    "approvalPolicy": "never",
                    "sandbox": "read-only",
                    "ephemeral": True,
                    "baseInstructions": (
                        "You are BiliNote's Markdown note generation backend. "
                        "Only output the requested Markdown. Do not call tools, "
                        "run commands, inspect files, or modify files."
                    ),
                },
            )
            thread_id = thread_response["result"]["thread"]["id"]
            self._send(
                process,
                {
                    "id": 3,
                    "method": "turn/start",
                    "params": {
                        "threadId": thread_id,
                        "input": [{
                            "type": "text",
                            "text": prompt,
                            "text_elements": [],
                        }],
                        "approvalPolicy": "never",
                        "model": model,
                    },
                },
            )
            state = self._read_turn(process, stdout_queue)
            if state.error:
                raise CodexAppServerError(state.error)
            return self.clean_markdown(state.text)
        finally:
            self._stop_process(process)

    def _start_process(self) -> subprocess.Popen:
        return subprocess.Popen(
            [self.codex_bin, "app-server", "--stdio"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            encoding="utf-8",
            errors="replace",
        )

    @staticmethod
    def _start_reader_threads(process: subprocess.Popen, stdout_queue: queue.Queue, stderr_lines: list[str]) -> None:
        def read_stdout():
            assert process.stdout is not None
            for line in process.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    stdout_queue.put(json.loads(line))
                except json.JSONDecodeError:
                    stdout_queue.put({"method": "error", "params": {"error": {"message": line}}})

        def read_stderr():
            assert process.stderr is not None
            for line in process.stderr:
                if line.strip():
                    stderr_lines.append(line.strip())

        threading.Thread(target=read_stdout, daemon=True).start()
        threading.Thread(target=read_stderr, daemon=True).start()

    @staticmethod
    def _send(process: subprocess.Popen, payload: dict) -> None:
        if process.stdin is None:
            raise CodexAppServerError("Codex app-server stdin is closed")
        process.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
        process.stdin.flush()

    def _request(
        self,
        process: subprocess.Popen,
        stdout_queue: queue.Queue,
        request_id: int,
        method: str,
        params: dict,
    ) -> dict:
        self._send(process, {"id": request_id, "method": method, "params": params})
        deadline = time.monotonic() + self.timeout_seconds
        while time.monotonic() < deadline:
            remaining = max(0.1, deadline - time.monotonic())
            try:
                message = stdout_queue.get(timeout=min(1.0, remaining))
            except queue.Empty:
                if process.poll() is not None:
                    raise CodexAppServerError(f"Codex app-server exited while waiting for {method}")
                continue
            if message.get("id") == request_id:
                if "error" in message:
                    raise CodexAppServerError(self._error_message(message["error"]))
                return message
        raise CodexAppServerError(f"Codex app-server timed out during {method}")

    def _read_turn(self, process: subprocess.Popen, stdout_queue: queue.Queue) -> CodexTurnState:
        state = CodexTurnState()
        deadline = time.monotonic() + self.timeout_seconds
        while time.monotonic() < deadline:
            remaining = max(0.1, deadline - time.monotonic())
            try:
                message = stdout_queue.get(timeout=min(1.0, remaining))
            except queue.Empty:
                if process.poll() is not None:
                    raise CodexAppServerError("Codex app-server exited during turn")
                continue
            if message.get("id") == 3 and "error" in message:
                raise CodexAppServerError(self._error_message(message["error"]))
            self.handle_notification(message, state)
            if state.done:
                return state
        raise CodexAppServerError("Codex app-server turn timed out")

    @staticmethod
    def _stop_process(process: subprocess.Popen) -> None:
        if process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
```

- [ ] **Step 4: Run client tests**

Run:

```powershell
cd backend
pytest tests/test_codex_app_server_client.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit Task 2**

Run:

```powershell
git add backend/app/gpt/codex_app_server_client.py backend/tests/test_codex_app_server_client.py
git commit -m "feat: add codex app-server json rpc client"
```

---

### Task 3: Codex GPT Adapter And Factory Routing

**Files:**

- Create: `backend/app/gpt/codex_app_server_gpt.py`
- Modify: `backend/app/gpt/gpt_factory.py`
- Test: `backend/tests/test_codex_app_server_gpt.py`
- Test: `backend/tests/test_gpt_factory.py`

- [ ] **Step 1: Write failing adapter tests**

Create `backend/tests/test_codex_app_server_gpt.py`:

```python
import pytest

from app.gpt.codex_app_server_gpt import CodexAppServerGPT
from app.gpt.codex_app_server_client import CodexAppServerError
from app.models.gpt_model import GPTSource
from app.models.transcriber_model import TranscriptSegment


class FakeClient:
    def __init__(self):
        self.prompts = []

    def run_markdown_turn(self, prompt: str, model: str, cwd=None) -> str:
        self.prompts.append((prompt, model, cwd))
        return "# Generated Note"


def test_summarize_calls_codex_client_with_bilinote_prompt(monkeypatch):
    fake_client = FakeClient()
    gpt = CodexAppServerGPT(model="gpt-5.5", client=fake_client)
    source = GPTSource(
        title="Video Title",
        segment=[TranscriptSegment(start=0, end=3, text="hello world")],
        tags=[],
        _format=["toc", "link"],
    )

    markdown = gpt.summarize(source)

    assert markdown == "# Generated Note"
    prompt, model, _cwd = fake_client.prompts[0]
    assert model == "gpt-5.5"
    assert "Video Title" in prompt
    assert "hello world" in prompt
    assert "Only output Markdown" in prompt


def test_video_image_inputs_fail_with_clear_error():
    gpt = CodexAppServerGPT(model="gpt-5.5", client=FakeClient())
    source = GPTSource(
        title="Video Title",
        segment=[TranscriptSegment(start=0, end=3, text="hello world")],
        tags=[],
        video_img_urls=["https://example.com/a.jpg"],
    )

    with pytest.raises(CodexAppServerError, match="Video image inputs"):
        gpt.summarize(source)
```

Create `backend/tests/test_gpt_factory.py`:

```python
from app.gpt.codex_app_server_gpt import CodexAppServerGPT
from app.gpt.gpt_factory import GPTFactory
from app.gpt.universal_gpt import UniversalGPT
from app.models.model_config import ModelConfig


def test_factory_returns_codex_app_server_gpt():
    config = ModelConfig(
        name="Codex App Server",
        provider="codex_app_server",
        api_key="",
        base_url="codex_app_server://local",
        model_name="gpt-5.5",
    )

    gpt = GPTFactory.from_config(config)

    assert isinstance(gpt, CodexAppServerGPT)


def test_factory_keeps_openai_compatible_route(monkeypatch):
    class FakeProvider:
        @property
        def get_client(self):
            return object()

    monkeypatch.setattr(
        "app.gpt.gpt_factory.OpenAICompatibleProvider",
        lambda api_key, base_url: FakeProvider(),
    )
    config = ModelConfig(
        name="OpenAI",
        provider="built-in",
        api_key="key",
        base_url="https://api.openai.com/v1",
        model_name="gpt-4o-mini",
    )

    gpt = GPTFactory.from_config(config)

    assert isinstance(gpt, UniversalGPT)
```

- [ ] **Step 2: Run failing adapter tests**

Run:

```powershell
cd backend
pytest tests/test_codex_app_server_gpt.py tests/test_gpt_factory.py -q
```

Expected: FAIL with missing `codex_app_server_gpt`.

- [ ] **Step 3: Implement Codex GPT adapter**

Create `backend/app/gpt/codex_app_server_gpt.py`:

```python
from __future__ import annotations

from pathlib import Path
from typing import Optional

from app.gpt.codex_app_server_client import CodexAppServerClient, CodexAppServerError
from app.gpt.prompt import MERGE_PROMPT
from app.gpt.prompt_builder import generate_base_prompt
from app.gpt.request_chunker import RequestChunker
from app.gpt.universal_gpt import UniversalGPT
from app.models.gpt_model import GPTSource


class CodexAppServerGPT(UniversalGPT):
    def __init__(
        self,
        model: str,
        client: Optional[CodexAppServerClient] = None,
        cwd: Optional[str] = None,
    ):
        super().__init__(client=None, model=model)
        self.codex_client = client or CodexAppServerClient()
        self.cwd = cwd or str(Path.cwd())

    def list_models(self):
        return []

    def create_messages(self, segments: list, **kwargs):
        if kwargs.get("video_img_urls"):
            raise CodexAppServerError(
                "Video image inputs are not supported by the Codex App Server backend in this version."
            )
        content_text = generate_base_prompt(
            title=kwargs.get("title"),
            segment_text=self._build_segment_text(segments),
            tags=kwargs.get("tags"),
            _format=kwargs.get("_format"),
            style=kwargs.get("style"),
            extras=kwargs.get("extras"),
        )
        prompt = (
            "You are BiliNote's note generation backend.\n"
            "Only output Markdown.\n"
            "Do not call tools, run shell commands, inspect files, or modify files.\n\n"
            f"{content_text}"
        )
        return [{"role": "user", "content": prompt}]

    def _build_merge_messages(self, partials: list) -> list:
        merge_text = (
            "You are BiliNote's note merge backend.\n"
            "Only output the final merged Markdown note.\n"
            "Do not call tools, run shell commands, inspect files, or modify files.\n\n"
            + MERGE_PROMPT
            + "\n\n"
            + "\n\n---\n\n".join(partials)
        )
        return [{"role": "user", "content": merge_text}]

    @staticmethod
    def _messages_to_prompt(messages: list) -> str:
        if not messages:
            raise CodexAppServerError("No messages were provided to Codex App Server")
        content = messages[0].get("content")
        if isinstance(content, str):
            return content
        raise CodexAppServerError("Codex App Server backend only supports text prompts")

    def _chat_completion_create(self, messages: list):
        prompt = self._messages_to_prompt(messages)
        markdown = self.codex_client.run_markdown_turn(prompt, self.model, cwd=self.cwd)

        class _Message:
            def __init__(self, content: str):
                self.content = content

        class _Choice:
            def __init__(self, content: str):
                self.message = _Message(content)

        class _Response:
            def __init__(self, content: str):
                self.choices = [_Choice(content)]

        return _Response(markdown)

    def summarize(self, source: GPTSource) -> str:
        if source.video_img_urls:
            raise CodexAppServerError(
                "Video image inputs are not supported by the Codex App Server backend in this version."
            )
        return super().summarize(source)
```

- [ ] **Step 4: Route Codex provider in GPTFactory**

Modify `backend/app/gpt/gpt_factory.py`:

```python
from app.gpt.base import GPT
from app.gpt.codex_app_server_gpt import CodexAppServerGPT
from app.gpt.provider.OpenAI_compatible_provider import OpenAICompatibleProvider
from app.gpt.universal_gpt import UniversalGPT
from app.models.model_config import ModelConfig
from app.services.codex_app_server import CODEX_PROVIDER_ID, CODEX_PROVIDER_TYPE


class GPTFactory:
    @staticmethod
    def from_config(config: ModelConfig) -> GPT:
        if config.provider in {CODEX_PROVIDER_ID, CODEX_PROVIDER_TYPE} or config.name == "Codex App Server":
            return CodexAppServerGPT(model=config.model_name)
        client = OpenAICompatibleProvider(api_key=config.api_key, base_url=config.base_url).get_client
        return UniversalGPT(client=client, model=config.model_name)
```

- [ ] **Step 5: Run adapter and existing GPT tests**

Run:

```powershell
cd backend
pytest tests/test_codex_app_server_gpt.py tests/test_gpt_factory.py tests/test_universal_gpt_content_format.py tests/test_universal_gpt_checkpoint.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit Task 3**

Run:

```powershell
git add backend/app/gpt/codex_app_server_gpt.py backend/app/gpt/gpt_factory.py backend/tests/test_codex_app_server_gpt.py backend/tests/test_gpt_factory.py
git commit -m "feat: route note generation through codex app-server"
```

---

### Task 4: Frontend Codex Provider Configuration

**Files:**

- Modify: `BillNote_frontend/src/types/index.d.ts`
- Modify: `BillNote_frontend/src/services/model.ts`
- Modify: `BillNote_frontend/src/components/Form/modelForm/ModelSelector.tsx`
- Modify: `BillNote_frontend/src/components/Form/modelForm/Form.tsx`

- [ ] **Step 1: Add Codex status type**

Modify `BillNote_frontend/src/types/index.d.ts`:

```ts
export interface ICodexAppServerStatus {
  codex_cli_available: boolean
  codex_version: string | null
  auth_available: boolean
  default_model: string | null
  ready: boolean
}
```

- [ ] **Step 2: Add Codex status API service**

Modify `BillNote_frontend/src/services/model.ts`:

```ts
import { ICodexAppServerStatus } from '@/types'
```

Add:

```ts
export const getCodexAppServerStatus = async (): Promise<ICodexAppServerStatus> => {
  return await request.get('/codex_app_server/status', cfg({ silent: true }))
}
```

- [ ] **Step 3: Make ModelSelector support manual Codex model entry**

Modify `BillNote_frontend/src/components/Form/modelForm/ModelSelector.tsx`.

Change props:

```ts
interface ModelSelectorProps {
  providerId: string
  manualOnly?: boolean
  defaultModel?: string | null
  onSaved?: () => Promise<void> | void
}
```

Change function signature:

```ts
export function ModelSelector({ providerId, manualOnly = false, defaultModel, onSaved }: ModelSelectorProps) {
```

Add this effect after `useState` declarations:

```ts
  useEffect(() => {
    if (manualOnly && defaultModel && !selectedModel) {
      setSelectedModel(defaultModel)
    }
  }, [manualOnly, defaultModel, selectedModel, setSelectedModel])
```

Change the existing load effect:

```ts
  useEffect(() => {
    if (providerId && !manualOnly) {
      loadModels(providerId)
    }
  }, [providerId, manualOnly, loadModels])
```

Inside `handleSubmit()`, after `await addNewModel(providerId, selectedModel)`, add:

```ts
      await onSaved?.()
```

Replace the selector block with conditional manual input:

```tsx
      {manualOnly ? (
        <Input
          value={selectedModel}
          onChange={e => setSelectedModel(e.target.value)}
          placeholder="例如 gpt-5.5"
          className="w-[300px]"
        />
      ) : (
        <Select value={selectedModel} onValueChange={setSelectedModel}>
          <SelectTrigger className="w-[300px]">
            <SelectValue placeholder="请选择模型" />
          </SelectTrigger>
          <SelectContent>
            <div className="p-2">
              <Input
                placeholder="搜索模型..."
                value={search}
                onChange={e => setSearch(e.target.value)}
                className="h-8"
              />
            </div>
            {filteredModels.map((model, index) => (
              <SelectItem key={`${model.id}-${index}`} value={model.id}>
                {model.id}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      )}
```

Only render the refresh button for non-manual providers:

```tsx
        {!manualOnly && (
          <Button
            variant="ghost"
            type="button"
            onClick={() => loadModels(providerId)}
            disabled={loading}
          >
            {loading ? '加载中...' : '刷新模型'}
          </Button>
        )}
```

- [ ] **Step 4: Special-case Codex provider form**

Modify `BillNote_frontend/src/components/Form/modelForm/Form.tsx`.

Change the existing service import from `@/services/model.ts` to include `getCodexAppServerStatus`:

```ts
import { testConnection, fetchModels, deleteModelById, getCodexAppServerStatus } from '@/services/model.ts'
```

Add the type import:

```ts
import type { ICodexAppServerStatus } from '@/types'
```

Add state near existing state declarations:

```ts
  const [codexStatus, setCodexStatus] = useState<ICodexAppServerStatus | null>(null)
```

Add derived value after form creation:

```ts
  const providerType = providerForm.watch('type')
  const isCodexProvider = id === 'codex_app_server' || providerType === 'codex_app_server'
```

Add loader:

```ts
  const refreshCodexStatus = async () => {
    if (!isCodexProvider) return
    try {
      const status = await getCodexAppServerStatus()
      setCodexStatus(status)
    } catch {
      setCodexStatus(null)
    }
  }
```

Add effect:

```ts
  useEffect(() => {
    refreshCodexStatus()
  }, [isCodexProvider])
```

Change `handleTest()` first lines:

```ts
    if (isCodexProvider) {
      try {
        setTesting(true)
        const status = await getCodexAppServerStatus()
        setCodexStatus(status)
        if (status.ready) {
          toast.success('Codex App Server 可用')
        } else if (!status.codex_cli_available) {
          toast.error('未检测到 Codex CLI，请先安装 npm i -g @openai/codex')
        } else if (!status.auth_available) {
          toast.error('Codex CLI 未登录，请先运行 codex login')
        } else {
          toast.error('Codex App Server 暂不可用')
        }
      } finally {
        setTesting(false)
      }
      return
    }
```

Hide API Key and Base URL fields for Codex by wrapping those `FormField` blocks:

```tsx
          {!isCodexProvider && (
            <>
              {/* existing API Key FormField */}
              {/* existing Base URL FormField */}
            </>
          )}
```

Render a Codex status card where those fields were hidden:

```tsx
          {isCodexProvider && (
            <div className="flex flex-col gap-2 rounded border p-3 text-sm">
              <div className="font-medium">本地 Codex Runtime</div>
              <div>CLI：{codexStatus?.codex_cli_available ? '已安装' : '未检测到'}</div>
              <div>登录：{codexStatus?.auth_available ? '已登录' : '未登录'}</div>
              <div>版本：{codexStatus?.codex_version || '-'}</div>
              <div>默认模型：{codexStatus?.default_model || '-'}</div>
              <Button type="button" onClick={handleTest} variant="ghost" disabled={testing}>
                {testing ? '测试中...' : '测试 Codex 连接'}
              </Button>
            </div>
          )}
```

Change `ModelSelector` usage:

```tsx
          <ModelSelector
            providerId={id!}
            manualOnly={isCodexProvider}
            defaultModel={codexStatus?.default_model}
            onSaved={async () => {
              const nextModels = await loadModelsById(id!)
              setModels(nextModels)
            }}
          />
```

- [ ] **Step 5: Run frontend verification**

Run:

```powershell
cd BillNote_frontend
pnpm lint
pnpm build
```

Expected: both commands complete successfully.

- [ ] **Step 6: Commit Task 4**

Run:

```powershell
git add BillNote_frontend/src/types/index.d.ts BillNote_frontend/src/services/model.ts BillNote_frontend/src/components/Form/modelForm/ModelSelector.tsx BillNote_frontend/src/components/Form/modelForm/Form.tsx
git commit -m "feat: configure codex app-server provider in settings"
```

---

### Task 5: End-To-End Verification

**Files:**

- No new source files.
- Uses existing backend and frontend.

- [ ] **Step 1: Run focused backend tests**

Run:

```powershell
cd backend
pytest tests/test_codex_app_server_status.py tests/test_codex_app_server_client.py tests/test_codex_app_server_gpt.py tests/test_gpt_factory.py -q
```

Expected: PASS.

- [ ] **Step 2: Run existing regression tests around note post-processing and GPT chunking**

Run:

```powershell
cd backend
pytest tests/test_note_helper.py tests/test_screenshot_marker.py tests/test_request_chunker.py tests/test_universal_gpt_content_format.py tests/test_universal_gpt_checkpoint.py -q
```

Expected: PASS.

- [ ] **Step 3: Verify Codex provider is seeded in the local DB**

Restart backend or run the startup seed path, then run:

```powershell
cd backend
@'
from app.db.provider_dao import seed_default_providers, get_provider_by_id
seed_default_providers()
provider = get_provider_by_id("codex_app_server")
print(provider.id, provider.name, provider.type, provider.base_url)
'@ | python -
```

Expected output contains:

```text
codex_app_server Codex App Server codex_app_server codex_app_server://local
```

- [ ] **Step 4: Verify Codex status endpoint**

With backend running, run:

```powershell
Invoke-RestMethod http://localhost:8483/api/codex_app_server/status
```

Expected response has `code = 0` and `data.ready = true` when `codex login` has been completed. If `data.ready = false`, the response should identify whether CLI or auth is missing.

- [ ] **Step 5: Add a Codex model in the UI**

Open:

```text
http://localhost:3015/settings/model/codex_app_server
```

Expected:

- API Key field is hidden.
- Base URL field is hidden.
- Codex status card is visible.
- Default model from `%USERPROFILE%\.codex\config.toml` is shown when present.
- Saving `gpt-5.5` adds it to the enabled model list.

- [ ] **Step 6: Generate a short note with Codex selected**

Use an existing short BiliNote task or a short video link.

Expected:

- The home page model selector includes the saved Codex model.
- The note task completes.
- Markdown is written under `backend/note_results`.
- Existing RAG indexing still runs after note save.
- If Codex fails, the task shows a clear error instead of saving empty Markdown.

- [ ] **Step 7: Run final project checks**

Run:

```powershell
cd backend
pytest -q
cd ..\BillNote_frontend
pnpm lint
pnpm build
```

Expected: all checks pass.

- [ ] **Step 8: Commit verification adjustments if needed**

If verification required small fixes, commit only those fixes:

```powershell
git status --short
git add <changed-files>
git commit -m "fix: stabilize codex app-server integration"
```

If no fixes were required, do not create an empty commit.

---

## Self-Review Checklist

- Spec coverage:
  - Provider configuration: Task 1 and Task 4.
  - Local Codex status checks: Task 1 and Task 5.
  - JSON-RPC stdio client: Task 2.
  - GPT-layer integration only: Task 3.
  - Existing pipeline preservation: Task 3 and Task 5.
  - Frontend settings UX: Task 4.
  - Clear error cases: Task 1, Task 2, Task 3, and Task 5.

- Type consistency:
  - Provider id/type is always `codex_app_server`.
  - Backend endpoint is `/api/codex_app_server/status`.
  - Frontend status interface is `ICodexAppServerStatus`.
  - GPT adapter class is `CodexAppServerGPT`.
  - JSON-RPC client class is `CodexAppServerClient`.

- Transport note:
  - The local probe confirmed `codex app-server --stdio` accepts newline-delimited JSON-RPC messages and responds with newline-delimited JSON.
