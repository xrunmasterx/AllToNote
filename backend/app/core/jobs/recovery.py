from __future__ import annotations

from dataclasses import dataclass

from app.core.ports.jobs import (
    AttemptMetadataRepositoryPort,
    AttemptStoragePort,
)


@dataclass(frozen=True)
class ArtifactStepDescriptor:
    checkpoint_step_id: str
    pending_step: str
    schema_id: str
    input_hash: str


@dataclass(frozen=True)
class RecoveryPlan:
    reusable_checkpoint_steps: tuple[str, ...]
    pending_steps: tuple[str, ...]


class RecoveryPlanner:
    def __init__(
        self,
        metadata_repository: AttemptMetadataRepositoryPort,
        attempt_storage: AttemptStoragePort,
    ) -> None:
        self._metadata_repository = metadata_repository
        self._attempt_storage = attempt_storage

    def plan_remaining_steps(
        self,
        job_id: str,
        steps: tuple[ArtifactStepDescriptor, ...],
    ) -> RecoveryPlan:
        reusable: list[str] = []
        for index, step in enumerate(steps):
            metadata = self._metadata_repository.latest_checkpoint(
                job_id, step.checkpoint_step_id
            )
            if metadata is None or not self._attempt_storage.validate_checkpoint(
                metadata,
                expected_schema_id=step.schema_id,
                expected_input_hash=step.input_hash,
            ):
                return RecoveryPlan(
                    reusable_checkpoint_steps=tuple(reusable),
                    pending_steps=tuple(item.pending_step for item in steps[index:]),
                )
            reusable.append(step.checkpoint_step_id)
        return RecoveryPlan(tuple(reusable), ())


__all__ = ["ArtifactStepDescriptor", "RecoveryPlan", "RecoveryPlanner"]
