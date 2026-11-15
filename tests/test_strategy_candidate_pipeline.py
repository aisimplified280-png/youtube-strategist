"""Regression tests for review-only metadata-quality candidates."""

import unittest

from channel_strategy_worker import _metadata_candidates


class MetadataCandidatePipelineTests(unittest.TestCase):
    def setUp(self):
        self.snapshot = {
            "opportunities": [{
                "video_id": "abcDEF12345",
                "title": "AI tools",
                "impressions": None,
                "reasons": ["2 views in the scan period versus a 6 median."],
                "current_metadata": {
                    "title": "AI tools",
                    "description": "",
                    "tags": [],
                },
            }]
        }
        self.candidate = {
            "video_id": "abcDEF12345",
            "diagnosis": "metadata_quality",
            "decision": "review",
            "confidence": 0.78,
            "fields_to_modify": ["description"],
            "factual_evidence": "AI tools",
            "proposed_title": "AI tools",
            "proposed_description": "A proposed description grounded in the existing title.",
            "proposed_tags": [],
            "rationale": "The current description is empty.",
        }

    def test_metadata_quality_candidate_can_enter_review_without_ctr(self):
        result = _metadata_candidates(
            self.snapshot,
            {"metadata_candidates": [self.candidate]},
            limit=3,
        )
        self.assertEqual(len(result), 1)
        self.assertTrue(result[0]["metadata_audit_only"])
        self.assertEqual(result[0]["decision"], "review")
        self.assertEqual(result[0]["current_metadata"]["description"], "")

    def test_metadata_quality_candidate_requires_exact_source_quote(self):
        candidate = {**self.candidate, "factual_evidence": "This phrase is not in source metadata"}
        result = _metadata_candidates(
            self.snapshot,
            {"metadata_candidates": [candidate]},
            limit=3,
        )
        self.assertEqual(result, [])

    def test_metadata_quality_candidate_cannot_be_auto_publish(self):
        candidate = {**self.candidate, "decision": "auto_publish"}
        result = _metadata_candidates(
            self.snapshot,
            {"metadata_candidates": [candidate]},
            limit=3,
        )
        self.assertEqual(result, [])

    def test_metadata_quality_candidate_requires_review_confidence_band(self):
        candidate = {**self.candidate, "confidence": 0.91}
        result = _metadata_candidates(
            self.snapshot,
            {"metadata_candidates": [candidate]},
            limit=3,
        )
        self.assertEqual(result, [])


if __name__ == "__main__":
    unittest.main()
