import json
import re
from urllib.parse import quote, urljoin, urlsplit

from bs4 import BeautifulSoup

from .http import FetchError, HttpClient
from .models import Candidate, Catalog, Release
from .versions import endpoint_present, numeric_version, parse_endpoint, family_upper


def add_release(catalog: Catalog, label: str, url: str, *, names=(), published_at=None, yanked=False, record_url=None):
    version = numeric_version(label, names)
    if version is None:
        catalog.skipped_labels.append(label)
        return
    catalog.releases.append(Release(str(version), label, url, catalog.source_type, published_at, yanked, record_url))


class CatalogReader:
    def __init__(self, client: HttpClient, max_pages: int = 30, max_html_pages: int = 8):
        self.client = client
        self.max_pages = max_pages
        self.max_html_pages = max_html_pages

    def read(self, candidate: Candidate, current: str, target: str, mode: str) -> list[Catalog]:
        if candidate.kind == "pypi":
            return [self.pypi(candidate)]
        if candidate.kind == "npm":
            return [self.npm(candidate)]
        if candidate.kind == "github":
            releases = self.github(candidate.repository, "releases")
            catalogs = [releases]
            if not releases.enumeration_complete or not all(endpoint_present(releases.releases, v, mode) for v in (current, target)):
                catalogs.append(self.github(candidate.repository, "tags"))
            if not any(c.releases for c in catalogs) and candidate.homepage:
                catalogs.append(self.html(candidate.homepage, candidate.name, current, target, mode))
            return catalogs
        return [self.html(candidate.url, candidate.name, current, target, mode)]

    def pypi(self, candidate: Candidate) -> Catalog:
        data = candidate.metadata if "releases" in candidate.metadata else self.client.json(candidate.url)
        if "releases" not in data or not isinstance(data["releases"], dict):
            raise FetchError("PyPI response has no release catalog")
        catalog = Catalog(candidate.url, "pypi_release_catalog")
        for version, files in data["releases"].items():
            if not files:
                catalog.skipped_labels.append(version + " (no available files)")
                continue
            timestamps = [f["upload_time_iso_8601"] for f in files if f.get("upload_time_iso_8601")]
            add_release(catalog, version, candidate.url, published_at=min(timestamps) if timestamps else None,
                        yanked=all(f.get("yanked", False) for f in files))
        return catalog

    def npm(self, candidate: Candidate) -> Catalog:
        # Abbreviated metadata avoids downloading every historical README.
        page = self.client.get(candidate.url, accept="application/vnd.npm.install-v1+json")
        try:
            data = json.loads(page["body"])
        except ValueError as exc:
            raise FetchError("Invalid npm catalog JSON") from exc
        if not isinstance(data.get("versions"), dict):
            raise FetchError("npm response has no release catalog")
        catalog = Catalog(candidate.url, "npm_release_catalog")
        for version in data["versions"]:
            add_release(catalog, version, candidate.url, published_at=data.get("time", {}).get(version))
        return catalog

    def github(self, repository: str, resource: str) -> Catalog:
        root = f"https://api.github.com/repos/{repository}/{resource}"
        catalog = Catalog(root, "github_" + resource)
        if resource == "tags":
            catalog.warnings.append("Git tags identify source references; they do not prove that a corresponding package or binary was published.")
        for number in range(1, self.max_pages + 1):
            url = root + f"?per_page=100&page={number}"
            try:
                page = self.client.get(url)
                items = json.loads(page["body"])
                if not isinstance(items, list):
                    raise FetchError("Expected a GitHub catalog array")
            except (FetchError, ValueError) as exc:
                catalog.enumeration_complete = False
                catalog.warnings.append(str(exc))
                break
            for item in items:
                label = item.get("tag_name" if resource == "releases" else "name", "")
                if item.get("draft") or item.get("prerelease"):
                    catalog.skipped_labels.append(label)
                    continue
                evidence_url = item.get("html_url") or "https://github.com/" + repository + "/tree/" + quote(label, safe="")
                add_release(catalog, label, url, names=(repository.split("/")[-1],), published_at=item.get("published_at"), record_url=evidence_url)
            if not items or (len(items) < 100 and 'rel="next"' not in page.get("headers", {}).get("link", "")):
                break
        else:
            catalog.enumeration_complete = False
            catalog.warnings.append(f"GitHub {resource} enumeration hit the {self.max_pages}-page limit; more versions may exist.")
        return catalog

    def html(self, root: str, project_name: str, current=None, target=None, mode="family") -> Catalog:
        catalog = Catalog(root, "html_release_index", enumeration_complete=False)
        catalog.warnings.append("HTML discovery is bounded and heuristic; it cannot establish catalog completeness.")
        queue = [(0, root)]
        seen = set()
        host = urlsplit(root).hostname
        while queue and len(seen) < self.max_html_pages:
            queue.sort(key=lambda x: (-x[0], x[1]))
            _, url = queue.pop(0)
            if url in seen:
                continue
            seen.add(url)
            try:
                page = self.client.get(url, accept="text/html")
            except FetchError as exc:
                catalog.warnings.append(str(exc))
                continue
            soup = BeautifulSoup(page["body"], "html.parser")
            for anchor in soup.select("a[href]"):
                link = urljoin(page["final_url"], anchor["href"]).split("#")[0]
                text = anchor.get_text(" ", strip=True)
                if urlsplit(link).hostname != host:
                    continue
                label = None
                if numeric_version(text):
                    label = text
                elif re.search(r"release|version|changelog", text, re.I) and not re.search(r"alpha|beta|\brc\b|preview|candidate", text, re.I):
                    match = re.search(r"(?<![\w.])v?\d+(?:\.\d+){0,3}(?![\w.]|[-_](?:alpha|beta|rc|dev))", text, re.I)
                    if match:
                        label = match.group()
                if label:
                    add_release(catalog, label, url, record_url=link)
                relevance = 4 if re.search(r"release|changelog|upgrade", text + " " + link, re.I) else 1 if re.search(r"documentation|download", text, re.I) else 0
                # A labeled release outside the input families cannot help this interval.
                linked_version = numeric_version(label) if label else None
                if linked_version is None:
                    match = re.search(r"(?:release[-/]|/)(\d+(?:[.-]\d+){0,3})(?:/|\.html|$)", urlsplit(link).path)
                    if match:
                        linked_version = numeric_version(match.group(1).replace("-", "."))
                if linked_version is not None and current and target:
                    upper = family_upper(parse_endpoint(target)) if mode == "family" else parse_endpoint(target)
                    if linked_version < parse_endpoint(current) or (linked_version >= upper if mode == "family" else linked_version > upper):
                        continue
                    relevance += 5
                if relevance and link not in seen and urlsplit(link).hostname == host and not re.search(r"\.(?:zip|gz|xz|exe|pdf)$", urlsplit(link).path):
                    queue.append((relevance, link))
        return catalog
