"""
Test for app/json_extract.py -- the shared, robust JSON-extraction
helper that replaced 20 near-identical inline call sites across 16
files, all of which only handled a response that started with a
markdown fence and nothing else. Live testing surfaced real failures
("Extra data", "Expecting value") from content_policy.py and quality.py
when Claude added any prose around the JSON -- this locks in that the
replacement actually handles those shapes, not just the case that was
already working.
"""
import sys

sys.path.insert(0, ".")
from app.json_extract import extract_json_text  # noqa: E402


def test_bare_json_passes_through_unchanged():
    print("=" * 60)
    print("TEST 1: a bare JSON object/array (the already-working, instructed case) passes through unchanged")
    print("=" * 60)
    assert extract_json_text('{"allowed": true}') == '{"allowed": true}'
    assert extract_json_text('[1, 2, 3]') == '[1, 2, 3]'
    print("PASS\n")


def test_fenced_json():
    print("=" * 60)
    print("TEST 2: a markdown-fenced JSON block is extracted")
    print("=" * 60)
    assert extract_json_text('```json\n{"allowed": true}\n```') == '{"allowed": true}'
    assert extract_json_text('```\n{"allowed": true}\n```') == '{"allowed": true}'
    print("PASS\n")


def test_fenced_json_with_surrounding_prose():
    print("=" * 60)
    print("TEST 3: a fenced JSON block with prose BEFORE and/or AFTER it is still extracted")
    print("=" * 60)
    assert extract_json_text('Sure, here you go:\n```json\n{"allowed": true}\n```') == '{"allowed": true}'
    assert extract_json_text('```json\n{"allowed": true}\n```\nLet me know if you need anything else.') == '{"allowed": true}'
    assert extract_json_text(
        'Sure, here you go:\n```json\n{"allowed": true}\n```\nHope that helps!'
    ) == '{"allowed": true}'
    print("PASS\n")


def test_bare_json_with_surrounding_prose():
    print("=" * 60)
    print("TEST 4: an unfenced JSON object surrounded by prose is still extracted")
    print("=" * 60)
    assert extract_json_text('Here is my assessment: {"allowed": true} -- hope that helps!') == '{"allowed": true}'
    assert extract_json_text('wrapped text [1, 2, 3] more text') == '[1, 2, 3]'
    print("PASS\n")


def test_no_json_present_returns_input_unchanged():
    print("=" * 60)
    print("TEST 5: no JSON anywhere -- returns the (stripped) input as-is so the caller's own error handling still applies")
    print("=" * 60)
    assert extract_json_text('not json at all') == 'not json at all'
    assert extract_json_text('') == ''
    assert extract_json_text('   ') == ''
    print("PASS\n")


def run():
    test_bare_json_passes_through_unchanged()
    test_fenced_json()
    test_fenced_json_with_surrounding_prose()
    test_bare_json_with_surrounding_prose()
    test_no_json_present_returns_input_unchanged()
    print("ALL TESTS PASSED")


run()
