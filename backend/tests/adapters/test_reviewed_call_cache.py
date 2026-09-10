from concurrent.futures import ThreadPoolExecutor

from app.adapters.models.reviewed_call_cache import ReviewedCallCache
from app.core.domain.ids import sha256_digest
from app.core.ports.model_executor import ModelExecutionResult, ModelFinishReason


def test_atomic_cache_roundtrip_corruption_and_concurrent_publish(tmp_path):
    cache = ReviewedCallCache(tmp_path)
    key = sha256_digest(b"scope")
    request_hash = sha256_digest(b"request")
    result = ModelExecutionResult(text="approved", actual_model_identity="fixture/model",
        finish_reason=ModelFinishReason.STOP, input_tokens=1, output_tokens=2, provider_request_id=None)
    assert cache.load(key) == {}
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _: cache.save(key, {request_hash: result}), range(16)))
    assert cache.load(key) == {request_hash: result}
    path = next(tmp_path.glob("*.json"))
    path.write_bytes(path.read_bytes().replace(b"approved", b"modified"))
    assert cache.load(key) == {}
    assert cache.load("../escape") == {}
