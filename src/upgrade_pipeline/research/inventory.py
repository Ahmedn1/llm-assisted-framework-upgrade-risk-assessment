"""Recognize release inventories; prose version mentions are not release records."""
import re
from urllib.parse import urlsplit

from upgrade_resolver.versions import numeric_version


def json_inventory(data, url):
    host = urlsplit(url).hostname
    if host == 'pypi.org' and isinstance(data, dict) and isinstance(data.get('releases'), dict):
        return [v for v, files in data['releases'].items() if files], 'pypi_release_catalog'
    if host == 'registry.npmjs.org' and isinstance(data, dict) and isinstance(data.get('versions'), dict):
        return list(data['versions']), 'npm_release_catalog'
    if host == 'api.github.com' and '/releases' in urlsplit(url).path and isinstance(data, list):
        return [r['tag_name'] for r in data if isinstance(r, dict) and isinstance(r.get('tag_name'), str)
                and not r.get('draft') and not r.get('prerelease')], 'github_releases'
    return [], None


def html_inventory(links):
    labels = []
    for url, label in links:
        path = urlsplit(url).path
        if not re.search(r'/releases?(?:/|-)|/changelog', path, re.I):
            continue
        for match in re.finditer(r'(?<![\w.])v?(\d+(?:\.\d+){0,3})(?![\w.]|\.\d)', label):
            version = match.group(1)
            if version in path and numeric_version(version) is not None:
                labels.append(version)
    # One incidental link from a guide is not an index.
    labels = list(dict.fromkeys(labels))
    return (labels, 'html_release_index') if len(labels) >= 2 else ([], None)
