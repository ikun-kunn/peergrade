"""Prepare anonymous rating packets and summarize paired human ratings."""
import argparse
import csv
import json
from pathlib import Path
from secrets import SystemRandom
from statistics import mean
from uuid import uuid4


DIMENSIONS = ('specificity', 'evidence_grounding', 'actionability')
INPUT_FIELDS = ('participant_id', 'assignment_id', 'assignment_text', 'consent',
                'before_review', 'before_evidence', 'after_review', 'after_evidence',
                'adopted', 'missing_reason')
SCORE_FIELDS = ('record_id', *DIMENSIONS)


def read_csv(path, fields):
    with path.open(encoding='utf-8-sig', newline='') as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames != list(fields):
            raise ValueError('Unexpected CSV header: ' + path.name)
        rows = list(reader)
    if any(None in row or None in row.values() for row in rows):
        raise ValueError('Malformed CSV row: ' + path.name)
    return rows


def write_csv(path, fields, rows):
    with path.open('x', encoding='utf-8-sig', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def write_json(path, data):
    with path.open('x', encoding='utf-8') as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2)
        stream.write('\n')


def prepare(source, destination):
    rows = read_csv(source, INPUT_FIELDS)
    participants = set()
    pairs, items, exclusions = [], [], []
    for row in rows:
        participant = row['participant_id'].strip()
        assignment = row['assignment_id'].strip()
        if not participant or not assignment or participant in participants:
            raise ValueError('Each participant needs one unique pseudonymous ID and one assignment ID.')
        participants.add(participant)
        if row['consent'] not in {'yes', 'no'}:
            raise ValueError('Consent must be yes or no.')
        if row['consent'] == 'no' or row['missing_reason'].strip():
            exclusions.append({'participant_id': participant,
                               'reason': '未同意或已撤回' if row['consent'] == 'no' else row['missing_reason'].strip()})
            continue
        if not all(row[field].strip() for field in ('assignment_text', 'before_review', 'after_review')):
            raise ValueError('Incomplete pairs must have a missing_reason.')
        if row['adopted'] not in {'yes', 'no'}:
            raise ValueError('Indicate whether the participant adopted the model feedback.')
        pair = {'participant_id': participant, 'assignment_id': assignment,
                'modified': any(row[f'before_{field}'] != row[f'after_{field}'] for field in ('review', 'evidence')),
                'adopted': row['adopted'] == 'yes'}
        for phase in ('before', 'after'):
            record = uuid4().hex
            pair[phase] = record
            items.append((record, row['assignment_text'], row[f'{phase}_review'], row[f'{phase}_evidence']))
        pairs.append(pair)
    if not pairs:
        raise ValueError('No complete consented pairs; do not publish a result.')
    SystemRandom().shuffle(items)
    destination.mkdir(parents=True, exist_ok=False)
    with (destination / 'blind_packet.txt').open('x', encoding='utf-8') as stream:
        for record, assignment, review, evidence in items:
            stream.write(f'记录编号：{record}\n原作业：\n{assignment}\n评价：\n{review}\n证据：\n{evidence or "（无）"}\n\n')
    for index in (1, 2):
        write_csv(destination / f'rater_{index}.csv', SCORE_FIELDS,
                  ({'record_id': item[0]} for item in items))
    write_json(destination / 'organizer_only.json', {'pairs': pairs, 'exclusions': exclusions})


def load_scores(path, expected):
    ratings = {}
    for row in read_csv(path, SCORE_FIELDS):
        record = row['record_id']
        if record not in expected or record in ratings:
            raise ValueError('Unknown or repeated blind record ID: ' + path.name)
        if any(row[dimension] not in {'0', '1', '2'} for dimension in DIMENSIONS):
            raise ValueError('Every score must be an integer from 0 to 2; do not treat blanks as zero.')
        ratings[record] = tuple(int(row[dimension]) for dimension in DIMENSIONS)
    if ratings.keys() != expected:
        raise ValueError('Both graders must rate all prepared records.')
    return ratings


def summarize(mapping_path, first_path, second_path):
    mapping = json.loads(mapping_path.read_text(encoding='utf-8'))
    pairs, exclusions = mapping['pairs'], mapping['exclusions']
    if not pairs:
        raise ValueError('No complete pairs.')
    expected = {pair[phase] for pair in pairs for phase in ('before', 'after')}
    if len(expected) != 2 * len(pairs) or len({pair['participant_id'] for pair in pairs}) != len(pairs):
        raise ValueError('Invalid paired mapping.')
    first, second = (load_scores(path, expected) for path in (first_path, second_path))
    averaged = {record: tuple(mean(scores) for scores in zip(first[record], second[record])) for record in expected}
    before = [averaged[pair['before']] for pair in pairs]
    after = [averaged[pair['after']] for pair in pairs]

    def metric(left, right):
        result = {'before_mean': mean(left), 'after_mean': mean(right),
                  'mean_delta': mean(end - start for start, end in zip(left, right))}
        return {key: round(value, 4) for key, value in result.items()}

    dimensions = {name: metric([row[index] for row in before], [row[index] for row in after])
                  for index, name in enumerate(DIMENSIONS)}
    initial, final = [sum(row) for row in before], [sum(row) for row in after]
    changes = [end - start for start, end in zip(initial, final)]
    counts = {'improved': sum(change > 0 for change in changes), 'unchanged': changes.count(0),
              'declined': sum(change < 0 for change in changes)}
    differences = [abs(a - b) for record in expected for a, b in zip(first[record], second[record])]
    return {'method': 'two independent human raters; mean of scores; paired descriptive comparison',
            'collected_count': len(pairs) + len(exclusions), 'valid_pairs': len(pairs),
            'excluded_count': len(exclusions), 'modified_count': sum(pair['modified'] for pair in pairs),
            'exclusion_reasons': {reason: sum(row['reason'] == reason for row in exclusions)
                                  for reason in sorted({row['reason'] for row in exclusions})},
            'adopted_count': sum(pair['adopted'] for pair in pairs),
            'dimensions': dimensions, 'total': metric(initial, final),
            'change_counts': counts, 'change_percent': {key: round(100 * value / len(pairs), 2) for key, value in counts.items()},
            'rater_exact_agreement': round(differences.count(0) / len(differences), 4),
            'rater_mean_absolute_difference': round(mean(differences), 4),
            'limitations': 'Exploratory paired observation without a control group; not proof of causal effect or generalizability.'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    prep = commands.add_parser('prepare')
    prep.add_argument('collection', type=Path)
    prep.add_argument('destination', type=Path)
    analyze = commands.add_parser('summarize')
    for name in ('mapping', 'first', 'second', 'output'):
        analyze.add_argument(name, type=Path)
    args = parser.parse_args()
    try:
        if args.command == 'prepare':
            prepare(args.collection, args.destination)
        else:
            write_json(args.output, summarize(args.mapping, args.first, args.second))
    except (ValueError, OSError, KeyError) as error:
        parser.error(str(error))


if __name__ == '__main__':
    main()
