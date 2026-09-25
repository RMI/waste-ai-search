from waste_ai_search.foundry_client import parse_json_response


def test_parse_json_response_accepts_fenced_json():
    payload = parse_json_response('```json\n{"attributes": []}\n```')

    assert payload == {"attributes": []}
