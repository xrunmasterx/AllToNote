from __future__ import annotations

from dataclasses import dataclass


JOB_PACK_ENVIRONMENT_EVENT = "execution.pack-environment.v1"


@dataclass(frozen=True, slots=True)
class ExecutionPackIdentity:
    pack_id: str
    pack_version: str
    platform: str
    manifest_sha256: str

    def __post_init__(self) -> None:
        if (
            any(
                type(value) is not str or not value.strip()
                for value in (
                    self.pack_id,
                    self.pack_version,
                    self.platform,
                )
            )
            or type(self.manifest_sha256) is not str
            or len(self.manifest_sha256) != 71
            or not self.manifest_sha256.startswith("sha256:")
            or any(
                character not in "0123456789abcdef"
                for character in self.manifest_sha256[7:]
            )
        ):
            raise ValueError("Execution Pack identity is invalid")


@dataclass(frozen=True, slots=True)
class JobPackEnvironmentSnapshot:
    schema_version: int
    packs: tuple[ExecutionPackIdentity, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "packs", tuple(self.packs))
        if (
            self.schema_version != 1
            or not self.packs
            or any(
                not isinstance(pack, ExecutionPackIdentity)
                for pack in self.packs
            )
            or len({pack.pack_id for pack in self.packs}) != len(self.packs)
        ):
            raise ValueError("Job Pack environment snapshot is invalid")

    def pack(self, pack_id: str) -> ExecutionPackIdentity:
        for pack in self.packs:
            if pack.pack_id == pack_id:
                return pack
        raise ValueError("Required execution Pack is not frozen")


__all__ = [
    "ExecutionPackIdentity",
    "JOB_PACK_ENVIRONMENT_EVENT",
    "JobPackEnvironmentSnapshot",
]
