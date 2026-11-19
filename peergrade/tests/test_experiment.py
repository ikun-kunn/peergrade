import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from experiment import DIMENSIONS, INPUT_FIELDS, SCORE_FIELDS, prepare, summarize, write_csv


class ExperimentTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.source = self.root / 'collection.csv'
        self.packet = self.root / 'ratings'

    def seed(self, empty=False):
        base = dict(participant_id='private-1', assignment_id='A', assignment_text='合成作业正文',
                    consent='yes', before_review='初稿评价', before_evidence='', after_review='终稿评价',
                    after_evidence='正文', adopted='yes', missing_reason='')
        write_csv(self.source, INPUT_FIELDS, [base | {'consent': 'no'}] if empty else [base,
                  base | {'participant_id': 'private-2', 'consent': 'no', 'before_review': '不公开的内容'},
                  base | {'participant_id': 'private-3', 'after_review': '', 'missing_reason': '退出'},
                  base | {'participant_id': 'private-4', 'after_review': '初稿评价', 'after_evidence': '', 'adopted': 'no'}])

    def ratings(self, partial=False):
        mapping = json.loads((self.packet / 'organizer_only.json').read_text(encoding='utf-8'))
        paths = [self.root / f'completed-{index}.csv' for index in (1, 2)]
        for index, path in enumerate(paths):
            rows = []
            for pair in mapping['pairs']:
                for phase in ('after', 'before'):
                    score = (2 if index == 0 else 1) if pair['modified'] and phase == 'after' else 1
                    rows.append({'record_id': pair[phase], **dict.fromkeys(DIMENSIONS, str(score))})
            if partial and index == 0:
                rows[0][DIMENSIONS[0]] = ''
            write_csv(path, SCORE_FIELDS, rows)
        return paths

    def test_blind_packet_and_paired_statistics(self):
        self.seed()
        prepare(self.source, self.packet)
        packet = (self.packet / 'blind_packet.txt').read_text(encoding='utf-8')
        self.assertNotIn('private-', packet)
        self.assertNotIn('不公开的内容', packet)
        self.assertEqual(packet.count('记录编号：'), 4)
        result = summarize(self.packet / 'organizer_only.json', *self.ratings())
        self.assertEqual(result['collected_count'], 4)
        self.assertEqual(result['valid_pairs'], 2)
        self.assertEqual(result['excluded_count'], 2)
        self.assertEqual(result['change_counts'], {'improved': 1, 'unchanged': 1, 'declined': 0})
        self.assertEqual(result['total']['mean_delta'], 0.75)
        self.assertEqual(result['rater_exact_agreement'], 0.75)
        self.assertEqual(result['modified_count'], 1)

    def test_missing_rating_cannot_be_treated_as_zero(self):
        self.seed()
        prepare(self.source, self.packet)
        with self.assertRaises(ValueError):
            summarize(self.packet / 'organizer_only.json', *self.ratings(partial=True))

    def test_empty_or_nonconsenting_sample_cannot_generate_packet(self):
        self.seed(empty=True)
        with self.assertRaises(ValueError):
            prepare(self.source, self.packet)
        self.assertFalse(self.packet.exists())
