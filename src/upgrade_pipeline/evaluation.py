"""Check a risk model against hand-written expectations: python -m upgrade_pipeline evaluate GOLD MODEL...

Each gold check targets one distinction extraction must make (required conditions,
alternative paths, removed APIs, ...). Subjects are matched with regexes against path names, descriptions,
condition statements, expected values, search patterns and migration steps. Evidence quotes are not
matched, because one quoted list often names several subjects.
"""
import argparse
import json
import re
import sys
from pathlib import Path

SEVERE = ('critical', 'high', 'medium')


def path_text(path):
    parts = [path['path_name'], path['description']]
    for condition in path['conditions']:
        parts += [condition['statement'], condition['expected_value'], *condition['search_patterns']]
    parts += [step['step'] for step in path.get('migration_steps', [])]
    return '\n'.join(parts)


def matching(model, pattern):
    regex = re.compile(pattern, re.I)
    paths = [p for p in model['risk_paths'] if regex.search(path_text(p))]
    context = [c for c in model['contextual_facts'] if regex.search(c['subject'] + '\n' + c['statement'])]
    return paths, context


def names(paths):
    return [p['path_name'] for p in paths]


def check(model, spec):
    """Return (passed, detail)."""
    kind, paths, context = spec['type'], *matching(model, spec.get('match', r'(?!)'))
    if kind == 'required_conditions':
        good = [p for p in paths if any(c['condition_role'] == 'required' and c['category'] == 'version' for c in p['conditions'])
                and any(c['condition_role'] == 'required' and c['category'] != 'version' for c in p['conditions'])]
        return bool(good), f'paths with required version + application conditions: {names(good)}; matched: {names(paths)}'
    if kind == 'alternative_paths':
        found = {pattern: names(matching(model, pattern)[0]) for pattern in spec['matches']}
        hit = [p for p, n in found.items() if n]
        distinct = {n[0] for n in found.values() if n}
        return len(hit) >= spec['min_distinct'] and len(distinct) >= spec['min_distinct'], f'paths per subject: {found}'
    if kind == 'risk_type':
        good = [p for p in paths if p['risk_type'] in spec['risk_types']]
        return bool(good), f"wanted {spec['risk_types']}; matched: {[(p['path_name'], p['risk_type']) for p in paths]}"
    if kind == 'migration_step':
        step = re.compile(spec['step'], re.I)
        good = [p for p in paths if any(step.search(s['step']) for s in p.get('migration_steps', []))]
        return bool(good), f"steps: {[(p['path_name'], [s['step'] for s in p.get('migration_steps', [])]) for p in paths]}"
    if kind == 'known_incompatibility':
        good = [p for p in paths if 'known_issue' in p.get('change_kinds', [])] + \
               [c for c in context if c['change'] == 'known_issue' or c.get('known_incompatibility')]
        return bool(good), f"matched paths' change_kinds: {[(p['path_name'], p.get('change_kinds')) for p in paths]}; context: {len(context)}"
    if kind == 'not_asserted':
        # Suspected or forward-looking claims may appear only as uncertain/low-confidence conditions, low-level paths, or context.
        asserted = [p for p in paths if p['risk_level'] in SEVERE and any(
            c['condition_role'] == 'required' and c['confidence'] != 'low' for c in p['conditions'])]
        return not asserted, f'asserted as a firm risk in: {names(asserted)}; also in context: {len(context)}'
    if kind == 'contextual':
        severe = [p for p in paths if p['risk_level'] in SEVERE]
        low = [p for p in paths if p['risk_level'] not in SEVERE]
        return bool((context or low) and not severe), f'context: {len(context)}; low paths: {names(low)}; severe paths: {names(severe)}'
    raise ValueError(f'Unknown check type {kind!r}')


def evaluate(model, gold):
    results = []
    for spec in gold['checks']:
        passed, detail = check(model, spec)
        results.append({'id': spec['id'], 'distinction': spec['distinction'], 'passed': passed, 'detail': detail})
    by_distinction = {}
    for result in results:
        by_distinction.setdefault(result['distinction'], []).append(result['passed'])
    return {'passed': sum(r['passed'] for r in results), 'total': len(results),
            'distinctions': {d: f'{sum(v)}/{len(v)}' for d, v in by_distinction.items()}, 'results': results}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('gold', type=Path)
    parser.add_argument('models', nargs='+', type=Path, help='risk_model.json files, or case directories containing one')
    args = parser.parse_args(argv)
    gold = json.loads(args.gold.read_text())
    failed = False
    for path in args.models:
        path = path / 'risk_model.json' if path.is_dir() else path
        report = evaluate(json.loads(path.read_text()), gold)
        failed |= report['passed'] < report['total']
        print(f"== {path}: {report['passed']}/{report['total']} checks passed")
        for distinction, score in report['distinctions'].items():
            print(f'   {score:>5}  {distinction}')
        for result in report['results']:
            if not result['passed']:
                print(f"   FAIL {result['id']}: {result['detail'][:300]}")
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
