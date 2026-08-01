from __future__ import annotations

from app.db.models.providers import Provider
from app.services.provider import ProviderService


def test_get_all_providers_safe_never_returns_the_plaintext_api_key(
    monkeypatch,
) -> None:
    secret = "provider-service-secret-canary-8172"
    provider = Provider(
        id="provider-1",
        name="Provider",
        logo="custom",
        type="custom",
        api_key=secret,
        base_url="https://provider.example/v1",
        enabled=1,
    )
    monkeypatch.setattr(
        "app.services.provider.get_all_providers",
        lambda: [provider],
    )

    result = ProviderService.get_all_providers_safe()

    assert result[0]["api_key"] == ProviderService.mask_key(secret)
    assert secret not in repr(result)
