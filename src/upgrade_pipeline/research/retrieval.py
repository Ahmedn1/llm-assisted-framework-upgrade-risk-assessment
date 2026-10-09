"""Read-only public retrieval with robots checks and auditable text projections."""
import base64
import hashlib
import json
import re
from urllib.parse import quote, urldefrag, urlsplit
from urllib.robotparser import RobotFileParser

from evidence_collector.extraction import html_document, markdown_document, xml_links
from upgrade_resolver.http import FetchError, validate_public_url
from upgrade_resolver.versions import numeric_version, parse_endpoint, family_upper
from ..common import HEADING_MARK, VERSION_TOKEN, normalize
from .inventory import json_inventory, html_inventory


def retrieval_url(url):
    parsed = urlsplit(url)
    parts = parsed.path.strip('/').split('/')
    if parsed.hostname == 'github.com' and len(parts) >= 4:
        root = 'https://api.github.com/repos/' + '/'.join(parts[:2])
        if parts[2] in ('issues', 'pull') and parts[3].isdigit():
            return root + ('/pulls/' if parts[2] == 'pull' else '/issues/') + parts[3]
        if len(parts) >= 5 and parts[2:4] == ['releases', 'tag']:
            return root + '/releases/tags/' + quote('/'.join(parts[4:]), safe='')
        if len(parts) >= 5 and parts[2] == 'blob':
            # Slash-containing refs require an explicit contents API URL from the agent.
            return root + '/contents/' + '/'.join(parts[4:]) + '?ref=' + quote(parts[3], safe='')
    return url


class Retriever:
    def __init__(self, client, *, max_sources=100):
        self.client = client
        self.max_sources = max_sources
        self.sources = {}
        self.aliases = {}
        self.robots = {}
        self.interval = None

    def allowed(self, url):
        validate_public_url(url, resolve_dns=False)
        parsed = urlsplit(url)
        if parsed.hostname in ('api.github.com', 'pypi.org', 'registry.npmjs.org'):
            return
        origin = f'{parsed.scheme}://{parsed.netloc}'
        if origin not in self.robots:
            try:
                page = self.client.get(origin + '/robots.txt', accept='text/plain')
                policy = RobotFileParser()
                policy.parse(page['body'].splitlines())
                self.robots[origin] = policy
            except FetchError as exc:
                if not any(code in str(exc) for code in ('HTTP 404', 'HTTP 410')):
                    raise FetchError(f'Cannot check robots.txt for {origin}: {exc}') from exc
                self.robots[origin] = None
        policy = self.robots[origin]
        if policy is not None and not policy.can_fetch('UpgradeResolver', url):
            raise FetchError(f'robots.txt disallows {url}')

    def fetch(self, url, offset=0, link_offset=0):
        url = urldefrag(url)[0]
        if url not in self.aliases:
            if len(self.sources) >= self.max_sources:
                raise FetchError('Retrieved-source budget exhausted')
            requested = retrieval_url(url)
            self.allowed(requested)
            api = urlsplit(requested).hostname in ('api.github.com', 'pypi.org', 'registry.npmjs.org')
            page = self.client.get(requested, accept='application/json' if api else 'text/html')
            self.allowed(page['final_url'])
            body = page['body']
            if re.search(r'anomaly\.js|id=["\']challenge-form|cf-chl-|Just a moment\.\.\.', body[:20000], re.I):
                raise FetchError('Bot challenge, not document content')
            inventory_labels, inventory_kind = [], None
            projection = 'extracted_text'
            canonical = page['final_url']
            if api or body.lstrip().startswith(('{', '[')):
                data = json.loads(body)
                inventory_labels, inventory_kind = json_inventory(data, requested)
                projection = 'json'
                if isinstance(data, dict) and data.get('encoding') == 'base64' and 'content' in data:
                    text = base64.b64decode(data['content']).decode('utf-8')
                    canonical = data.get('html_url') or canonical
                    title = data.get('name', canonical)
                    sections, links = markdown_document(text, canonical)
                    projection = 'decoded_repository_file'
                else:
                    # Registry metadata can dwarf the context; retain an explicit release inventory.
                    if isinstance(data, dict) and isinstance(data.get('releases'), dict):
                        data = {'info': data.get('info', {}), 'available_release_versions': [v for v, files in data['releases'].items() if files]}
                        projection = 'registry_inventory'
                    elif isinstance(data, dict) and isinstance(data.get('versions'), dict):
                        data = {k: v for k, v in data.items() if k in ('name', 'description', 'homepage', 'repository', 'dist-tags') } | {'available_release_versions': list(data['versions'])}
                        projection = 'registry_inventory'
                    if inventory_kind == 'github_releases':
                        scoped = set(self.scoped_labels(inventory_labels))
                        data = {'release_records': [{k: r.get(k) for k in ('tag_name', 'name', 'url', 'html_url')}
                                                    for r in data if r.get('tag_name') in scoped]}
                        projection = 'release_inventory'
                    if inventory_kind and 'available_release_versions' in data:
                        data['available_release_versions'] = self.scoped_labels(inventory_labels)
                        if 'info' in data:
                            data['info'] = {k: v for k, v in data['info'].items() if k in ('name', 'summary', 'home_page', 'project_urls')}
                    text = json.dumps(data, ensure_ascii=False, indent=2)
                    title = canonical
                    sections, links = markdown_document(text, canonical)
                    if isinstance(data, dict) and data.get('body') is not None:
                        title = data.get('name') or data.get('title') or canonical
                        canonical = data.get('html_url') or canonical
            elif (re.match(r'text/(?:plain|markdown)', page.get('headers', {}).get('content-type', ''))
                  or re.search(r'\.(?:md|markdown|rst|txt)$', urlsplit(canonical).path, re.I)):
                # Raw changelogs mention JSX tags; HTML extraction would strip them and truncate sections.
                text, title = body, (re.findall(r'^#{1,2}\s+(.+)$', body, re.M) or [canonical])[0]
                sections, links = markdown_document(body, canonical)
                projection = 'markdown_text'
            elif re.search(r'<(?:urlset|sitemapindex|rss|feed)\b', body[:500]):
                _, locations = xml_links(body)
                title, text, sections = canonical, '\n'.join(locations), []
                links = [(u, '') for u in locations]
                projection = 'xml_links'
            else:
                title, text, sections, links, _ = html_document(body, canonical)
                inventory_labels, inventory_kind = html_inventory(links)
                if re.search(r'^Sign in to GitHub', title, re.I):
                    raise FetchError('Login page, not evidence')
            for next_url in re.findall(r'<([^>]+)>; rel="next"', page.get('headers', {}).get('link', '')):
                links.append((next_url, 'Next inventory page'))
            if len(text.strip()) < 20:
                raise FetchError('Insufficient extracted content')
            source_id = 'src_' + hashlib.sha256(canonical.encode()).hexdigest()[:16]
            if source_id not in self.sources:
                self.sources[source_id] = {'id': source_id, 'url': canonical, 'retrieval_url': requested,
                    'title': title, 'text': text, 'sections': sections, 'links': links, 'seen': [],
                    'retrieved_at': page.get('retrieved_at'), 'content_sha256': hashlib.sha256(text.encode()).hexdigest(),
                    'response_sha256': hashlib.sha256(body.encode()).hexdigest(), 'projection': projection,
                    'inventory_labels': self.scoped_labels(inventory_labels), 'inventory_kind': inventory_kind}
            self.aliases[url] = source_id
        source = self.sources[self.aliases[url]]
        excerpt = source['text'][offset:offset + 12000]
        source['seen'].append(excerpt)
        return {'source_id': source['id'], 'url': source['url'], 'title': source['title'],
                'text': excerpt, 'offset': offset, 'next_offset': offset + len(excerpt) if offset + len(excerpt) < len(source['text']) else None,
                'links': source['links'][link_offset:link_offset + 40],
                'next_link_offset': link_offset + 40 if link_offset + 40 < len(source['links']) else None,
                'projection': source['projection'], 'inventory_kind': source['inventory_kind'],
                'inventory_count': len(source['inventory_labels']), 'version_outline': self.version_outline(source)}

    def version_outline(self, source):
        """Offsets of in-range version headings, so long changelogs need not be paged through blindly."""
        outline, position = [], 0
        for section in source['sections']:
            found = source['text'].find(section['heading'], position)
            if found < 0:
                continue
            position = found
            labels = VERSION_TOKEN.findall(section['heading'])
            if labels and self.scoped_labels(labels):
                outline.append({'heading': section['heading'][:120], 'offset': found})
        return outline[:40]

    def scoped_labels(self, labels):
        stable = [label for label in labels if numeric_version(label) is not None]
        if self.interval is None:
            return stable
        current, target, mode = self.interval
        lower, upper = parse_endpoint(current), parse_endpoint(target)
        if mode == 'family':
            upper = family_upper(upper)
        return [label for label in stable if lower <= numeric_version(label) and
                (numeric_version(label) < upper if mode == 'family' else numeric_version(label) <= upper)]

    def cite_body(self, source_id, quote):
        source = self.cite(source_id, quote)
        headings = {normalize(h) for h in [source['title']] + [s['heading'] for s in source['sections']]}
        # Changelog quotes naturally lead with their version heading. cite() already proved the quote is
        # contiguous in the fetched text, so heading lines are allowed; the remainder must be body text.
        lines = [normalize(HEADING_MARK.sub('', line)) for line in quote.splitlines()]
        remainder = ' '.join(line for line in lines if line and line not in headings)
        if not remainder or any(remainder in h for h in headings):
            raise ValueError('Title-only or heading-only citation is not substantive evidence; include the body text under the heading')
        if remainder not in normalize('\n'.join(s['text'] for s in source['sections'])):
            raise ValueError('Evidence quote must contain body text, not only titles, headings or a link index')
        return source

    def version_context(self, source, quote):
        """The nearest version-bearing heading at or before the quote, else the page title: what ties a passage to a release."""
        text = normalize(source['text'])
        at = text.find(normalize(quote))
        governing, position = '', 0
        for section in source['sections']:
            heading = normalize(section['heading'])
            found = text.find(heading, position)
            if found < 0:
                continue
            if found > at:
                break
            position = found
            if VERSION_TOKEN.search(heading):
                governing = heading
        # A changelog's title is just its newest entry, so a governing version heading takes precedence.
        return governing or source['title']

    def cite(self, source_id, quote):
        source = self.sources.get(source_id)
        if source is None or not quote.strip() or not any(' '.join(quote.split()) in ' '.join(excerpt.split()) for excerpt in source['seen']):
            raise ValueError('Citation must quote text from an observed fetch result, not search snippets or memory'
                             + (divergence(quote, source['seen']) if source else ''))
        return source


def divergence(quote, excerpts):
    """Show where a near-miss quote leaves the observed text so the model can repair it without memory."""
    quote = ' '.join(quote.split())
    best, at, text = 0, 0, ''
    for excerpt in excerpts:
        normalized = ' '.join(excerpt.split())
        low, high = 0, len(quote)
        while low < high:  # prefix containment is monotone in prefix length
            mid = (low + high + 1) // 2
            if quote[:mid] in normalized:
                low = mid
            else:
                high = mid - 1
        if low > best:
            best, at, text = low, normalized.find(quote[:low]) + low, normalized
    cut = quote.rfind(' ', 0, best) + 1  # report from a word boundary, not a coincidental shared letter
    if cut < 20:
        return ''
    best, at = cut, at - (best - cut)
    return (f'. The first {best} characters match, then the quote has {quote[best:best + 60]!r} but the source continues '
            f'{text[at:at + 120]!r}. Quote one contiguous passage; do not join separate passages.')
