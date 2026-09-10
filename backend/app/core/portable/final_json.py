"""Recover one complete final JSON object without repairing or changing its values."""
import json


def final_json_object(text, schema_json):
    try:
        json.loads(text)
        return text, False
    except ValueError:
        pass
    if not schema_json:
        return text, False
    schema = json.loads(schema_json)
    expected = set(schema.get('properties', {}))
    if schema.get('type') != 'object' or not expected:
        return text, False
    decoder = json.JSONDecoder()
    for start, character in enumerate(text):
        if character != '{':
            continue
        try:
            value, end = decoder.raw_decode(text, start)
        except ValueError:
            continue
        if (type(value) is dict and set(value) == expected and not text[end:].strip()):
            # The normal recipe parser still checks duplicate keys, schema, IDs,
            # coverage and semantics. Preserve the exact final object bytes.
            return text[start:end], True
    return text, False
