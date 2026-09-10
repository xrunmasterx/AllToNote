"""Conservative spelling equivalence, not arithmetic or entity inference."""
import re

_DIGITS = dict(zip('零〇一二两三四五六七八九', (0, 0, 1, 2, 2, 3, 4, 5, 6, 7, 8, 9)))
_UNITS = {'十': 10, '百': 100, '千': 1000}
_HAN = '零〇一二两三四五六七八九十百千'
# Grammatical counters/ordinal markers, not a vocabulary for any video subject.
_QUANTITY = re.compile(
    rf'(?<![负{_HAN}])(?:第|规则|步骤|编号|章节|条款|图|表)([{_HAN}]+)|'
    rf'(?<![负{_HAN}])([{_HAN}]+)(?=分钟|小时|厘米|千米|公斤|千克|毫升|[个次遍步秒天周月年米克升倍根张手笔项份页行列台人组种段条])'
)


def _integer(text):
    units = [c for c in text if c in _UNITS]
    if units and units[-1] != '十' and text[-1] in _DIGITS and not any(c in text for c in '零〇'):
        # Colloquial 一百二 / 两千三 can imply a missing trailing unit.
        return None
    if all(c in _DIGITS for c in text):
        return str(int(''.join(str(_DIGITS[c]) for c in text)))
    total, digit = 0, 0
    for c in text:
        if c in _DIGITS:
            digit = _DIGITS[c]
        else:
            total += (digit or 1) * _UNITS[c]
            digit = 0
    return str(total + digit)


def normalize_numeric_spelling(source, target):
    """Only compare a Chinese quantity to an Arabic value present on the other side.

    Do not split concatenated digit strings, calculate a value, interpret bare
    ambiguous Chinese labels, or broadly turn idioms such as 一直 into numbers.
    This function does not establish who owns a quantity; semantic review does.
    """
    def convert(text, counterpart):
        allowed = set(re.findall(r'(?<![\d.])\d+(?![\d.])', counterpart))
        def replace(match):
            index = 1 if match[1] else 2
            value = _integer(match[index])
            if value is None or value not in allowed:
                return match[0]
            offset = match.start(index) - match.start()
            return match[0][:offset] + value
        return _QUANTITY.sub(replace, text)
    return convert(source, target), convert(target, source)
