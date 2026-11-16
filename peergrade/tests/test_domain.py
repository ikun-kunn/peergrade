import unittest
from domain import allocate, content_hash, feedback_summary, quality_level, score_review, validate_ai_result, validate_anchors


class DomainTests(unittest.TestCase):
    def test_balanced_allocation(self):
        for n in (3, 5, 10, 30):
            owners = [str(i) for i in range(n)]
            pairs = allocate(owners, 42)
            self.assertEqual(len(pairs), n * 2)
            self.assertEqual(len(set(pairs)), n * 2)
            for person in owners:
                self.assertEqual(sum(a == person for a, _ in pairs), 2)
                self.assertEqual(sum(b == person for _, b in pairs), 2)
            self.assertTrue(all(a != b for a, b in pairs))
            self.assertEqual(pairs, allocate(owners, 42))

    def test_anchor_unicode_and_tampering(self):
        text = '第一段😀第二段：证据。'
        anchor = {'start': 3, 'end': 7, 'quote': text[3:7], 'content_hash': content_hash(text)}
        validate_anchors(text, [anchor])
        for change in ({'start': 2}, {'quote': '伪造'}, {'content_hash': 'bad'}):
            with self.assertRaises(ValueError):
                validate_anchors(text, [{**anchor, **change}])

    def test_scores(self):
        self.assertEqual(score_review([5, 4, 3]), 12)
        self.assertEqual([quality_level(x) for x in ([0, 1, 1], [1, 1, 1], [2, 2, 1])], ['LOW', 'MEDIUM', 'HIGH'])
        for invalid in ([True, 1, 2], [0, 1, 3]):
            with self.assertRaises(ValueError):
                quality_level(invalid)

    def test_ai_result_and_feedback(self):
        result = {'specificity': 2, 'evidence_grounding': 1, 'actionability': 2,
                  'quality_level': 'LOW', 'issues': [], 'suggestions': []}
        self.assertEqual(validate_ai_result(result)['quality_level'], 'HIGH')
        with self.assertRaises(ValueError):
            validate_ai_result({**result, 'specificity': True})
        summary = feedback_summary([[5, 5, 5], [3, 3, 4]])
        self.assertEqual(summary['average_total'], 12.5)
        self.assertTrue(summary['high_disagreement'])
        self.assertTrue(feedback_summary([[1, 2, 3]])['incomplete'])


if __name__ == '__main__':
    unittest.main()
