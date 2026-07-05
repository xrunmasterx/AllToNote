from pathlib import Path
import sys


BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.gpt.codex_app_server_gpt import CodexAppServerGPT
from app.services.chat_service import chat
from app.services.note import NoteGenerator


def test_chat_rejects_codex_provider_before_gpt_factory(monkeypatch):
    monkeypatch.setattr(
        "app.services.chat_service.VectorStoreManager",
        lambda: (_ for _ in ()).throw(AssertionError("vector store should not be called")),
    )
    monkeypatch.setattr(
        "app.services.chat_service.ProviderService.get_provider_by_id",
        staticmethod(
            lambda _id: {
                "id": "codex_app_server",
                "name": "Codex App Server",
                "type": "codex_app_server",
                "api_key": "",
                "base_url": "codex-app-server://local",
            }
        ),
    )
    monkeypatch.setattr(
        "app.services.chat_service.GPTFactory.from_config",
        staticmethod(lambda _config: (_ for _ in ()).throw(AssertionError("GPT factory should not be called"))),
    )

    try:
        chat(
            task_id="task-1",
            question="hello",
            history=[],
            provider_id="codex_app_server",
            model_name="gpt-5.5",
        )
    except ValueError as exc:
        assert "note generation only" in str(exc)
    else:
        raise AssertionError("Expected Codex chat guard to reject")


def test_note_generation_rejects_codex_images_before_download(monkeypatch):
    class _FakeDownloader:
        def download_subtitles(self, video_url):
            raise AssertionError("subtitle lookup should not run")

        def download_video(self, video_url):
            raise AssertionError("video download should not run")

        def download(self, **kwargs):
            raise AssertionError("media download should not run")

    generator = NoteGenerator.__new__(NoteGenerator)
    generator.video_img_urls = []
    generator.video_path = None

    monkeypatch.setattr(generator, "_update_status", lambda *args, **kwargs: None)
    monkeypatch.setattr(generator, "_get_downloader", lambda _platform: _FakeDownloader())
    monkeypatch.setattr(
        generator,
        "_get_gpt",
        lambda _model_name, _provider_id: CodexAppServerGPT(model="gpt-5.5"),
    )

    result = generator.generate(
        video_url="https://example.com/video",
        platform="bilibili",
        task_id="task-1",
        model_name="gpt-5.5",
        provider_id="codex_app_server",
        screenshot=True,
    )

    assert result is None
