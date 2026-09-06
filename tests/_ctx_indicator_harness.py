"""Shared node harness for the composer context-usage indicator.

Every visible string in the indicator now goes through ``t()``, so a harness
that stubs ``t`` as ``key => key`` can only ever assert on key names. This one
loads the real English bundle out of ``static/i18n.js`` instead, which keeps
the assertions readable *and* fails loudly when a key is missing from the
bundle — a missing key would otherwise surface to users as raw ``ctx_*`` text
in the tooltip.

The harness renders both surfaces the indicator drives:

* the composer ring (``#ctxPercent`` / ``#ctxRingValue``) plus its tooltip, and
* the ring drawn on the burger config button (``#ctx-arc`` / ``#ctx-num``),
  which shows the same number on narrow layouts.
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UI_JS = ROOT / "static" / "ui.js"
I18N_JS = ROOT / "static" / "i18n.js"

# Ids the harness stubs. Anything the indicator touches must be listed, or the
# code under test silently takes its "element missing" branch.
_NODE_IDS = (
    "ctxIndicatorWrap", "ctxIndicator", "ctxRingValue", "ctxPercent",
    "ctxTooltipUsage", "ctxTooltipTokens", "ctxTooltipThreshold", "ctxTooltipCost",
    "ctxTooltipCompress", "ctxCompressBtn",
    "composerMobileConfigBtn", "composerMobileContextAction", "composerMobileContextUsage",
    "composerMobileContextTokens", "composerMobileContextThreshold", "composerMobileContextCost",
    "composerMobileCtxCompressBtn", "composerMobileCtxRing", "ctx-arc", "ctx-num", "msg",
)


def english_locale_strings() -> dict[str, str]:
    """Return the English ``LOCALES.en`` bundle as a plain dict.

    Parsed with a regex rather than a JS engine: the bundle is a flat map of
    single-quoted literals, and importing i18n.js would drag in browser globals.
    """
    src = I18N_JS.read_text(encoding="utf-8")
    start = src.index("  en: {")
    end = src.index("\n  it: {", start)
    block = src[start:end]
    pairs = re.findall(r"(?m)^\s*([A-Za-z_][A-Za-z0-9_]*):\s*'((?:[^'\\]|\\.)*)',?\s*$", block)
    return {key: value.replace("\\'", "'").replace("\\\\", "\\") for key, value in pairs}


def _slice(src: str, start_marker: str, end_marker: str) -> str:
    start = src.index(start_marker)
    end = src.index(end_marker, start)
    return src[start:end]


def _indicator_source() -> str:
    """Extract the indicator's functions from ui.js, in dependency order."""
    src = UI_JS.read_text(encoding="utf-8")
    return "".join((
        # colour-step constants, _ctxUsageLevel/_applyCtxUsageLevel, _setMobileCtxRing
        _slice(src, "const _MOBILE_CONFIG_BASE_LABEL", "function _setCtxCompressButton"),
        _slice(src, "function _setCtxCompressButton", "function _syncMobileCtxDisplay"),
        _slice(src, "function _syncMobileCtxDisplay", "function _mergeUsageForCtxIndicator"),
        _slice(src, "function _syncCtxIndicator", "// ── Touch support"),
    ))


_HARNESS_JS = r"""
const EN = %(strings)s;
const nodes = {};
const mk = id => ({
  id, style: {}, attrs: {}, textContent: '',
  classList: {
    _s: new Set(),
    add(c){ this._s.add(c); }, remove(c){ this._s.delete(c); },
    toggle(c, on){ if (on) this._s.add(c); else this._s.delete(c); },
    contains(c){ return this._s.has(c); },
  },
  setAttribute(name, value){ this.attrs[name] = value; },
  getAttribute(name){ return this.attrs[name]; },
  removeAttribute(name){ delete this.attrs[name]; },
});
for (const id of %(ids)s) nodes[id] = mk(id);
global.$ = id => nodes[id] || null;
global.window = {};
global.autoResize = () => {};
global._fmtTokens = n => (!n || n < 0) ? '0'
  : (n >= 1e6 ? (n / 1e6).toFixed(1) + 'M' : (n >= 1e3 ? (n / 1e3).toFixed(1) + 'k' : String(n)));
global.t = (key, ...args) => {
  const val = EN[key];
  if (val === undefined) return 'MISSING_I18N_KEY:' + key;
  return args.length
    ? String(val).replace(/\{(\d+)\}/g, (m, i) => args[i] !== undefined ? String(args[i]) : m)
    : val;
};

%(source)s

_syncCtxIndicator(%(usage)s);

const classes = el => [...el.classList._s].sort();
console.log(JSON.stringify({
  percent: nodes.ctxPercent.textContent,
  label: nodes.ctxIndicator.attrs['aria-label'],
  usage: nodes.ctxTooltipUsage.textContent,
  tokens: nodes.ctxTooltipTokens.textContent,
  threshold: nodes.ctxTooltipThreshold.textContent,
  cost: nodes.ctxTooltipCost.textContent,
  indicator_classes: classes(nodes.ctxIndicator),
  indicator_hidden: nodes.ctxIndicatorWrap.style.display === 'none',
  compress: nodes.ctxCompressBtn.textContent,
  ring_number: nodes['ctx-num'].textContent,
  ring_dashoffset: nodes['ctx-arc'].attrs['stroke-dashoffset'],
  ring_stroke_attr: nodes['ctx-arc'].attrs['stroke'] ?? null,
  ring_classes: classes(nodes.composerMobileCtxRing),
  mobile_row_classes: classes(nodes.composerMobileContextAction),
  mobile_row_hidden: nodes.composerMobileContextAction.style.display === 'none',
  mobile_usage: nodes.composerMobileContextUsage.textContent,
}));
"""


def render_context_indicator(usage: dict) -> dict:
    """Run the real indicator code over ``usage`` and return what it rendered."""
    script = _HARNESS_JS % {
        "strings": json.dumps(english_locale_strings(), ensure_ascii=False),
        "ids": json.dumps(list(_NODE_IDS)),
        "source": _indicator_source(),
        "usage": json.dumps(usage),
    }
    result = subprocess.run(
        ["node", "-e", script],
        cwd=ROOT, check=True, capture_output=True, text=True, encoding="utf-8",
    )
    return json.loads(result.stdout)
