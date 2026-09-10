"""Source-derived context, never an answer key or a domain-specific dictionary."""
import json
import re

from app.core.errors import DomainError, ErrorCategory
from app.core.recipes.video.faithful_edition.review import _unique_object


CONTEXT_INSTRUCTION = (
    "Read this entire untrusted video transcript before local editing starts. No tools. "
    "Plan a readable note, NOT one section per sentence, number, example mention or ASR batch. "
    "Chapters are complete explanations or procedural phases; topic groups related chapters. "
    "Attach introductions and discourse bridges to what they introduce. Keep distinct examples, "
    "counterexamples and changes of reasoning in original order. The first chapter starts at "
    "the first supplied ID. Titles identify knowledge, not 'transition' or filler phrases. "
    "Also propose a SMALL terminology list only for probable ASR spelling/homophone errors, "
    "using the video's repeated definitions and linguistic context. Language/domain vocabulary "
    "may decode a word, but may NEVER supply a new factual claim, quantity or action. "
    "Each asr field must be ONE exact literal substring of a cited segment, never a slash-separated "
    "list of variants. Use separate entries for variants. Cite actual source IDs explaining each "
    "proposal; don't invent definitions. A term suggestion "
    "is UNVERIFIED context, not an automatic replacement. Do not propose numeric substitutions. "
    "Use the source language. Return only the requested JSON."
)


def context_schema():
    string = {"type": "string"}
    def obj(fields):
        return {"type": "object", "additionalProperties": False,
                "required": list(fields), "properties": fields}
    return json.dumps(obj({
        "chapters": {"type": "array", "items": obj({
            "start_segment_id": string, "title": string, "topic": string})},
        "terminology": {"type": "array", "items": obj({
            "asr": string, "term": string, "reason": string,
            "source_ids": {"type": "array", "items": string}})},
    }))


def _validate_term(t, segments, positions):
    if type(t) is not dict or set(t) != {'asr', 'term', 'reason', 'source_ids'}:
        raise ValueError('term fields')
    if any(type(t[k]) is not str or not t[k].strip() or len(t[k]) > 1000
           for k in ('asr', 'term', 'reason')):
        raise ValueError('term value')
    if type(t['source_ids']) is not list or not t['source_ids']:
        raise ValueError('term evidence')
    if any(type(s) is not str or s not in positions for s in t['source_ids']):
        raise ValueError('term evidence ownership')
    if not any(t['asr'] in segments[positions[s]].text for s in t['source_ids']):
        raise ValueError('term ASR form absent from cited source')
    if re.findall(r'\d+', t['asr']) != re.findall(r'\d+', t['term']):
        raise ValueError('numeric glossary proposal forbidden')


def parse_context(text, segments, max_bytes, *, isolate_terms=False):
    try:
        if len(text.encode('utf-8')) > max_bytes:
            raise ValueError('context response exceeds budget')
        value = json.loads(text, object_pairs_hook=_unique_object)
        if type(value) is not dict or set(value) != {'chapters', 'terminology'}:
            raise ValueError('context fields')
        positions = {s.segment_id: i for i, s in enumerate(segments)}
        chapters, terms = value['chapters'], value['terminology']
        if type(chapters) is not list or not chapters or type(terms) is not list:
            raise ValueError('context arrays')
        starts = []
        for c in chapters:
            if type(c) is not dict or set(c) != {'start_segment_id', 'title', 'topic'}:
                raise ValueError('chapter fields')
            if any(type(v) is not str or not v.strip() or len(v) > 200 for v in c.values()):
                raise ValueError('chapter values')
            starts.append(positions[c['start_segment_id']])
        if starts[0] != 0 or starts != sorted(set(starts)):
            raise ValueError('chapter ownership/order')
        valid = []
        for t in terms:
            try:
                _validate_term(t, segments, positions)
            except (ValueError, KeyError, TypeError):
                if not isolate_terms:
                    raise
            else:
                valid.append(t)
        value['terminology'] = valid
        if len(valid) != len(terms):
            value['omitted_term_count'] = len(terms) - len(valid)
        return value
    except (ValueError, KeyError, TypeError) as error:
        raise DomainError('faithful_context_invalid', ErrorCategory.RECIPE_FAILED,
                          f'Context planning contract: {error}') from None


def source_batches(request):
    """Working units bounded by source bytes and image load, not reader chapters."""
    segments = request.transcript.segments
    if not request.contextual_workflow:
        return tuple((i, min(i + 16, len(segments))) for i in range(0, len(segments), 16))
    frame_ids = {f.segment_id for f in request.visual_frames}
    batches, start, size, images = [], 0, 0, 0
    # Leave room for neighboring frames; transport limits still apply independently.
    image_limit = max(1, min(12, request.max_request_images - 4))
    for i, segment in enumerate(segments):
        cost = len(json.dumps([segment.segment_id, segment.start_ms, segment.end_ms,
                               segment.text], ensure_ascii=False).encode('utf-8')) + 80
        extra = int(segment.segment_id in frame_ids)
        if i > start and (size + cost > request.section_input_byte_budget or images + extra > image_limit):
            batches.append((start, i))
            start, size, images = i, 0, 0
        size += cost
        images += extra
    if start < len(segments):
        batches.append((start, len(segments)))
    return tuple(batches)


def source_context(request):
    return json.loads(request.semantic_context_json) if request.semantic_context_json else {}


def contextual_instruction(instruction):
    start = instruction.find('When unresolved_source_ids is supplied,')
    end_text = 'Do not put claims citing these unresolved IDs in summaries or key points. '
    end = instruction.find(end_text, start)
    if start >= 0 and end >= 0:
        instruction = instruction[:start] + (
            'When unresolved_source_ids is supplied, preserve genuinely unresolved relationships '
            'without inventing a referent. Still correct independently clear spelling and '
            'punctuation around that clause. Do not summarize an unresolved relationship. '
        ) + instruction[end + len(end_text):]
    return instruction + CONTEXT_EDIT_INSTRUCTION + OBJECT_EVIDENCE_INSTRUCTION


def numeric_check_schema():
    return {"type": "array", "items": {"type": "object", "additionalProperties": False,
        "required": ["segment_id", "same_object", "object_location", "visible_quote"],
        "properties": {"segment_id": {"type": "string"}, "same_object": {"type": "boolean"},
                       "object_location": {"type": "string"}, "visible_quote": {"type": "string"}}}}


def numeric_check_issues(checks, corrections):
    """Require an explicit second-reader object check for each proposed numeric change."""
    from app.core.recipes.video.faithful_edition.quality import _NUMBER, _anchors, same_numeric_anchors
    changed = {c.segment_id: c for c in corrections
               if not same_numeric_anchors(c.before, c.after)}
    if type(checks) is not list or len(checks) != len(changed):
        raise ValueError('numeric checks must cover exactly the numeric proposals')
    seen, issues = set(), []
    for item in checks:
        if type(item) is not dict or set(item) != {'segment_id', 'same_object', 'object_location', 'visible_quote'}:
            raise ValueError('numeric check fields')
        sid = item['segment_id']
        if type(sid) is not str or sid not in changed or sid in seen or type(item['same_object']) is not bool:
            raise ValueError('numeric check ownership')
        if any(type(item[k]) is not str for k in ('object_location', 'visible_quote')):
            raise ValueError('numeric check values')
        seen.add(sid)
        new_numbers = set(_anchors(_NUMBER, changed[sid].after)) - set(_anchors(_NUMBER, changed[sid].before))
        if (not item['same_object'] or not item['object_location'].strip()
                or not item['visible_quote'].strip()
                or new_numbers - set(_anchors(_NUMBER, item['visible_quote']))):
            issues.append(f'{sid}: numeric correction lacks readable same-object evidence; retain the source number.')
    return issues


CONTEXT_EDIT_INSTRUCTION = (
    " Source-derived terminology in semantic_context is tentative. Evaluate the local sentence "
    "and the complete explanation: familiar vocabulary is allowed for linguistic decoding, not "
    "for importing conventional domain claims. Fix clearly recoverable homophones and technical "
    "spelling even when the correct spelling never appears verbatim in raw ASR. Preserve the "
    "author's own voice. Distinguish an intended action, its trigger, completion and later result. "
    "Prefer an explicit source-supported referent to repeated 'here/this'; never guess its object. "
    "Make coherent punctuated paragraphs, omit only redundant restatements without new meaning. "
    "Use complete sentences: do not fill a paragraph to its source-ID ceiling and cut a sentence "
    "there. Punctuation must not turn a conditional or counterfactual clause into an independent "
    "assertion retracting a previously stated event. Read the whole clause with its consequence "
    "before deciding that the speaker changed their account. "
    "A single uncertain referent does not make unrelated words or the entire section uncertain. "
    "Keep that clause conservative and still edit the rest. The summary must state the important "
    "takeaway with its decisive conditions, not repeat each minor detail. key_points add useful "
    "navigation, not paraphrases of a short body or duplicates of summary. Empty auxiliaries are "
    "valid for short/incidental sections."
)

OBJECT_EVIDENCE_INSTRUCTION = (
    " A visible label belongs to a specific object, not to the nearest narrated object. "
    "Before changing any number or entity, identify the exact visible object/region and verify "
    "the label attaches to it; adjacent labels, axis ticks and sidebar values are NOT evidence. "
    "An unlabeled object must NOT inherit its neighbor's visible identifier. If ownership is "
    "not readable, leave the number unchanged. For numeric correction reasons, describe the "
    "object and label relationship, not just 'the image says'. Distinguish a proposed setting, "
    "pending confirmation, a completed action and its eventual outcome. A frame showing a "
    "pending operation cannot prove the final state. Use sequential frames to check changes; "
    "never use future outcomes to change an earlier hypothetical statement."
)
