"""Discover identities through provider searches and links, never project tables."""
import re
from urllib.parse import quote, urlencode, urljoin, urlsplit

from bs4 import BeautifulSoup

from web_search import search_web

from .http import FetchError, HttpClient
from .models import Candidate, Evidence
from .source_policy import source_preference


def normalized(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


def package_names(project: str) -> list[str]:
    """Generic naming candidates, never project-specific aliases."""
    names = [project.lower().strip()]
    if names[0].endswith(".js"):
        names.append(names[0][:-3])
    return names


def github_repository(value: str) -> str:
    value = value.removeprefix("git+")
    if value.startswith("git@github.com:"):
        value = "https://github.com/" + value.split(":", 1)[1]
    parsed = urlsplit(value)
    if parsed.hostname not in ("github.com", "www.github.com"):
        return ""
    parts = parsed.path.strip("/").split("/")
    if len(parts) < 2 or parts[0] in ("search", "topics", "collections", "features", "orgs", "settings"):
        return ""
    if not all(re.fullmatch(r"[\w.-]+", p) for p in parts[:2]):
        return ""
    return "/".join(parts[:2]).removesuffix(".git")


def brand_matches(project: str, url: str) -> bool:
    host = urlsplit(url).hostname or ""
    if host in ("github.com", "pypi.org", "www.npmjs.com", "registry.npmjs.org"):
        return False
    brand = normalized(project)
    return bool(brand) and any(normalized(label) in (brand, brand + "project", brand + "framework") for label in host.split("."))


def score_candidate(candidate: Candidate, project: str) -> None:
    score = 0
    reasons = []
    brand = normalized(project)
    if normalized(candidate.name) == brand:
        score += 4
        reasons.append("Exact normalized project-name match (+4)")
    if candidate.repository and normalized(candidate.repository.split("/")[-1]) == brand:
        score += 3
        reasons.append("Repository name matches the requested project (+3)")
    if brand_matches(project, candidate.homepage):
        score += 4
        reasons.append("Project name matches a homepage host label (+4; heuristic, not ownership proof)")
    if re.search(r"(?<!\w)" + re.escape(project) + r"(?!\w)", candidate.description, re.I):
        score += 2
        reasons.append("Description explicitly names the requested project (+2)")
    ancillary = r"\b(client|driver|wrapper|bindings|plugin|adapter|sdk|operator|tutorial|example|boilerplate|template|orm)\b"
    if re.search(ancillary, candidate.description, re.I) and not re.search(ancillary, project, re.I):
        score -= 8
        reasons.append("Description suggests a client, integration, or example rather than the requested software (-8)")
    if candidate.metadata.get("fork"):
        score -= 10
        reasons.append("Repository is a fork (-10)")
    if candidate.metadata.get("reciprocal_link"):
        score += 4
        reasons.append("Homepage links back to this repository (+4)")
    if candidate.kind in ("npm", "pypi"):
        component_match = normalized(candidate.name) in {normalized(n) for n in package_names(project)}
        candidate.metadata["component_match"] = component_match
        if not component_match:
            score -= 12
            reasons.append("Package name does not identify the requested component; a shared repository is insufficient (-12)")
    candidate.score = score
    candidate.reasons = reasons
    candidate.rejected = score < 0


class Discoverer:
    def __init__(self, client: HttpClient, web_search: str = "auto"):
        self.client = client
        self.web_search = web_search
        self.candidates: dict[str, Candidate] = {}
        self.warnings: list[str] = []

    def add(self, candidate: Candidate):
        existing = self.candidates.get(candidate.url)
        if existing:
            existing.evidence.extend(e for e in candidate.evidence if e not in existing.evidence)
        else:
            self.candidates[candidate.url] = candidate

    def attempt(self, label: str, operation):
        try:
            return operation()
        except (FetchError, KeyError, TypeError, ValueError) as exc:
            self.warnings.append(f"{label}: {exc}")
            return None

    def github(self, repository: str, evidence: Evidence | None = None):
        url = f"https://api.github.com/repos/{repository}"
        data = self.client.json(url)
        self.add(self.github_candidate(data, url, evidence))

    @staticmethod
    def github_candidate(data: dict, source_url: str, evidence: Evidence | None = None) -> Candidate:
        proofs = [Evidence(source_url, "full_name", data["full_name"]),
                  Evidence(source_url, "description", data.get("description") or "")]
        if data.get("homepage"):
            proofs.append(Evidence(source_url, "homepage", data["homepage"]))
        if evidence:
            proofs.append(evidence)
        return Candidate("github", data["name"], data["html_url"], data.get("description") or "",
                         data["full_name"], data.get("homepage") or "", proofs, metadata=data)

    def npm(self, package: str, evidence: Evidence | None = None):
        url = "https://registry.npmjs.org/" + quote(package, safe="@")
        # Only fetch one version for identity; full catalogs can be very large.
        data = self.client.json(url + "/latest")
        repo = data.get("repository") or {}
        repo_url = repo if isinstance(repo, str) else repo.get("url", "")
        proofs = [Evidence(url + "/latest", "name", data["name"]),
                  Evidence(url + "/latest", "repository", repo_url),
                  Evidence(url + "/latest", "description", data.get("description") or "")]
        if evidence:
            proofs.append(evidence)
        self.add(Candidate("npm", data["name"], url, data.get("description") or "",
                           github_repository(repo_url), data.get("homepage") or "", proofs, metadata=data))

    def pypi(self, package: str, evidence: Evidence | None = None):
        url = "https://pypi.org/pypi/" + quote(package, safe="") + "/json"
        data = self.client.json(url)
        info = data["info"]
        links = info.get("project_urls") or {}
        repo_url = next((v for v in links.values() if github_repository(v)), "")
        homepage = next((v for k, v in links.items() if k.lower() in ("homepage", "home", "website")), info.get("home_page") or "")
        proofs = [Evidence(url, "info.name", info["name"]), Evidence(url, "info.summary", info.get("summary") or "")]
        proofs.extend(Evidence(url, "info.project_urls." + k, v) for k, v in links.items())
        if evidence:
            proofs.append(evidence)
        self.add(Candidate("pypi", info["name"], url, info.get("summary") or "",
                           github_repository(repo_url), homepage, proofs, metadata=data))

    def from_url(self, url: str, evidence: Evidence):
        repository = github_repository(url)
        parsed = urlsplit(url)
        parts = parsed.path.strip("/").split("/")
        if repository:
            self.github(repository, evidence)
        elif parsed.hostname in ("pypi.org", "www.pypi.org") and len(parts) >= 2 and parts[0] in ("project", "pypi"):
            self.pypi(parts[1], evidence)
        elif parsed.hostname in ("npmjs.com", "www.npmjs.com") and len(parts) >= 2 and parts[0] == "package":
            self.npm("/".join(parts[1:3]) if parts[1].startswith("@") else parts[1], evidence)
        elif parsed.hostname == "registry.npmjs.org":
            self.npm("/".join(parts[:2]) if parts[0].startswith("@") else parts[0], evidence)
        else:
            page = self.client.get(url, accept="text/html")
            soup = BeautifulSoup(page["body"], "html.parser")
            title = soup.title.get_text(" ", strip=True) if soup.title else ""
            description = soup.find("meta", attrs={"name": "description"})
            description = description.get("content", "") if description else title
            for element in soup.select("script, style, nav, footer"):
                element.decompose()
            excerpt = soup.get_text(" ", strip=True)[:6000]
            self.add(Candidate("html", title, page["final_url"], description,
                               homepage=page["final_url"], evidence=[evidence, Evidence(url, "title", title)],
                               metadata={"page_excerpt": excerpt}))

    def search_web(self, project: str, current: str, target: str):
        query = f'"{project}" official releases {current} to {target}'
        response = search_web(self.client, query, provider=self.web_search)
        self.warnings.extend(response.warnings)
        for item in response.results:
            self.attempt("Web search candidate", lambda link=item.url: self.from_url(
                link, Evidence(response.source_url, "search_result", link)))

    def discover(self, project: str, current: str, target: str, seed_urls: list[str]) -> list[Candidate]:
        # Seeds supplement automatic discovery and receive no authority bonus.
        for url in seed_urls:
            self.attempt("Seed URL", lambda url=url: self.from_url(url, Evidence(url, "user_supplied", "Discovery hint, not verified authority")))
        self.attempt("PyPI discovery", lambda: self.pypi(project))
        for package in package_names(project):
            self.attempt("npm exact-name discovery", lambda package=package: self.npm(package))

        def npm_search():
            url = "https://registry.npmjs.org/-/v1/search?" + urlencode({"text": project, "size": 5})
            data = self.client.json(url)
            for item in data.get("objects", []):
                name = item["package"]["name"]
                self.attempt("npm candidate", lambda name=name: self.npm(name, Evidence(url, "package.name", name)))

        def github_search():
            # Popularity affects candidate retrieval only; it is not an identity score.
            url = "https://api.github.com/search/repositories?" + urlencode({"q": f'"{project}" in:name,description fork:false', "sort": "stars", "per_page": 10})
            data = self.client.json(url)
            if data.get("incomplete_results"):
                self.warnings.append("GitHub search reported incomplete results")
            for item in data.get("items", []):
                self.add(self.github_candidate(item, url))

        self.attempt("npm discovery", npm_search)
        self.attempt("GitHub discovery", github_search)
        self.attempt("Web discovery", lambda: self.search_web(project, current, target))
        for candidate in self.candidates.values():
            score_candidate(candidate, project)

        # Expand the strongest candidates using actual homepage links.
        shortlist = sorted(self.candidates.values(), key=lambda c: (-c.score, c.url))[:5]
        for candidate in shortlist:
            if candidate.score < 4 or not candidate.homepage:
                continue
            self.attempt("Homepage verification", lambda candidate=candidate: self.inspect_homepage(candidate, project))
        # Resolve repository renames through the API, including redirects from old
        # links in historical documentation. No alias table is maintained.
        repository_aliases = {}
        for candidate in list(self.candidates.values()):
            score_candidate(candidate, project)
            if candidate.score < 6 or not candidate.repository:
                continue
            repository = candidate.repository
            if repository not in repository_aliases:
                data = self.attempt("Repository canonicalization", lambda repository=repository: self.client.json("https://api.github.com/repos/" + repository))
                if isinstance(data, dict) and data.get("full_name"):
                    repository_aliases[repository] = data["full_name"]
            if repository in repository_aliases:
                candidate.repository = repository_aliases[repository]
                candidate.evidence.append(Evidence("https://api.github.com/repos/" + repository, "full_name", candidate.repository))
        for candidate in self.candidates.values():
            score_candidate(candidate, project)
        return sorted(self.candidates.values(), key=lambda c: (-c.score, c.url))

    def inspect_homepage(self, candidate: Candidate, project: str):
        if urlsplit(candidate.homepage).hostname == "github.com":
            return
        page = self.client.get(candidate.homepage, accept="text/html")
        soup = BeautifulSoup(page["body"], "html.parser")
        anchors = [(urljoin(page["final_url"], a["href"]), a.get_text(" ", strip=True)) for a in soup.select("a[href]")]
        if candidate.repository and any(github_repository(url).lower() == candidate.repository.lower() for url, _ in anchors):
            candidate.metadata["reciprocal_link"] = True
            candidate.evidence.append(Evidence(page["final_url"], "repository_backlink", "https://github.com/" + candidate.repository))
        # A registry lacking repository metadata can be connected through its homepage.
        if not candidate.repository:
            repositories = {github_repository(url) for url, _ in anchors if github_repository(url)}
            matching = sorted(r for r in repositories if normalized(r.split("/")[-1]) == normalized(project))
            if len(matching) == 1:
                candidate.repository = matching[0]
                candidate.metadata["reciprocal_link"] = True
                candidate.evidence.append(Evidence(page["final_url"], "repository_link", matching[0]))
        # Record observed project-site links; do not infer ownership from page text.
        release_links = [(url, label) for url, label in anchors
                         if url != page["final_url"] and urlsplit(url).hostname == urlsplit(page["final_url"]).hostname
                         and re.search(r"release notes|releases|changelog", label, re.I)
                         and not re.search(r"\d", label)]
        for url, label in sorted(set(release_links))[:3]:
            self.attempt("Project release link", lambda url=url: self.from_url(
                url, Evidence(page["final_url"], "project_release_link", url)))
        # Discover registry links even when package and project names differ.
        seen = set()
        for url, label in anchors:
            if urlsplit(url).hostname in ("pypi.org", "www.npmjs.com", "npmjs.com") and url not in seen:
                seen.add(url)
                self.attempt("Linked registry", lambda url=url: self.from_url(url, Evidence(page["final_url"], "package_link", url)))
                if len(seen) >= 3:
                    break


def identity_key(candidate: Candidate) -> str:
    if candidate.repository:
        return "github:" + candidate.repository.lower()
    # Do not group unrelated packages solely because they claim the same homepage.
    if candidate.kind == "html":
        return "site:" + (urlsplit(candidate.url).hostname or candidate.url).lower()
    return candidate.kind + ":" + candidate.url.lower()


def rank_identities(candidates: list[Candidate]) -> list[dict]:
    groups = {}
    for candidate in candidates:
        if candidate.rejected:
            continue
        key = identity_key(candidate)
        groups.setdefault(key, []).append(candidate)
    result = []
    for key, members in groups.items():
        best = max(c.score for c in members)
        # Repeated mirrors/search results are not independent votes.
        result.append({"identity": key, "score": best, "preference": min(source_preference(c) for c in members if c.score == best), "members": members})
    return sorted(result, key=lambda g: (-g["score"], g["preference"], g["identity"]))
