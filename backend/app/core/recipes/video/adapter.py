from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

from app.core.application.video_service import VideoService
from app.core.config.model import JobConfigSnapshot
from app.core.domain.video import (
    FaithfulLanguagePolicy,
    ResolvedVideoOutput,
    ScreenshotPolicy,
    TranscriptDocument,
    TranscriptSegment,
    VideoDocumentKind,
    VideoProduceRequest,
)
from app.core.errors import DomainError, ErrorCategory
from app.core.jobs.model import JobExecutionOwner
from app.core.recipes.contracts import (
    InputDescriptor,
    ProduceRequest,
    ProduceSubmission,
    RecipeKey,
)


_SUPPORTED_KEYS = {
    RecipeKey("alltonote.video-course-note", 1): 1,
    RecipeKey("alltonote.video-producer", 2): 2,
}
_ALLOWED_PARAMETERS = frozenset(
    {
        "provider_profile",
        "model_override",
        "transcriber_profile",
        "output_language",
        "quality_preset",
        "faithful_language_policy",
        "style",
        "screenshot_policy",
        "provided_transcript",
        "resolved_outputs",
        "config_snapshot",
    }
)


def _invalid(code: str, message: str) -> DomainError:
    return DomainError(code, ErrorCategory.INVALID_REQUEST, message)


def _optional_text(parameters: Mapping[str, object], key: str, default: str) -> str:
    value = parameters.get(key, default)
    if type(value) is not str or not value.strip():
        raise _invalid("video_recipe_parameters_invalid", f"{key} must be text")
    return value


def _model_override(parameters: Mapping[str, object]) -> str | None:
    value = parameters.get("model_override")
    if value is not None and (type(value) is not str or not value.strip()):
        raise _invalid("video_recipe_parameters_invalid", "model_override must be text or null")
    return value


def _transcript(value: object) -> TranscriptDocument | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise _invalid("video_recipe_parameters_invalid", "provided_transcript must be an object")
    try:
        segments = tuple(
            TranscriptSegment(
                segment["segment_id"],
                segment["start_ms"],
                segment["end_ms"],
                segment["text"],
            )
            for segment in value["segments"]
        )
        return TranscriptDocument(value["language"], segments)
    except (KeyError, TypeError):
        raise _invalid("video_recipe_parameters_invalid", "provided_transcript is invalid") from None


def _resolved_outputs(value: object) -> tuple[ResolvedVideoOutput, ...] | None:
    if value is None:
        return None
    if not isinstance(value, tuple):
        raise _invalid("video_recipe_parameters_invalid", "resolved_outputs must be a list")
    try:
        return tuple(
            ResolvedVideoOutput(
                VideoDocumentKind(item["document_kind"]),
                item["recipe_id"],
                item["recipe_version"],
                item["quality_preset"],
            )
            for item in value
        )
    except (KeyError, TypeError, ValueError):
        raise _invalid("video_recipe_parameters_invalid", "resolved_outputs are invalid") from None


def _thaw_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


def _transcript_parameters(transcript: TranscriptDocument | None) -> object:
    if transcript is None:
        return None
    return {
        "language": transcript.language,
        "segments": [
            {
                "segment_id": segment.segment_id,
                "start_ms": segment.start_ms,
                "end_ms": segment.end_ms,
                "text": segment.text,
            }
            for segment in transcript.segments
        ],
    }


def _resolved_output_parameters(
    outputs: tuple[ResolvedVideoOutput, ...] | None,
) -> object:
    if outputs is None:
        return None
    return [
        {
            "document_kind": output.document_kind.value,
            "recipe_id": output.recipe_id,
            "recipe_version": output.recipe_version,
            "quality_preset": output.quality_preset,
        }
        for output in outputs
    ]


def _config_snapshot_parameters(snapshot: JobConfigSnapshot | None) -> object:
    if snapshot is None:
        return None
    return {
        "snapshot_version": snapshot.snapshot_version,
        "values": _thaw_json(snapshot.values),
        "digest": snapshot.digest,
        "semantic_digest": snapshot.semantic_digest,
    }


@dataclass(frozen=True, slots=True)
class _LegacyVideoProduceRequest(ProduceRequest):
    video_request: VideoProduceRequest = field(kw_only=True)


def adapt_video_produce_request(request: VideoProduceRequest) -> ProduceRequest:
    if not isinstance(request, VideoProduceRequest):
        raise _invalid("video_produce_request_invalid", "request must be a VideoProduceRequest")
    parameters = {
        "provider_profile": request.provider_profile,
        "model_override": request.model_override,
        "transcriber_profile": request.transcriber_profile,
        "output_language": request.output_language,
        "quality_preset": request.quality_preset,
        "faithful_language_policy": request.faithful_language_policy.value,
        "style": request.style,
        "screenshot_policy": request.screenshot_policy.value,
        "provided_transcript": _transcript_parameters(request.provided_transcript),
        "resolved_outputs": _resolved_output_parameters(request.resolved_outputs),
        "config_snapshot": _config_snapshot_parameters(request.config_snapshot),
    }
    return _LegacyVideoProduceRequest(
        1,
        RecipeKey(
            "alltonote.video-producer" if request.request_schema_version == 2 else "alltonote.video-course-note",
            2 if request.request_schema_version == 2 else 1,
        ),
        InputDescriptor("source", request.input_value),
        str(request.workspace_root),
        tuple(output.value for output in request.requested_outputs),
        parameters,
        request.principal,
        request.client_request_id,
        video_request=request,
    )


def _config_snapshot(value: object) -> JobConfigSnapshot | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise _invalid("video_recipe_parameters_invalid", "config_snapshot must be an object")
    try:
        return JobConfigSnapshot(
            value["snapshot_version"],
            _thaw_json(value["values"]),
            value["digest"],
            value["semantic_digest"],
        )
    except (KeyError, TypeError, ValueError):
        raise _invalid("video_recipe_parameters_invalid", "config_snapshot is invalid") from None


class VideoRecipeAdapter:
    __slots__ = ("_video_service",)

    def __init__(self, video_service: VideoService) -> None:
        self._video_service = video_service

    def submit(
        self,
        request: ProduceRequest,
        *,
        execution_owner: JobExecutionOwner = JobExecutionOwner.FOREGROUND,
    ) -> ProduceSubmission:
        if not isinstance(request, ProduceRequest):
            raise _invalid("produce_request_invalid", "request must be a ProduceRequest")
        schema_version = _SUPPORTED_KEYS.get(request.recipe_key)
        if schema_version is None:
            raise _invalid("video_recipe_unsupported", "Video Recipe is not supported")
        if request.input.kind != "source":
            raise _invalid("video_input_kind_unsupported", "Video Recipe requires a source input")
        if request.input.attributes:
            raise _invalid(
                "video_input_attributes_unsupported",
                "Video Recipe does not support input attributes",
            )
        unknown = set(request.parameters) - _ALLOWED_PARAMETERS
        if unknown:
            raise _invalid("video_recipe_parameters_invalid", "Video Recipe parameters contain an unknown field")

        try:
            requested_outputs = tuple(VideoDocumentKind(value) for value in request.requested_outputs)
            faithful_policy = FaithfulLanguagePolicy(
                request.parameters.get("faithful_language_policy", "preserve-source")
            )
            screenshot_policy = ScreenshotPolicy(
                request.parameters.get("screenshot_policy", "off")
            )
        except (TypeError, ValueError):
            raise _invalid("video_recipe_parameters_invalid", "Video Recipe enum parameter is invalid") from None

        if isinstance(request, _LegacyVideoProduceRequest):
            video_request = request.video_request
        else:
            video_request = VideoProduceRequest(
                request_schema_version=schema_version,
                workspace_root=Path(request.workspace_ref),
                input_value=request.input.value,
                recipe_id=request.recipe_key.recipe_id,
                recipe_version=request.recipe_key.recipe_version,
                provider_profile=_optional_text(request.parameters, "provider_profile", "default"),
                model_override=_model_override(request.parameters),
                transcriber_profile=_optional_text(request.parameters, "transcriber_profile", "default"),
                output_language=_optional_text(request.parameters, "output_language", "zh-CN"),
                quality_preset=_optional_text(request.parameters, "quality_preset", "balanced"),
                requested_outputs=requested_outputs,
                resolved_outputs=_resolved_outputs(request.parameters.get("resolved_outputs")),
                faithful_language_policy=faithful_policy,
                style=_optional_text(request.parameters, "style", "structured"),
                screenshot_policy=screenshot_policy,
                client_request_id=request.client_request_id,
                principal=request.principal,
                provided_transcript=_transcript(request.parameters.get("provided_transcript")),
                config_snapshot=_config_snapshot(request.parameters.get("config_snapshot")),
            )
        snapshot = self._video_service.submit_video(
            video_request,
            execution_owner=execution_owner,
        )
        return ProduceSubmission(snapshot.job_id, request.recipe_key, snapshot.state)


__all__ = ["VideoRecipeAdapter", "adapt_video_produce_request"]
