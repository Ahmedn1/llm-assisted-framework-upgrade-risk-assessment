"""Deterministic source provenance, preferences and interval coverage."""
from urllib.parse import urlsplit
from .versions import endpoint_present, parse_endpoint, select_releases


def source_preference(candidate):
    """User preference for ties, never a claim of ownership."""
    if any(e.field == 'search_result' for e in candidate.evidence):
        return 0
    return 1 if candidate.kind == 'github' else 2 if candidate.kind in ('npm', 'pypi') else 3


def provenance(candidate):
    links = [vars(e) for e in candidate.evidence if e.field in
             ('project_release_link', 'repository_backlink', 'repository_link', 'package_link')]
    return {'relationships': links,
            'mirror_declared': 'mirror' in candidate.description.lower(),
            'ownership_verified': False,
            'note': 'Observed links corroborate association; titles, search positions and self-asserted homepages do not prove ownership.'}


def authority_priority(candidate, catalog):
    """Higher is preferred; interpretable source-role heuristic, not proof."""
    if catalog.source_type == 'github_tags' or provenance(candidate)['mirror_declared']:
        return 1
    if catalog.source_type in ('npm_release_catalog', 'pypi_release_catalog', 'github_releases'):
        return 3
    if any(e.field == 'project_release_link' for e in candidate.evidence):
        return 4
    if any(word in (candidate.url + ' ' + candidate.name).lower() for word in ('release', 'changelog')):
        return 3
    return 2


def catalog_coverage(catalog, current, target, mode):
    selected = select_releases(catalog.releases, current, target, mode)
    precision = len(parse_endpoint(target).release)
    detailed = sum(len(parse_endpoint(r.version).release) > precision for r in selected)
    return {'endpoint_checks': {f'{label}_found': endpoint_present(catalog.releases, v, mode)
                               for label, v in [('current', current), ('target', target)]},
            'interval_version_count': len(selected),
            'granularity': 'detailed_releases' if detailed else 'endpoint_precision_only',
            'detailed_version_count': detailed,
            'enumeration_complete': catalog.enumeration_complete}


def annotate_agreement(catalogs, current, target, mode):
    """Agreement is corroboration of observed sets, not proof of publication/completeness."""
    rows = []
    for catalog in catalogs:
        versions = {r.version for r in select_releases(catalog.releases, current, target, mode)}
        rows.append((catalog, versions))
    return [{'source_url': c.source_url, **catalog_coverage(c, current, target, mode),
             'matching_observed_catalogs': [other.source_url for other, values in rows
                                           if other.source_url != c.source_url and versions and values == versions]}
            for c, versions in rows]
