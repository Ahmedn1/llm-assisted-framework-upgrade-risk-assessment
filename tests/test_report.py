import json
import re

from upgrade_pipeline.final_output import build_final_output
from upgrade_pipeline.report import build_index, build_report, index_row, main
from pipeline_fixtures import run


def final_output():
    verified, _, _ = run()
    return build_final_output(verified)


def embedded(page):
    return json.loads(re.search(r'<script type="application/json" id="report-data">(.*?)</script>', page, re.S).group(1))


def test_report_embeds_the_final_output_and_loads_nothing_external():
    output = final_output()
    page = build_report(output)
    assert embedded(page) == output
    assert '<title>Orbit 1 → 2 upgrade risks</title>' in page
    assert not re.search(r'<(?:script|link|img)[^>]+(?:src|href)=', page)  # self-contained: no external requests


def test_hostile_page_text_cannot_break_out_of_the_embedded_data():
    output = final_output()
    quote = '</script><script>alert(1)</script> '
    output['risk_paths'][0]['conditions'][0]['evidence'][0]['quote_or_summary'] = quote
    page = build_report(output)
    assert page.count('</script>') == 2  # only the page's own two script elements close
    assert embedded(page)['risk_paths'][0]['conditions'][0]['evidence'][0]['quote_or_summary'] == quote
    assert 'innerHTML' not in page  # page text is only ever inserted as text


def test_report_command_writes_reports_and_a_batch_index(tmp_path):
    output = final_output()
    for name in ('01-orbit', '02-comet'):
        (tmp_path / name).mkdir()
        (tmp_path / name / 'final_output.json').write_text(json.dumps({**output, 'project': name[3:].title()}))
    assert main([str(tmp_path / '01-orbit'), str(tmp_path / '02-comet')]) == 0
    assert (tmp_path / '01-orbit' / 'report.html').exists()
    index = (tmp_path / 'index.html').read_text()
    assert 'href="01-orbit/report.html"' in index and 'Comet 1 → 2' in index


def test_index_escapes_names():
    row = index_row({**final_output(), 'project': '<b>x</b>'}, 'a.html')
    assert '&lt;b&gt;x&lt;/b&gt;' in build_index([row])


def test_pipeline_writes_the_report_by_default(tmp_path, monkeypatch):
    from upgrade_pipeline.postprocess import finalize
    verified, _, _ = run()
    manifest = {'files': {}}
    finalize(tmp_path, manifest, verified)
    assert manifest['files'] == {'final_output': 'final_output.json', 'report': 'report.html'}
    assert embedded((tmp_path / 'report.html').read_text())['project'] == 'Orbit'
