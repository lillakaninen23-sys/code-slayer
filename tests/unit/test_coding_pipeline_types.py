"""`coding.pipeline_types`: closed-field model-facing parsers (mirroring
`coding.contracts.parse_coder_model_result()`'s exact fail-closed
posture) and the pure verdict-blocking gates.
"""

from __future__ import annotations

from code_slayer.coding.pipeline_types import (
    ReviewResult,
    ReviewVerdict,
    SecurityResult,
    SecurityVerdict,
    diff_fingerprint,
    parse_reviewer_model_result,
    parse_security_model_result,
    review_blocks_progress,
    security_blocks_progress,
)


def test_parse_reviewer_model_result_accepts_well_formed_payload():
    result = parse_reviewer_model_result({
        "verdict": "CHANGES_REQUIRED", "summary": "needs work",
        "findings": [{"description": "missing test", "severity": "major", "path": "a.py"}],
    })
    assert result is not None
    assert result.verdict == ReviewVerdict.CHANGES_REQUIRED
    assert result.findings[0].description == "missing test"


def test_parse_reviewer_model_result_rejects_unrecognized_top_level_field():
    assert parse_reviewer_model_result({
        "verdict": "PASS", "summary": "", "findings": [], "mutations": ["not allowed"],
    }) is None


def test_parse_reviewer_model_result_rejects_unknown_verdict():
    assert parse_reviewer_model_result({"verdict": "MAYBE", "summary": "", "findings": []}) is None


def test_parse_reviewer_model_result_rejects_non_mapping():
    assert parse_reviewer_model_result(["PASS"]) is None
    assert parse_reviewer_model_result("PASS") is None
    assert parse_reviewer_model_result(None) is None


def test_parse_reviewer_model_result_rejects_finding_with_extra_field():
    assert parse_reviewer_model_result({
        "verdict": "PASS", "summary": "", "findings": [{"description": "x", "affected_paths": []}],
    }) is None


def test_parse_security_model_result_accepts_well_formed_payload():
    result = parse_security_model_result({
        "verdict": "FAIL", "summary": "secret found",
        "findings": [{"description": "hardcoded key", "category": "secrets", "blocking": True}],
    })
    assert result is not None
    assert result.verdict == SecurityVerdict.FAIL
    assert result.findings[0].blocking is True


def test_parse_security_model_result_rejects_non_bool_blocking():
    assert parse_security_model_result({
        "verdict": "PASS", "summary": "",
        "findings": [{"description": "x", "category": "y", "blocking": "true"}],
    }) is None


def test_parse_security_model_result_rejects_unrecognized_verdict():
    assert parse_security_model_result({
        "verdict": "PASS_WITH_WARNINGS", "summary": "", "findings": [],
    }) is None


def test_review_blocks_progress_only_false_for_pass():
    fp = diff_fingerprint("diff")
    assert not review_blocks_progress(ReviewResult(ReviewVerdict.PASS, "", (), fp))
    assert review_blocks_progress(ReviewResult(ReviewVerdict.CHANGES_REQUIRED, "", (), fp))
    assert review_blocks_progress(ReviewResult(ReviewVerdict.BLOCKED, "", (), fp))


def test_security_blocks_progress_only_false_for_pass():
    fp = diff_fingerprint("diff")
    assert not security_blocks_progress(SecurityResult(SecurityVerdict.PASS, "", (), fp))
    assert security_blocks_progress(SecurityResult(SecurityVerdict.FAIL, "", (), fp))
    assert security_blocks_progress(SecurityResult(SecurityVerdict.HUMAN_REQUIRED, "", (), fp))


def test_diff_fingerprint_is_deterministic_and_content_sensitive():
    a = diff_fingerprint("diff --git a b\n+hello\n")
    b = diff_fingerprint("diff --git a b\n+hello\n")
    c = diff_fingerprint("diff --git a b\n+goodbye\n")
    assert a == b
    assert a != c
