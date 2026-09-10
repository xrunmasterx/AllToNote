import json

import pytest

from app.core.portable.final_json import final_json_object
from app.core.recipes.video.faithful_edition.quality import same_numeric_anchors


@pytest.mark.parametrize('source,target', [
    ('等两分钟', '等2分钟'), ('规则6', '规则六'), ('连续五次', '连续5次'),
    ('发五张票，每组三个人', '发5张票，每组3个人'),
    ('共一百零二页', '共102页'), ('第十二步', '第12步'),
])
def test_equal_numeric_spelling(source, target):
    assert same_numeric_anchors(source, target)
    assert same_numeric_anchors(target, source)


@pytest.mark.parametrize('source,target', [
    ('等两分钟', '等3分钟'), ('负三米', '3米'), ('负十三米', '3米'), ('温度-3', '温度3'),
    ('一直运行', '1直运行'), ('二一出来', '第2个出来'),
    ('数量12345', '数量1、2、3、4、5'), ('值20、38、40', '值20、20、38、40'),
    ('一百二米', '102米'),
])
def test_real_or_ambiguous_numeric_change_stays_rejected(source, target):
    assert not same_numeric_anchors(source, target)


def schema():
    return json.dumps({'type': 'object', 'properties': {'pass': {'type': 'boolean'},
                                                    'issues': {'type': 'array'}}})


def test_final_object_recovery_preserves_a_failed_verdict():
    final = json.dumps({'pass': False, 'issues': ['missing prerequisite']})
    value, recovered = final_json_object('{"issues":["unfinished ' + final, schema())
    assert recovered and value == final
    assert json.loads(value)['pass'] is False


@pytest.mark.parametrize('value', [
    '{"pass":true,"issues":[]} trailing garbage',
    '{"pass":true,"issues":[',
    '{"pass":true,"wrong":[]}',
])
def test_does_not_invent_or_complete_invalid_json(value):
    assert final_json_object(value, schema()) == (value, False)


def test_valid_json_is_unchanged():
    value = '{"pass": false, "issues": ["do not drop this"]}'
    assert final_json_object(value, schema()) == (value, False)
