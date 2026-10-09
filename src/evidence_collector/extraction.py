"""Deterministic document parsing; no inferred upgrade-risk claims."""
import re
from urllib.parse import urljoin, urldefrag, urlsplit
from xml.etree import ElementTree
from bs4 import BeautifulSoup
from upgrade_resolver.versions import numeric_version

TOPICS = re.compile(r'migrat|upgrad|release|changelog|breaking|deprecat|compatib|removed|documentation|\bdocs?\b|readme|\bapi\b', re.I)

# Candidate-section signals for later risk extraction. Deliberately recall-oriented keyword rules:
# they nominate sections for review and never assert that a risk exists.
SECTION_TOPICS = {
    'breaking_change': r'breaking change|\bbreaking\b|backwards?[- ]incompatib|incompatible change',
    'deprecation': r'deprecat',
    'removal': r'\bremov(?:ed|al|es)\b|\bdrop(?:ped|s)? support|no longer (?:available|exported|supported|included)',
    'changed_default': r'\bdefaults? (?:to|is now|now|changed|has changed)|chang(?:e|ed|es) (?:the )?default|new default|now (?:enabled|disabled|on|off) by default',
    'behavior_change': r'behaviou?rs? (?:change|differently)|behaves? differently|now (?:throws?|warns?|errors?|returns?|raises?|fails?)|stricter|no longer (?:\w+ ){0,2}(?:accepts?|allows?|returns?|works?)',
    'runtime_requirement': r'requires? (?:python|node(?:\.js)?|go|java|kernel|glibc)|minimum (?:supported )?(?:python|node|go|runtime)|(?:python|node(?:\.js)?) \d+(?:\.\d+)? (?:or (?:later|newer|higher)|\+|is required)|supported (?:python|node(?:\.js)?) versions?',
    'dependency_requirement': r'peer ?dependenc|\bdependenc(?:y|ies)\b|minimum (?:supported )?version of|requires? [\w.@/-]+ (?:>=|version) ?\d',
    'configuration_change': r'\bsettings?\b|config(?:uration)? (?:option|key|file|setting|parameter)|\bflag\b|environment variable|feature gates?|\brenamed?\b',
    'migration_step': r'migrat|how to upgrade|upgrade guide|\bupgrading\b|replace [\w.`()]+(?: [\w.`()]+){0,3} with|instead of|use [\w.`()]+ instead',
    'data_migration': r'pg_upgrade|pg_dump|dump and restore|schema migration|data (?:format|migration)|on-disk format|\breindex|storage version|catalog version',
    'api_change': r'\bnew (?:api|method|hook)s?\b|\bsignature\b|exported from|\bapis?\b (?:changed|removed|renamed)',
    'known_issue': r'known (?:issue|problem|bug)s?|\bregression|\bworkaround|\bcaveat',
    'security': r'\bsecurity\b|cve-\d{4}|vulnerab',
}
SECTION_TOPIC_PATTERNS = {name: re.compile(pattern, re.I) for name, pattern in SECTION_TOPICS.items()}


def section_topics(heading, text):
    content = heading + '\n' + text
    return [name for name, pattern in SECTION_TOPIC_PATTERNS.items() if pattern.search(content)]


def annotate_sections(sections, url):
    for section in sections:
        section.setdefault('url', urldefrag(url)[0] + ('#' + section['anchor'] if section.get('anchor') else ''))
        section['change_related'] = bool(TOPICS.search(section['heading'] + ' ' + section['text']))
        section['topics'] = section_topics(section['heading'], section['text'])
    return sections


def heading_path(stack, level):
    """Ancestor headings, kept so a subsection still knows which release heading it falls under."""
    del stack[level:]
    return [h for h in stack if h]


def classify(url, title, text):
    hint = url + ' ' + title
    if re.search(r'migrat|upgrad', hint, re.I):
        return 'upgrade_guide'
    if re.search(r'deprecat', hint, re.I):
        return 'deprecation_notice'
    if re.search(r'changelog|release.?notes|/releases?/', hint, re.I):
        return 'release_notes'
    if re.search(r'/blog/|/news/', url, re.I):
        return 'blog_post'
    if re.search(r'/docs?/|documentation|\bdocs?\b|readme|\bapi\b', hint, re.I):
        return 'api_documentation'
    return 'supporting_document'


def html_document(body, url):
    soup = BeautifulSoup(body, 'html.parser')
    title = soup.title.get_text(' ', strip=True) if soup.title else url
    base_tag = soup.find('base', href=True)
    base = urljoin(url, base_tag['href']) if base_tag else url
    navigation = [(urljoin(base, a['href']), a.get_text(' ', strip=True)) for a in soup.select('nav a[href]')
                  if re.search(r'upgrad|migrat|changelog', a.get_text(' ', strip=True), re.I)]
    feeds = [urljoin(base, a['href']) for a in soup.select('link[href]')
             if a.get('type') in ('application/rss+xml', 'application/atom+xml')]
    for element in soup.select('script, style, nav, footer, header, aside, form'):
        element.decompose()
    root = soup.select_one('main, article, [role="main"], .chapter, .sect1') or soup.body or soup
    links = navigation + [(urljoin(base, a['href']), a.get_text(' ', strip=True) + ' ' + a.get('title', '')) for a in root.select('a[href]')]
    sections = []
    current = {'heading': title, 'anchor': None, 'text': '', 'heading_path': []}
    stack = []
    for item in root.find_all(['h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'p', 'pre', 'li', 'table']):
        if item.find_parent(['li', 'pre', 'table']):
            continue
        text = item.get_text(' ', strip=True)
        if not text:
            continue
        if item.name.startswith('h'):
            if current['text']:
                sections.append(current)
            anchor = item.get('id')
            if not anchor:
                nearby = item.find('a', attrs={'id': True}) or item.find('a', attrs={'name': True})
                anchor = nearby.get('id', nearby.get('name')) if nearby else None
            if not anchor and item.parent:
                ancestor = item.find_parent(attrs={'id': True})
                anchor = ancestor.get('id') if ancestor else None
            level = int(item.name[1])
            current = {'heading': text, 'anchor': anchor, 'text': '', 'heading_path': heading_path(stack, level - 1)}
            stack.extend([''] * (level - 1 - len(stack)) + [text])
        else:
            current['text'] += ('\n' if current['text'] else '') + text
    if current['text']:
        sections.append(current)
    if not sections:
        sections = [{'heading': title, 'anchor': None, 'text': root.get_text(' ', strip=True), 'heading_path': []}]
    annotate_sections(sections, url)
    text = '\n\n'.join(s['heading'] + '\n' + s['text'] for s in sections)
    return title, text, sections, links, feeds


def markdown_document(body, url):
    sections = []
    current = {'heading': url, 'anchor': None, 'text': '', 'heading_path': []}
    stack = []
    for line in body.splitlines():
        marker = re.match(r'^(#{1,6})\s+', line)
        if marker:
            if current['text']:
                sections.append(current)
            level, heading = len(marker.group(1)), re.sub(r'^#+\s+', '', line)
            current = {'heading': heading, 'anchor': None, 'text': '', 'heading_path': heading_path(stack, level - 1)}
            stack.extend([''] * (level - 1 - len(stack)) + [heading])
        else:
            current['text'] += line + '\n'
    if current['text']:
        sections.append(current)
    for section in sections:
        section['url'] = url
    annotate_sections(sections, url)
    links = [(urljoin(url, u), label) for label, u in re.findall(r'\[([^\]]*)\]\((https?://[^\s)]+)\)', body)]
    links += [(u.rstrip('.,'), '') for u in re.findall(r'https?://[^\s<>"\)]+', body)]
    return sections, links


def xml_links(body):
    if re.search(r'<!DOCTYPE|<!ENTITY', body, re.I):
        raise ValueError('XML declarations/entities are not supported')
    try:
        root = ElementTree.fromstring(body)
    except ElementTree.ParseError as exc:
        raise ValueError('Malformed sitemap or feed XML') from exc
    kind = root.tag.split('}')[-1]
    links = []
    for element in root.iter():
        tag = element.tag.split('}')[-1]
        if tag == 'loc' and element.text:
            links.append(element.text.strip())
        elif tag == 'link':
            value = element.get('href') or element.text
            if value:
                links.append(value.strip())
    return kind, links


def version_mentions(text, versions):
    # Mentions are evidence to review, not claims that every mentioned version is affected.
    labels = set(re.findall(r'(?<![\w.])v?(\d+(?:\.\d+){0,3})(?![\w.])', text))
    return [v for v in versions if v in labels]


def milestone_mentions(title, url, versions, milestones):
    """Versions whose documentation milestone (13 for 13.0.0) is named by the title or URL path, as in
    "Upgrading: Version 14" or /blog/next-13. Body text is left out: a bare major number there is too ambiguous."""
    probe = title + ' ' + re.sub(r'[/_-]+', ' ', re.sub(r'(?<=\d)[-_](?=\d)', '.', urlsplit(url).path))
    named = [m for m in milestones if re.search(r'(?<![\w.])v?' + re.escape(m) + r'(?![\w]|\.\d)', probe, re.I)]
    return [v for v in versions if any(numeric_version(v) == numeric_version(m) for m in named)]
