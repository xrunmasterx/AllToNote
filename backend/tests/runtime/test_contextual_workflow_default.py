from types import SimpleNamespace
from unittest.mock import Mock

from app.core.domain.video import FaithfulLanguagePolicy, TranscriptDocument, TranscriptSegment
from app.core.ports.model_executor import ModelExecutionBinding
from app.core.recipes.video.compilation.contracts import TranscriptBasis
from app.runtime import _RuntimeFaithfulEditionCompiler


def test_runtime_promotes_contextual_workflow_with_experiment_limits():
    binding = ModelExecutionBinding(1, "fixture", "fixture/model-v1", "fixture-profile",
                                    128000, 16000, 4, True, False, 600)
    profile = Mock()
    profile.binding = binding
    profile.transcript_basis.return_value = TranscriptBasis.PLATFORM_CAPTION
    compiler = Mock()
    runtime = _RuntimeFaithfulEditionCompiler(profile=profile, compiler=compiler)
    transcript = TranscriptDocument("en", (TranscriptSegment("seg_000001", 0, 4000, "Use the first input."),))
    request = SimpleNamespace(
        provider_profile="fixture", model_override=None, transcript=transcript,
        transcript_basis="platform-caption", source_duration_ms=4000, source_language="en",
        language_policy=FaithfulLanguagePolicy.PRESERVE_SOURCE, output_language=None,
        source_title="Example", visual_frames=(),
        output=SimpleNamespace(recipe_id="alltonote.video-faithful-edition", recipe_version=1),
    )
    execution = object()
    runtime.compile(request, execution=execution)
    frozen = compiler.compile.call_args.args[0]
    assert frozen.contextual_workflow is True
    assert frozen.allow_partial_fallback is True
    assert frozen.parser_limits.max_segment_refs_per_paragraph == 24
    assert frozen.section_input_byte_budget == 12288
    runtime.compilation_identity()
    assert profile.compiler_identity.call_args.kwargs["behavior"]["contextual_workflow"] == 1
