"""Human-readable reports: a self-contained interactive HTML page per upgrade, and an index for a batch.

The page embeds final_output.json and renders it in the browser, with no external requests, so it works
from disk, offline or as an attachment. Quotes and titles come from public web pages: they are inserted as
text, never as HTML, and only http(s) URLs become links.
"""
import html
import json
import os
import sys
from pathlib import Path

from .common import get_logger

log = get_logger('report')


def embed(data):
    """JSON safe inside <script>: '</' cannot close the element and U+2028/9 cannot break the parser."""
    return (json.dumps(data, ensure_ascii=False).replace('</', '<\\/')
            .replace('\u2028', '\\u2028').replace('\u2029', '\\u2029'))


STYLE = """
/* Layout: overview band, then tabs; risks use a filter sidebar beside grouped, collapsible cards. */
:root{
  --bg:#f4f6f9;--panel:#ffffff;--sunken:#eef1f5;--fg:#18202c;--muted:#5b6575;--line:#dde2ea;--accent:#2457a6;--accent-soft:#e3ecf9;
  --critical:#b42318;--high:#c4520c;--medium:#a17a00;--low:#2c7a4b;--unknown:#6b7280;
  --ok:#2c7a4b;--warn:#a17a00;--bad:#b42318;--quote:#f6f7fa;--mark:#fff3bf;--shadow:0 1px 2px rgba(20,30,50,.06),0 2px 8px rgba(20,30,50,.05);
  --sans:system-ui,-apple-system,"Segoe UI",Roboto,"Helvetica Neue",sans-serif;--mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
  color-scheme:light}
@media (prefers-color-scheme:dark){:root{
  --bg:#10141a;--panel:#181d25;--sunken:#1f2530;--fg:#e3e8ef;--muted:#98a2b3;--line:#2a313d;--accent:#7ea8ea;--accent-soft:#1f2c42;
  --critical:#f0705e;--high:#f0955a;--medium:#dcbb56;--low:#67c18c;--unknown:#98a2b3;--ok:#67c18c;--warn:#dcbb56;--bad:#f0705e;
  --quote:#1d232c;--mark:#4a4220;--shadow:0 1px 2px rgba(0,0,0,.4);color-scheme:dark}}
*{box-sizing:border-box}
[hidden]{display:none!important}  /* layout rules such as display:grid must not reveal hidden tabs */
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.55 var(--sans)}
main{max-width:1240px;margin:0 auto;padding:28px 16px 80px}
a{color:var(--accent);text-underline-offset:2px}
h1{font-size:1.75rem;line-height:1.2;margin:2px 0 8px;text-wrap:balance}
h2{font-size:1.05rem;margin:0 0 10px}
h3{font-size:.8rem;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);margin:18px 0 8px}
code,.mono{font-family:var(--mono);font-size:.86em}
.muted{color:var(--muted)}.small{font-size:.84rem}
.eyebrow{font-size:.78rem;text-transform:uppercase;letter-spacing:.08em;color:var(--muted)}
.chips{display:flex;flex-wrap:wrap;gap:6px;align-items:center}
.chip{display:inline-flex;align-items:center;gap:6px;padding:2px 10px;border-radius:999px;background:var(--sunken);font-size:.8rem;white-space:nowrap}
.dot{width:9px;height:9px;border-radius:50%;flex:none;background:var(--unknown)}
.d-critical{background:var(--critical)}.d-high{background:var(--high)}.d-medium{background:var(--medium)}.d-low{background:var(--low)}
.c-high{background:var(--ok)}.c-medium{background:var(--warn)}.c-low{background:var(--bad)}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:10px;box-shadow:var(--shadow)}
/* overview */
.overview{display:grid;grid-template-columns:minmax(0,1.4fr) minmax(0,1fr);gap:14px;margin:20px 0}
.overview .panel{padding:14px 16px}
.sevbar{display:flex;height:12px;border-radius:6px;overflow:hidden;background:var(--sunken);margin:8px 0}
.sevbar span{display:block}
.legend{display:flex;flex-wrap:wrap;gap:12px;font-size:.84rem}
.legend span{display:inline-flex;align-items:center;gap:6px;font-variant-numeric:tabular-nums}
.top{list-style:none;margin:6px 0 0;padding:0}
.top li{display:flex;gap:8px;align-items:baseline;padding:5px 0;border-top:1px solid var(--line)}
.top li:first-child{border-top:0}
.top button{all:unset;cursor:pointer;color:var(--fg);flex:1;min-width:0}.top button:hover{color:var(--accent);text-decoration:underline}
.kpis{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px}
.kpi b{display:block;font-size:1.5rem;font-variant-numeric:tabular-nums;line-height:1.1}.kpi span{font-size:.8rem;color:var(--muted)}
details.fold>summary{cursor:pointer;color:var(--muted);font-size:.86rem;margin-top:10px}
/* tabs */
nav.tabs{display:flex;gap:2px;border-bottom:1px solid var(--line);margin:8px 0 18px;overflow-x:auto}
nav.tabs button{background:none;border:0;border-bottom:3px solid transparent;padding:10px 14px;font:inherit;color:var(--muted);cursor:pointer;white-space:nowrap}
nav.tabs button[aria-selected=true]{color:var(--fg);border-bottom-color:var(--accent);font-weight:600}
nav.tabs .n{font-size:.75rem;background:var(--sunken);border-radius:999px;padding:0 7px;margin-left:6px}
/* risks layout */
.layout{display:grid;grid-template-columns:260px minmax(0,1fr);gap:20px;align-items:start}
.sidebar{position:sticky;top:12px;max-height:calc(100vh - 24px);overflow:auto;padding:14px}
.sidebar>summary{font-weight:600;cursor:pointer;list-style:none}
.sidebar>summary::-webkit-details-marker{display:none}
.field{margin:12px 0}.field>label:not(.facet),.field .label{display:block;font-size:.78rem;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);margin-bottom:6px}
input[type=search],select{width:100%;font:inherit;padding:7px 9px;border:1px solid var(--line);border-radius:7px;background:var(--panel);color:var(--fg)}
.facet{display:flex;align-items:center;gap:8px;padding:3px 4px;border-radius:6px;cursor:pointer;font-size:.88rem}
.facet:hover{background:var(--sunken)}.facet input{margin:0}.facet .count{margin-left:auto;color:var(--muted);font-variant-numeric:tabular-nums}
.btn{font:inherit;font-size:.84rem;padding:5px 10px;border:1px solid var(--line);border-radius:7px;background:var(--panel);color:var(--fg);cursor:pointer}
.btn:hover{border-color:var(--accent)}.btn.link{border:0;background:none;color:var(--accent);padding:2px 4px}
.toolbar{display:flex;flex-wrap:wrap;gap:8px;align-items:center;justify-content:space-between;margin-bottom:10px}
/* groups and cards */
details.group{margin:0 0 14px}
details.group>summary{display:flex;align-items:center;gap:10px;cursor:pointer;padding:8px 2px;font-weight:600;list-style:none}
details.group>summary::-webkit-details-marker,details.card>summary::-webkit-details-marker{display:none}
details.group>summary::before,details.card>summary .caret::before{content:"▸";color:var(--muted);transition:transform .15s;display:inline-block}
details.group[open]>summary::before,details.card[open]>summary .caret::before{transform:rotate(90deg)}
details.card{border-left:4px solid var(--unknown);margin:8px 0}
details.card[data-level=critical]{border-left-color:var(--critical)}details.card[data-level=high]{border-left-color:var(--high)}
details.card[data-level=medium]{border-left-color:var(--medium)}details.card[data-level=low]{border-left-color:var(--low)}
details.card>summary{cursor:pointer;list-style:none;padding:12px 14px;display:grid;grid-template-columns:auto minmax(0,1fr);gap:4px 10px}
details.card>summary .caret{grid-row:span 3;padding-top:2px}
.card-title{font-weight:600}
.preview{color:var(--muted);font-size:.88rem;display:-webkit-box;-webkit-line-clamp:1;-webkit-box-orient:vertical;overflow:hidden}
details.card[open] .preview{display:none}
.card-body{padding:4px 18px 16px 34px;border-top:1px solid var(--line)}
.callout{background:var(--sunken);border-left:3px solid var(--warn);border-radius:6px;padding:8px 10px;font-size:.86rem;margin:10px 0}
.cond{display:grid;grid-template-columns:auto minmax(0,1fr);gap:4px 10px;padding:8px 0;border-top:1px dashed var(--line)}
.cond:first-of-type{border-top:0}
.role{font-size:.72rem;font-weight:700;text-transform:uppercase;letter-spacing:.05em;padding:2px 8px;border-radius:5px;background:var(--sunken);height:fit-content;white-space:nowrap}
.role-required{background:var(--accent-soft);color:var(--accent)}.role-disqualifying{background:var(--sunken);color:var(--ok)}
.role-uncertain{background:var(--sunken);color:var(--warn)}
.cond .expect{font-family:var(--mono);font-size:.82rem;color:var(--muted)}
.cite{font:inherit;font-size:.75rem;font-weight:600;color:var(--accent);background:var(--accent-soft);border:0;border-radius:4px;padding:0 5px;margin-left:3px;cursor:pointer;vertical-align:1px}
details.why>summary{font-size:.8rem;color:var(--muted);cursor:pointer;margin-top:4px}
details.why>div{font-size:.84rem;color:var(--muted);padding:4px 0 0 2px}
ol.steps,ul.checks{margin:4px 0;padding-left:22px}ol.steps li,ul.checks li{margin:3px 0}
.patterns{display:flex;flex-wrap:wrap;gap:6px;margin-top:6px}
.pattern{display:inline-flex;align-items:center;border:1px solid var(--line);border-radius:6px;overflow:hidden;max-width:100%}
.pattern code{padding:2px 8px;background:var(--sunken);overflow-wrap:anywhere}
.pattern button{font:inherit;font-size:.74rem;border:0;border-left:1px solid var(--line);background:var(--panel);color:var(--accent);cursor:pointer;padding:2px 8px}
details.evidence>summary{cursor:pointer;font-size:.8rem;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);margin:16px 0 6px}
.quote{display:grid;grid-template-columns:auto minmax(0,1fr);gap:2px 10px;padding:8px 10px;border-radius:7px;background:var(--quote);margin:6px 0;transition:background .4s}
.quote.flash{background:var(--mark)}
.quote .num{font-weight:700;color:var(--accent);font-size:.82rem}
.quote blockquote{margin:0;white-space:pre-wrap;overflow-wrap:anywhere}
.quote .src{grid-column:2;font-size:.8rem;color:var(--muted);overflow-wrap:anywhere}
.card-foot{display:flex;flex-wrap:wrap;gap:8px;align-items:center;justify-content:space-between;margin-top:14px;font-size:.8rem;color:var(--muted)}
.empty{color:var(--muted);padding:24px 0;text-align:center}
/* other tabs */
.list-row{display:flex;gap:10px;align-items:baseline;padding:8px 0;border-top:1px solid var(--line)}
.list-row:first-child{border-top:0}.list-row .grow{flex:1;min-width:0}
details.section{padding:12px 14px;margin:0 0 12px}
details.section>summary{cursor:pointer;font-weight:600}
table{border-collapse:collapse;width:100%}th,td{border-bottom:1px solid var(--line);padding:8px;text-align:left;vertical-align:top}
th{font-size:.76rem;text-transform:uppercase;letter-spacing:.05em;color:var(--muted);font-weight:600}
.scroll{overflow-x:auto}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px;border-radius:4px}
@media (max-width:900px){.overview{grid-template-columns:minmax(0,1fr)}.layout{grid-template-columns:minmax(0,1fr)}
  .sidebar{position:static;max-height:none}.kpis{grid-template-columns:repeat(3,minmax(0,1fr))}.card-body{padding-left:16px}}
@media (prefers-reduced-motion:reduce){*{transition:none!important}}
"""

SCRIPT = r"""
const data = JSON.parse(document.getElementById('report-data').textContent);
const LEVELS = ['critical', 'high', 'medium', 'low', 'unknown'];
const CONFIDENCE = ['high', 'medium', 'low'];
const ROLE_LABEL = {required: 'requires', optional: 'raises risk', alternative: 'or', disqualifying: 'unless', uncertain: 'maybe'};
const paths = data.risk_paths || [];
const sources = Object.fromEntries((data.source_inventory || []).map(s => [s.source_id, s]));
const $ = (sel, root = document) => root.querySelector(sel);

function el(tag, attrs, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === 'class') node.className = v;
    else if (k.startsWith('on')) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v === true ? '' : v);
  }
  for (const c of children.flat(Infinity)) if (c !== null && c !== undefined && c !== false) node.append(c instanceof Node ? c : document.createTextNode(String(c)));
  return node;
}
// DOM append() would stringify arrays and nulls; every container is filled through add().
const add = (node, ...kids) => { node.append(...kids.flat(Infinity).filter(k => k !== null && k !== undefined && k !== false)); return node; };
const safeUrl = (u) => (typeof u === 'string' && /^https?:\/\//i.test(u)) ? u : null;
const link = (u, text) => safeUrl(u) ? el('a', {href: u, target: '_blank', rel: 'noopener noreferrer'}, text || u) : (text || u || '');
const pretty = (s) => String(s ?? '').replaceAll('_', ' ');
const plural = (n, word) => `${n} ${word}${n === 1 ? '' : 's'}`;
const levelDot = (l) => el('span', {class: 'dot d-' + l, title: l + ' risk'});
const confChip = (c) => c ? el('span', {class: 'chip', title: 'confidence'}, el('span', {class: 'dot c-' + c}), c + ' confidence') : null;
const summaryBody = () => (data.evidence_summary || {}).evidence_summary;
const debounce = (fn, ms = 150) => { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; };

// ---------- overview ----------
function overview() {
  const p = data.provenance || {};
  $('#title').textContent = `${data.project} ${data.current_version} → ${data.target_version}`;
  add($('#meta'), 
    el('span', {class: 'chip'}, el('span', {class: 'dot c-' + data.overall_confidence}), `overall confidence: ${data.overall_confidence}`),
    el('span', {class: 'chip'}, p.verified ? `verified · judge: ${p.verification_judge || 'none'}` : 'not verified'),
    (p.extraction_models || []).length ? el('span', {class: 'chip'}, 'extraction: ' + p.extraction_models.join(', ')) : null,
    p.generated_at ? el('span', {class: 'chip'}, 'generated ' + p.generated_at.replace('T', ' ').slice(0, 16) + ' UTC') : null);

  const byLevel = Object.fromEntries(LEVELS.map(l => [l, paths.filter(x => x.risk_level === l).length]));
  const types = Object.entries(paths.reduce((acc, x) => (acc[x.risk_type] = (acc[x.risk_type] || 0) + 1, acc), {})).sort((a, b) => b[1] - a[1]);
  const severe = [...paths].sort(rankSort).slice(0, 5);
  const body = summaryBody();
  add($('#overview'), 
    el('section', {class: 'panel'},
      el('h2', {}, `${plural(paths.length, 'risk path')}`),
      el('div', {class: 'sevbar', role: 'img', 'aria-label': 'risk paths by level'},
        LEVELS.filter(l => byLevel[l]).map(l => el('span', {style: `flex:${byLevel[l]};background:var(--${l})`, title: `${byLevel[l]} ${l}`}))),
      el('div', {class: 'legend'}, LEVELS.filter(l => byLevel[l]).map(l => el('span', {}, levelDot(l), `${byLevel[l]} ${l}`))),
      el('h3', {}, 'Main categories'),
      el('div', {class: 'chips'}, types.slice(0, 6).map(([t, n]) => el('span', {class: 'chip'}, `${pretty(t)} · ${n}`))),
      el('h3', {}, 'Most severe'),
      el('ul', {class: 'top'}, severe.map(x => el('li', {}, levelDot(x.risk_level),
        el('button', {type: 'button', onclick: () => reveal(x.path_id)}, x.path_name), confDotOnly(x.confidence)))),
      el('details', {class: 'fold'}, el('summary', {}, 'Generated summary'), el('p', {class: 'small'}, data.summary))),
    el('section', {class: 'panel'},
      el('h2', {}, 'Confidence and coverage'),
      el('div', {class: 'kpis'},
        kpi(body ? body.high_confidence_risks.length : '—', 'high confidence'),
        kpi(body ? body.medium_confidence_risks.length : '—', 'medium confidence'),
        kpi(body ? body.low_confidence_or_disputed_risks.length : '—', 'low or disputed'),
        kpi((data.source_inventory || []).length, 'sources'),
        kpi(body ? body.sources_with_disagreements.length : '—', 'source disagreements'),
        kpi((data.open_questions || []).length, 'open questions')),
      el('p', {class: 'small muted'}, body ? 'Confidence comes from re-checked quotes, an entailment judge and source trust; disputed risks are listed as low.'
        : 'Verification did not run, so confidence is preliminary.')));
}
const kpi = (n, label) => el('div', {class: 'kpi'}, el('b', {}, n), el('span', {}, label));
const confDotOnly = (c) => c ? el('span', {class: 'dot c-' + c, title: c + ' confidence'}) : null;
function rankSort(a, b) {
  return LEVELS.indexOf(a.risk_level) - LEVELS.indexOf(b.risk_level)
    || CONFIDENCE.indexOf(a.confidence) - CONFIDENCE.indexOf(b.confidence) || a.path_name.localeCompare(b.path_name);
}

// ---------- risk cards ----------
function evidenceIndex(p) {
  // Each quote is shown once per path; conditions and steps cite it by number.
  const list = [], key = new Map();
  const ref = (e) => {
    const k = (e.source_id || '') + '\u0000' + e.quote_or_summary;
    if (!key.has(k)) { list.push(e); key.set(k, list.length); }
    return key.get(k);
  };
  const conds = (p.conditions || []).map(c => ({c, refs: [...new Set((c.evidence || []).map(ref))]}));
  const steps = (p.migration_steps || []).map(s => ({s, refs: [...new Set((s.evidence || []).map(ref))]}));
  return {list, conds, steps};
}
function cites(path, refs) {
  return refs.map(n => el('button', {class: 'cite', type: 'button', title: 'Show quote ' + n, onclick: (ev) => { ev.preventDefault(); flash(path, n); }}, n));
}
function flash(path, n) {
  const box = document.getElementById(`ev-${path.path_id}`);
  if (box) box.open = true;
  const q = document.getElementById(`q-${path.path_id}-${n}`);
  if (!q) return;
  q.scrollIntoView({block: 'center', behavior: 'smooth'});
  q.classList.add('flash'); setTimeout(() => q.classList.remove('flash'), 1400);
}
function copy(text, button) {
  const label = button.textContent;
  const done = () => { button.textContent = 'copied'; setTimeout(() => button.textContent = label, 1200); };
  (navigator.clipboard ? navigator.clipboard.writeText(text) : Promise.reject()).then(done, () => {
    const range = document.createRange(); range.selectNodeContents(button.previousSibling || button);
    const sel = getSelection(); sel.removeAllRanges(); sel.addRange(range);
  });
}
function cardBody(p) {
  const {list, conds, steps} = evidenceIndex(p);
  const patterns = [...new Set((p.conditions || []).flatMap(c => c.search_patterns || []))];
  const notes = [...(p.flags || []).map(pretty), ...new Set((p.conditions || []).flatMap(c => c.ambiguities || []))];
  return el('div', {class: 'card-body'},
    el('p', {}, p.description),
    (p.flags || []).length ? el('div', {class: 'callout'}, 'Review: ' + p.flags.map(pretty).join(' · ')) : null,
    el('h3', {}, 'When this applies'),
    conds.map(({c, refs}) => el('div', {class: 'cond'},
      el('span', {class: 'role role-' + c.condition_role, title: pretty(c.condition_role)}, ROLE_LABEL[c.condition_role] || c.condition_role),
      el('div', {},
        el('div', {}, c.statement, cites(p, refs), ' ', confDotOnly(c.confidence)),
        el('div', {class: 'expect'}, `${pretty(c.category)}: ${pretty(c.operator)} ${c.expected_value || ''}`),
        el('details', {class: 'why'}, el('summary', {}, 'Why and how it was checked'),
          el('div', {},
            c.reasoning ? el('div', {}, c.reasoning) : null,
            c.verification ? el('div', {}, `Quote ${pretty(c.verification.grounding)} in the source · judge: ${pretty(c.verification.entailment)}`
              + ` · ${plural(c.verification.independent_sources, 'independent source')}`
              + (c.verification.conflicts.length ? ' · disagreements: ' + c.verification.conflicts.join(', ') : '')) : el('div', {}, 'Not verified.'),
            el('div', {}, `${c.confidence} confidence`)))))),
    steps.length ? [el('h3', {}, 'How to migrate'),
      el('ol', {class: 'steps'}, steps.map(({s, refs}) => el('li', {}, s.step, cites(p, refs))))] : null,
    (p.suggested_actions || []).length || patterns.length ? [el('h3', {}, 'How to check'),
      (p.suggested_actions || []).length ? el('ul', {class: 'checks'}, p.suggested_actions.map(a => el('li', {}, a))) : null,
      patterns.length ? el('div', {class: 'patterns'}, patterns.map(x => el('span', {class: 'pattern'}, el('code', {}, x),
        el('button', {type: 'button', title: 'Copy search pattern', onclick: (ev) => copy(x, ev.target)}, 'copy')))) : null] : null,
    notes.length ? [el('h3', {}, 'Notes'), el('ul', {class: 'checks small'}, notes.map(n => el('li', {}, n)))] : null,
    el('details', {class: 'evidence', id: `ev-${p.path_id}`},
      el('summary', {}, `Evidence · ${plural(list.length, 'quote')} from ${plural(new Set(list.map(e => e.source_id)).size, 'source')}`),
      list.map((e, i) => { const s = sources[e.source_id] || {};
        return el('div', {class: 'quote', id: `q-${p.path_id}-${i + 1}`}, el('span', {class: 'num'}, i + 1),
          el('blockquote', {}, e.quote_or_summary),
          el('div', {class: 'src'}, link(e.url || s.url, s.title || e.url), ` · ${pretty(e.source_type || s.source_type)}`,
            e.trust_tier ? ` · ${pretty(e.trust_tier)}` : '', (e.versions || []).length ? ` · ${e.versions.join(', ')}` : '')); })),
    el('div', {class: 'card-foot'},
      el('span', {}, (p.change_kinds || []).length ? 'Change: ' + p.change_kinds.map(pretty).join(', ') : ''),
      el('button', {class: 'btn link', type: 'button', onclick: (ev) => copy(location.href.split('#')[0] + '#' + p.path_id, ev.target)}, 'copy link')));
}
function card(p) {
  const n = new Set((p.conditions || []).flatMap(c => (c.evidence || []).map(e => e.source_id))).size;
  const node = el('details', {class: 'card panel', 'data-level': p.risk_level, id: p.path_id},
    el('summary', {},
      el('span', {class: 'caret', 'aria-hidden': 'true'}),
      el('span', {class: 'card-title'}, p.path_name),
      el('span', {class: 'preview'}, p.description),
      el('span', {class: 'chips'}, el('span', {class: 'chip'}, levelDot(p.risk_level), p.risk_level),
        el('span', {class: 'chip'}, pretty(p.risk_type)), el('span', {class: 'chip'}, pretty(p.affected_surface)), confChip(p.confidence),
        el('span', {class: 'small muted'}, `${plural((p.conditions || []).length, 'condition')} · ${plural(n, 'source')}`))));
  node.addEventListener('toggle', () => { if (node.open && !node.dataset.rendered) { node.append(cardBody(p)); node.dataset.rendered = '1'; } });
  return node;
}

// ---------- filters, grouping, sorting ----------
const state = {q: '', levels: new Set(), conf: new Set(), types: new Set(), surfaces: new Set(), sort: 'severity', group: 'level'};
function facet(title, key, values, counts, decorate) {
  return el('div', {class: 'field'}, el('span', {class: 'label'}, title),
    values.map(v => el('label', {class: 'facet'},
      el('input', {type: 'checkbox', value: v, onchange: (ev) => { ev.target.checked ? state[key].add(v) : state[key].delete(v); render(); }}),
      decorate ? decorate(v) : null, pretty(v), el('span', {class: 'count'}, counts[v]))));
}
function sidebar() {
  const count = (k) => paths.reduce((acc, x) => (acc[x[k]] = (acc[x[k]] || 0) + 1, acc), {});
  const uniq = (k) => Object.keys(count(k)).sort();
  const box = $('#sidebar');
  add(box, el('summary', {}, 'Filter and sort'),
    el('div', {class: 'field'}, el('label', {for: 'q'}, 'Search'),
      el('input', {id: 'q', type: 'search', placeholder: 'Names, conditions, quotes…  ( / )', oninput: debounce((ev) => { state.q = ev.target.value.toLowerCase(); render(); })})),
    facet('Risk level', 'levels', LEVELS.filter(l => count('risk_level')[l]), count('risk_level'), levelDot),
    facet('Confidence', 'conf', CONFIDENCE.filter(c => count('confidence')[c]), count('confidence'), (c) => el('span', {class: 'dot c-' + c})),
    facet('Risk type', 'types', uniq('risk_type'), count('risk_type')),
    facet('Affected surface', 'surfaces', uniq('affected_surface'), count('affected_surface')),
    el('div', {class: 'field'}, el('label', {for: 'sort'}, 'Sort'), el('select', {id: 'sort', onchange: (ev) => { state.sort = ev.target.value; render(); }},
      el('option', {value: 'severity'}, 'Severity, then confidence'), el('option', {value: 'confidence'}, 'Confidence, then severity'), el('option', {value: 'name'}, 'Name'))),
    el('div', {class: 'field'}, el('label', {for: 'group'}, 'Group by'), el('select', {id: 'group', onchange: (ev) => { state.group = ev.target.value; render(); }},
      el('option', {value: 'level'}, 'Risk level'), el('option', {value: 'risk_type'}, 'Risk type'), el('option', {value: 'affected_surface'}, 'Affected surface'), el('option', {value: 'none'}, 'No grouping'))),
    el('button', {class: 'btn', type: 'button', onclick: clearFilters}, 'Clear filters'));
  if (matchMedia('(max-width: 900px)').matches) box.open = false;
}
function clearFilters() {
  for (const k of ['levels', 'conf', 'types', 'surfaces']) state[k].clear();
  state.q = ''; document.querySelectorAll('#sidebar input').forEach(i => i.type === 'search' ? i.value = '' : i.checked = false);
  render();
}
function matches(p) {
  return (!state.levels.size || state.levels.has(p.risk_level)) && (!state.conf.size || state.conf.has(p.confidence))
    && (!state.types.size || state.types.has(p.risk_type)) && (!state.surfaces.size || state.surfaces.has(p.affected_surface))
    && (!state.q || JSON.stringify(p).toLowerCase().includes(state.q));
}
function sorter() {
  if (state.sort === 'name') return (a, b) => a.path_name.localeCompare(b.path_name);
  if (state.sort === 'confidence') return (a, b) => CONFIDENCE.indexOf(a.confidence) - CONFIDENCE.indexOf(b.confidence) || rankSort(a, b);
  return rankSort;
}
function render() {
  const shown = paths.filter(matches).sort(sorter());
  const active = state.levels.size + state.conf.size + state.types.size + state.surfaces.size + (state.q ? 1 : 0);
  $('#count').textContent = `${shown.length} of ${plural(paths.length, 'risk path')}` + (active ? ` · ${plural(active, 'filter')} active` : '');
  const list = $('#paths'); list.replaceChildren();
  if (!shown.length) { list.append(el('p', {class: 'empty'}, 'No risk paths match. ', el('button', {class: 'btn link', type: 'button', onclick: clearFilters}, 'Clear filters'))); return; }
  if (state.group === 'none') { shown.forEach(p => list.append(card(p))); return; }
  const key = state.group === 'level' ? 'risk_level' : state.group;
  const order = state.group === 'level' ? LEVELS : [...new Set(shown.map(p => p[key]))].sort();
  let opened = 0;
  for (const value of order) {
    const items = shown.filter(p => p[key] === value);
    if (!items.length) continue;
    const open = active > 0 || opened < 2;  // the two most severe groups start open, every group when filtering
    opened += 1;
    const group = el('details', {class: 'group', open},
      el('summary', {}, state.group === 'level' ? levelDot(value) : null, `${pretty(value)}${state.group === 'level' ? ' risk' : ''}`,
        el('span', {class: 'chip'}, items.length)));
    const fill = () => { if (!group.dataset.rendered) { items.forEach(p => group.append(card(p))); group.dataset.rendered = '1'; } };
    if (open) fill(); else group.addEventListener('toggle', fill);
    list.append(group);
  }
}
function setAll(open) {
  document.querySelectorAll('#paths details.group').forEach(g => { g.open = open; });
  if (open) document.querySelectorAll('#paths details.card').forEach(c => { c.open = true; });
  else document.querySelectorAll('#paths details.card').forEach(c => { c.open = false; });
}
function reveal(id) {
  showTab('risks');
  if (!document.getElementById(id)) { clearFilters(); }
  const p = paths.find(x => x.path_id === id);
  if (!p) return;
  const groups = [...document.querySelectorAll('#paths details.group')];
  for (const g of groups) if (!g.open && g.textContent.toLowerCase().includes('')) { /* open lazily below */ }
  let node = document.getElementById(id);
  if (!node) { groups.forEach(g => { g.open = true; g.dispatchEvent(new Event('toggle')); }); node = document.getElementById(id); }
  if (!node) return;
  node.closest('details.group') && (node.closest('details.group').open = true);
  node.open = true; node.dispatchEvent(new Event('toggle'));
  node.scrollIntoView({block: 'start', behavior: 'smooth'});
  try { history.replaceState(null, '', '#' + id); } catch (e) {}
}

// ---------- other tabs ----------
function evidenceTab() {
  const box = $('#tab-evidence'), body = summaryBody();
  if (!body) { add(box, el('p', {class: 'empty'}, 'Verification did not run for this upgrade, so there is no evidence summary.')); return; }
  const bucket = (title, items, open) => el('details', {class: 'section panel', open},
    el('summary', {}, `${title} `, el('span', {class: 'chip'}, items.length)),
    items.length ? el('div', {}, items.map(r => el('div', {class: 'list-row'}, levelDot(r.risk_level),
      el('div', {class: 'grow'}, el('button', {class: 'btn link', type: 'button', onclick: () => reveal(r.path_id)}, r.path_name),
        (r.reasons || []).length ? el('div', {class: 'small muted'}, r.reasons.slice(0, 3).join(' · ')) : null)))) : el('p', {class: 'empty'}, 'None.'));
  add(box, 
    bucket('High-confidence risks', body.high_confidence_risks, true),
    bucket('Medium-confidence risks', body.medium_confidence_risks, false),
    bucket('Low-confidence or disputed risks', body.low_confidence_or_disputed_risks, false),
    el('details', {class: 'section panel'}, el('summary', {}, 'Source disagreements ', el('span', {class: 'chip'}, body.sources_with_disagreements.length)),
      body.sources_with_disagreements.length ? body.sources_with_disagreements.map(d => el('div', {class: 'list-row'},
        el('span', {class: 'chip'}, pretty(d.kind)),
        el('div', {class: 'grow'}, el('b', {}, d.subject), ` · ${d.resolution}`, el('div', {class: 'small muted'}, d.reason),
          el('ul', {class: 'checks small'}, d.claims.map(c => el('li', {}, `${pretty(c.change)} in ${c.versions.join(', ')} (${pretty(c.trust_tier)})`,
            c.fact_id === d.winner_fact_id ? ' · preferred' : '', ' · ', link(c.url, 'source'))))))) : el('p', {class: 'empty'}, 'None.')));
}
function sourcesTab() {
  const box = $('#tab-sources'), all = data.source_inventory || [];
  const tiers = [...new Set(all.map(s => s.trust_tier))];
  const tbody = el('tbody');
  let tier = '', q = '';
  const draw = () => { tbody.replaceChildren(...all.filter(s => (!tier || s.trust_tier === tier)
    && (!q || JSON.stringify(s).toLowerCase().includes(q))).map(s => el('tr', {},
    el('td', {}, link(s.url, s.title || s.url)), el('td', {}, pretty(s.source_type)), el('td', {}, pretty(s.trust_tier)),
    el('td', {class: 'mono small'}, (s.versions || []).slice(0, 4).join(', ') + ((s.versions || []).length > 4 ? ` +${s.versions.length - 4}` : '')),
    el('td', {}, s.cited_by_risk_paths),
    el('td', {}, el('details', {class: 'why'}, el('summary', {}, 'why relevant'), el('div', {}, s.why_relevant, el('div', {}, 'Trust: ' + s.trust_basis))))))); };
  add(box, el('div', {class: 'toolbar'},
      el('input', {type: 'search', placeholder: 'Search sources…', style: 'max-width:320px', oninput: debounce((ev) => { q = ev.target.value.toLowerCase(); draw(); })}),
      el('select', {style: 'max-width:240px', onchange: (ev) => { tier = ev.target.value; draw(); }}, el('option', {value: ''}, 'All trust tiers'), tiers.map(t => el('option', {value: t}, pretty(t))))),
    el('div', {class: 'panel scroll'}, el('table', {}, el('thead', {}, el('tr', {}, ['Source', 'Type', 'Trust', 'Versions', 'Cited by', ''].map(h => el('th', {}, h)))), tbody)));
  draw();
}
function contextTab() {
  const box = $('#tab-context'), facts = data.contextual_facts || [];
  const byChange = facts.reduce((acc, f) => ((acc[f.change] = acc[f.change] || []).push(f), acc), {});
  const questions = data.open_questions || [], diagnostics = data.diagnostics || [];
  add(box, 
    el('details', {class: 'section panel', open: true}, el('summary', {}, 'Open questions ', el('span', {class: 'chip'}, questions.length)),
      questions.length ? el('ul', {class: 'checks'}, questions.map(q => el('li', {}, q))) : el('p', {class: 'empty'}, 'None.')),
    el('h2', {}, 'Contextual facts'),
    el('p', {class: 'muted small'}, 'Useful facts that are not upgrade risks: context, relaxations, and fixes shipped by the upgrade.'),
    ...Object.entries(byChange).sort((a, b) => b[1].length - a[1].length).map(([change, items]) => {
      const section = el('details', {class: 'section panel'}, el('summary', {}, `${pretty(change)} `, el('span', {class: 'chip'}, items.length)));
      section.addEventListener('toggle', () => { if (section.open && !section.dataset.rendered) { section.dataset.rendered = '1';
        section.append(...items.map(f => el('div', {class: 'list-row'}, el('div', {class: 'grow'}, f.statement,
          f.known_incompatibility ? [' ', el('span', {class: 'chip'}, 'known incompatibility')] : null,
          el('div', {class: 'small muted'}, (f.evidence || []).map(e => link(e.url, (sources[e.source_id] || {}).title || 'source')))))));
      } });
      return section;
    }),
    el('details', {class: 'section panel'}, el('summary', {}, 'Run diagnostics ', el('span', {class: 'chip'}, diagnostics.length)),
      el('p', {class: 'muted small'}, 'How this run went: budgets, rejected extractions and failed model calls. These describe the pipeline, not the upgrade.'),
      diagnostics.length ? el('ul', {class: 'checks small'}, diagnostics.map(d => el('li', {}, d))) : el('p', {class: 'empty'}, 'None.')));
}

// ---------- tabs and keys ----------
function showTab(name) {
  for (const b of document.querySelectorAll('nav.tabs button')) {
    const on = b.dataset.tab === name; b.setAttribute('aria-selected', on); document.getElementById('tab-' + b.dataset.tab).hidden = !on;
  }
}
function tabs() {
  const counts = {risks: paths.length, sources: (data.source_inventory || []).length, context: (data.contextual_facts || []).length + (data.open_questions || []).length};
  for (const b of document.querySelectorAll('nav.tabs button')) {
    if (counts[b.dataset.tab] !== undefined) b.append(el('span', {class: 'n'}, counts[b.dataset.tab]));
    b.addEventListener('click', () => { showTab(b.dataset.tab); try { history.replaceState(null, '', '#' + b.dataset.tab); } catch (e) {} });
  }
  const hash = decodeURIComponent(location.hash.slice(1));
  if (['risks', 'evidence', 'sources', 'context'].includes(hash)) showTab(hash);
  else { showTab('risks'); if (hash && paths.some(p => p.path_id === hash)) reveal(hash); }
}
document.addEventListener('keydown', (ev) => {
  const typing = /INPUT|SELECT|TEXTAREA/.test(document.activeElement.tagName);
  if (ev.key === '/' && !typing) { ev.preventDefault(); showTab('risks'); $('#sidebar').open = true; $('#q').focus(); }
  if (ev.key === 'Escape' && document.activeElement.id === 'q') { $('#q').value = ''; state.q = ''; render(); }
});

overview(); sidebar(); render(); evidenceTab(); sourcesTab(); contextTab(); tabs();
$('#expand').addEventListener('click', () => setAll(true));
$('#collapse').addEventListener('click', () => setAll(false));
"""

PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title><style>{style}</style></head>
<body><main>
<div class="eyebrow">Upgrade risk report</div>
<h1 id="title"></h1>
<div id="meta" class="chips"></div>
<div id="overview" class="overview"></div>
<nav class="tabs" role="tablist">
<button type="button" role="tab" data-tab="risks">Risk paths</button>
<button type="button" role="tab" data-tab="evidence">Evidence summary</button>
<button type="button" role="tab" data-tab="sources">Sources</button>
<button type="button" role="tab" data-tab="context">Context and open questions</button>
</nav>
<section id="tab-risks" class="layout">
<details id="sidebar" class="sidebar panel" open></details>
<div>
<div class="toolbar"><span id="count" class="muted"></span>
<span><button type="button" class="btn" id="expand">Expand all</button> <button type="button" class="btn" id="collapse">Collapse all</button></span></div>
<div id="paths"></div>
</div>
</section>
<section id="tab-evidence" hidden></section>
<section id="tab-sources" hidden></section>
<section id="tab-context" hidden></section>
<noscript><p>This report renders with JavaScript; the same data is in final_output.json.</p></noscript>
</main>
<script type="application/json" id="report-data">{data}</script>
<script>{script}</script>
</body></html>
"""


def build_report(final_output):
    title = f"{final_output['project']} {final_output['current_version']} → {final_output['target_version']} upgrade risks"
    return PAGE.format(title=html.escape(title), style=STYLE, data=embed(final_output), script=SCRIPT)


def write_report(output_dir, manifest, final_output):
    path = Path(output_dir) / 'report.html'
    path.write_text(build_report(final_output))
    manifest['files']['report'] = 'report.html'
    log.info('wrote %s', path)
    return path


def build_index(rows):
    """Batch overview linking each upgrade's report; rows: dicts with project, versions, counts and a relative href."""
    items = ''.join(
        f"<tr><td><a href=\"{html.escape(r['href'])}\">{html.escape(r['project'])} {html.escape(r['current_version'])} → "
        f"{html.escape(r['target_version'])}</a></td><td>{r['paths']}</td><td>{r['critical']}</td><td>{r['high']}</td>"
        f"<td>{html.escape(r['confidence'])}</td><td>{r['open_questions']}</td></tr>" for r in rows)
    body = (f"<main><p class=\"muted\">Upgrade risk reports</p><h1>{len(rows)} upgrades</h1><div class=\"scroll\"><table>"
            "<thead><tr><th>Upgrade</th><th>Risk paths</th><th>Critical</th><th>High</th><th>Overall confidence</th>"
            f"<th>Open questions</th></tr></thead><tbody>{items}</tbody></table></div></main>")
    return (f"<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,"
            f"initial-scale=1\"><title>Upgrade risk reports</title><style>{STYLE}</style></head><body>{body}</body></html>\n")


def index_row(final_output, href):
    paths = final_output['risk_paths']
    return {'project': final_output['project'], 'current_version': final_output['current_version'],
            'target_version': final_output['target_version'], 'href': href, 'paths': len(paths),
            'critical': sum(p['risk_level'] == 'critical' for p in paths), 'high': sum(p['risk_level'] == 'high' for p in paths),
            'confidence': final_output['overall_confidence'], 'open_questions': len(final_output['open_questions'])}


def main(argv=None):
    """Write report.html for saved case directories, and index.html when given several:
    upgrade-pipeline report DIR... [--index PATH]"""
    import argparse
    parser = argparse.ArgumentParser(prog='upgrade-pipeline report', description=main.__doc__)
    parser.add_argument('directories', nargs='+', type=Path, help='Case directories containing final_output.json')
    parser.add_argument('--index', type=Path, help='Also write a batch index here (default: none for one directory)')
    args = parser.parse_args(argv)
    rows = []
    for directory in args.directories:
        output = json.loads((directory / 'final_output.json').read_text())
        path = write_report(directory, {'files': {}}, output)
        rows.append((output, path))
    if args.index or len(rows) > 1:
        index = args.index or args.directories[0].resolve().parent / 'index.html'
        index.write_text(build_index([index_row(o, os.path.relpath(p, index.parent)) for o, p in rows]))
        log.info('wrote %s', index)
    return 0


if __name__ == '__main__':
    sys.exit(main())
