import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from app.core.domain.video import TranscriptSegment, TranscriptDocument
from app.core.errors import DomainError
from app.core.ports.model_executor import ModelExecutionResult, ModelFinishReason
from app.core.recipes.video.faithful_edition.contextual import (
    parse_context, source_batches, contextual_instruction,
    numeric_check_issues,
)
from app.core.recipes.video.faithful_edition.fidelity import FIDELITY_INSTRUCTION
from test_faithful_edition_compiler import _request, _compiler_context, _FaithfulExecutor
from app.core.recipes.video.faithful_edition.contracts import GroundedCorrection, FaithfulParagraphV1
from app.core.recipes.video.faithful_edition.review import FaithfulSectionReview
from app.core.recipes.video.faithful_edition.pipeline import plan_faithful_edition


def segments():
    return (TranscriptSegment('seg_000001', 0, 1000, '先保村文件。'),
            TranscriptSegment('seg_000002', 1000, 2000, '文件保存后再退出。'))


def context_data():
    return {'chapters': [{'start_segment_id': 'seg_000001', 'title': '保存与退出', 'topic': '文件操作'}],
            'terminology': [{'asr': '保村', 'term': '保存', 'reason': '后一句明确说保存文件',
                             'source_ids': ['seg_000001', 'seg_000002']}]}


def test_source_derived_nontrading_term_valid():
    assert parse_context(json.dumps(context_data()), segments(), 10000)['terminology'][0]['term'] == '保存'


@pytest.mark.parametrize('defect', ['unknown_id', 'missing_first', 'duplicate_start', 'invented_term', 'numeric_change'])
def test_context_rejects_invalid_evidence(defect):
    data = context_data()
    if defect == 'unknown_id':
        data['terminology'][0]['source_ids'] = ['seg_999999']
    elif defect == 'missing_first':
        data['chapters'][0]['start_segment_id'] = 'seg_000002'
    elif defect == 'duplicate_start':
        data['chapters'] *= 2
    elif defect == 'invented_term':
        data['terminology'][0]['asr'] = 'never spoken'
    else:
        data['terminology'][0]['term'] = '保存2份'
    with pytest.raises(DomainError):
        parse_context(json.dumps(data), segments(), 10000)


def test_context_response_budget():
    with pytest.raises(DomainError):
        parse_context(json.dumps(context_data()), segments(), 10)


def test_invalid_optional_term_does_not_discard_valid_chapters():
    data = context_data()
    data['terminology'].append({'asr': '保村/报存', 'term': '保存', 'reason': 'variant list',
                                'source_ids': ['seg_000001']})
    result = parse_context(json.dumps(data), segments(), 10000, isolate_terms=True)
    assert result['chapters'] == data['chapters']
    assert len(result['terminology']) == 1
    assert result['omitted_term_count'] == 1


def test_processing_batches_are_lossless_and_not_fixed_subtitle_count():
    values = tuple(TranscriptSegment(f'seg_{i+1:06}', i*1000, (i+1)*1000, '操作说明。') for i in range(100))
    request = SimpleNamespace(transcript=TranscriptDocument('zh', values), contextual_workflow=True,
                              visual_frames=(), max_request_images=40, section_input_byte_budget=12000)
    batches = source_batches(request)
    assert [i for start, end in batches for i in range(start, end)] == list(range(100))
    assert any(end-start > 16 for start, end in batches)
    request.contextual_workflow = False
    assert source_batches(request)[0] == (0, 16)


def test_context_instruction_does_not_freeze_whole_paragraph():
    instruction = contextual_instruction(FIDELITY_INSTRUCTION)
    assert 'paragraph containing ANY' not in instruction
    assert 'preserve genuinely unresolved relationships' in instruction
    assert 'independently clear spelling' in instruction
    assert 'unlabeled object must NOT inherit' in instruction


@pytest.mark.parametrize('same_object,quote,location,expected', [
    (True, '容器 8', '右侧容器的标签', False),
    (False, '容器 8', '相邻容器标签', True),
    (True, '容器 9', '右侧容器标签', True),
    (True, '容器 8', '', True),
])
def test_numeric_changes_need_independent_object_evidence(same_object, quote, location, expected):
    correction = GroundedCorrection('seg_000001', '容器9', '容器8', 'seg_000001', '容器8', '标签')
    issues = numeric_check_issues([{'segment_id': 'seg_000001', 'same_object': same_object,
                                   'visible_quote': quote, 'object_location': location}], (correction,))
    assert bool(issues) is expected


def test_numeric_checks_cannot_omit_proposal():
    correction = GroundedCorrection('seg_000001', '容器9', '容器8', 'seg_000001', '容器8', '标签')
    with pytest.raises(ValueError):
        numeric_check_issues([], (correction,))


def test_partial_recovery_preserves_individually_reviewed_paragraph(tmp_path):
    request = replace(_request(), section_input_byte_budget=12000)
    compiler, _ = _compiler_context(tmp_path, _FaithfulExecutor())
    ref = plan_faithful_edition(request).sections[0]
    section = compiler._source_section(request, ref, 'fixture')
    raw = request.transcript.segments
    paragraphs = tuple(FaithfulParagraphV1(i, s.text, (s.segment_id,)) for i, s in enumerate(raw))
    paragraphs = (replace(paragraphs[0], text=paragraphs[0].text + ' '), *paragraphs[1:])
    section = replace(section, paragraphs=paragraphs)
    review = FaithfulSectionReview((), (), False, False, ('second paragraph is wrong',), (),
                                  matched_paragraph_ordinals=(0,))
    result = compiler._partial_source_section(request, ref, section, review)
    assert result.paragraphs[0] == section.paragraphs[0]
    assert result.paragraphs[1].text.startswith(raw[1].text)
    assert 'faithful_partial_paragraph_fallback' in result.warnings


class ContextExecutor(_FaithfulExecutor):
    def complete(self, request, token):
        if request.stage_id in ('faithful-context-plan', 'faithful-overview-select'):
            self.requests.append(request)
            payload = json.loads(request.user_content)
            data = ({'chapters': [{'start_segment_id': payload['segments'][0][0], 'title': 'API setup',
                                  'topic': 'Software'}], 'terminology': []}
                    if request.stage_id == 'faithful-context-plan' else
                    {'section_ordinals': [payload['candidates'][0]['ordinal']]})
            return ModelExecutionResult(text=json.dumps(data), actual_model_identity='fixture/model-v1',
                input_tokens=100, output_tokens=50, finish_reason=ModelFinishReason.STOP,
                provider_request_id='context-fixture')
        return super().complete(request, token)


def test_context_workflow_end_to_end_keeps_coverage_and_uses_new_stages(tmp_path):
    executor = ContextExecutor()
    compiler, context = _compiler_context(tmp_path, executor)
    result = compiler.compile(replace(_request(), contextual_workflow=True), context)
    assert result.execution_summary.body_segment_reference_coverage_ratio == 1.0
    stages = [r.stage_id for r in executor.requests]
    assert 'faithful-context-plan' in stages
    assert 'faithful-overview-select' in stages
    assert 'faithful-facts' in stages and 'faithful-review' in stages
    assert len({s.reading_chapter_id for s in result.plan.sections}) == 1
