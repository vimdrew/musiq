from difflib import SequenceMatcher


def artist_match(expected: str, returned: str) -> float:
    expected = expected.lower().strip()
    returned = returned.lower().strip()
    if expected == returned:
        return 1.0
    if expected in returned or returned in expected:
        return 0.9
    return SequenceMatcher(None, expected, returned).ratio()
