"""Behavioral tests for the mobile toolsets/MCP entry points (PR #7437, issue #1431).

The composer footer collapses in stages (_fitComposerFooter): full labels ->
`.cf-icons` -> `.cf-icons.cf-burger`. The toolsets picker has a different entry
point in each collapsed stage:

  * `.cf-icons`  — the footer chip, rendered as a 44px icon
  * `.cf-burger` — the chip is hidden; the mobile config panel action drives it

These tests drive the real `toggleToolsetsDropdown()` / `closeToolsetsDropdown()`
/ `_positionToolsetsDropdown()` sources from ui.js inside a Node VM against a
minimal DOM stub, asserting observable behavior rather than source strings:

  1. the picker opens from BOTH entry points,
  2. `aria-expanded` tracks the dropdown on whichever trigger opened it,
  3. the fixed bottom sheet is not given a stale inline `left`,
  4. tapping the mobile action is not treated as an outside click.

Each assertion fails against the pre-fix sources: the toggle gated on the chip's
`offsetParent` (dead action in burger mode), `_positionToolsetsDropdown()` wrote
a footer-relative inline `left` over the fixed sheet, and the outside-click
handler did not know the mobile action existed.
"""
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

NODE = shutil.which("node")
REPO = Path(__file__).parent.parent
# Overridable so the suite can be pointed at a pre-fix ui.js to confirm these
# tests actually fail before the change (AGENTS.md: "A test must fail before
# your fix and pass after it").
UI_JS_PATH = Path(os.environ.get("HERMES_TEST_UI_JS", REPO / "static" / "ui.js"))

pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")


def _slice_balanced(src: str, start: int) -> str:
    """Return src[start:] up to and including the brace-balanced block."""
    depth = 0
    for i in range(src.index("{", start), len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[start:i + 1]
    raise AssertionError("unbalanced braces")


def _function(src: str, name: str) -> str:
    m = re.search(r"^function %s\s*\(" % re.escape(name), src, re.M)
    assert m, f"{name}() not found in {UI_JS_PATH}"
    return _slice_balanced(src, m.start())


def _outside_click_handler(src: str) -> str:
    """Body of the document click listener that closes the toolsets dropdown."""
    marker = "// Click-outside handler for toolsets dropdown"
    i = src.find(marker)
    assert i != -1, "click-outside handler comment not found"
    j = src.index("function", i)
    return _slice_balanced(src, j)


def _run(stage: str, click_target: str | None = None) -> dict:
    """Drive the real toggle/close sources in a given collapse stage.

    stage: "icons"  -> chip rendered, mobile action hidden
           "burger" -> chip hidden, mobile action rendered
    click_target: id to synthesize an outside-click against, or None to skip.
    """
    src = UI_JS_PATH.read_text(encoding="utf-8")
    payload = {
        "stage": stage,
        "clickTarget": click_target,
        "sources": "\n".join([
            _function(src, "_activeToolsetsTrigger") if "_activeToolsetsTrigger" in src else "",
            _function(src, "_restoreToolsetsDropdownHome") if "_restoreToolsetsDropdownHome" in src else "",
            "let _toolsetsDropdownHome = null;" if "_toolsetsDropdownHome" in src else "",
            _function(src, "_positionToolsetsDropdown"),
            _function(src, "toggleToolsetsDropdown"),
            _function(src, "closeToolsetsDropdown"),
        ]),
        "outsideHandler": _outside_click_handler(src),
    }
    js = "const params = " + json.dumps(payload) + ";\n" + r"""
const stage = params.stage;

function makeEl(id, rendered) {
  const cls = new Set();
  return {
    id,
    // offsetParent === null is how the sources detect "hidden by CSS".
    offsetParent: rendered ? {} : null,
    // Present so the pre-fix anchoring path runs to completion. Without these
    // the icons-stage case would throw inside _positionToolsetsDropdown() and
    // "fail" for a stub reason rather than a behavioral one — that stage worked
    // before this change and its test must keep passing against both versions.
    getBoundingClientRect: () => ({ left: 44, right: 88, width: 44, height: 44 }),
    offsetWidth: 300,
    offsetHeight: 240,
    style: {},
    _attrs: {},
    classList: {
      add: (c) => cls.add(c),
      remove: (c) => cls.delete(c),
      contains: (c) => cls.has(c),
    },
    setAttribute(k, v) { this._attrs[k] = v; },
    getAttribute(k) { return this._attrs[k]; },
  };
}

const dd = makeEl('composerToolsetsDropdown', true);
const originalParent = { _kids: [], insertBefore(el) { this._kids.push(el); el.parentNode = this; },
                         appendChild(el) { this._kids.push(el); el.parentNode = this; } };
dd.parentNode = originalParent;
dd.nextSibling = null;
dd.scrollHeight = 240;
const chip = makeEl('composerToolsetsChip', stage === 'icons');
const action = makeEl('composerMobileToolsetsAction', stage === 'burger');
const els = {
  composerToolsetsDropdown: dd,
  composerToolsetsChip: chip,
  composerMobileToolsetsAction: action,
  composerMobileConfigBtn: makeEl('composerMobileConfigBtn', stage === 'burger'),
  toolsetsInput: null,
  toolsetsDropdownState: null,
};
const $ = (id) => els[id] || null;

// The footer exists in both collapsed stages; the sheet is fixed-positioned
// there, which is exactly what the positioning routine must respect.
const footer = {
  getBoundingClientRect: () => ({ left: 0, top: 700, bottom: 800 }),
  clientWidth: 390,
};
const body = { _children: [], appendChild(el) { this._children.push(el); el.parentNode = body; } };
const document = {
  body,
  querySelector: (sel) => (sel === '.composer-footer' ? footer : null),
};
// Phone viewport: the reparenting path is what we want to exercise.
const window = {
  matchMedia: (q) => ({ matches: q.indexOf('max-width:640px') !== -1 }),
  visualViewport: { width: 390, height: 800, offsetTop: 0, offsetLeft: 0 },
  innerWidth: 390,
  innerHeight: 800,
};
const getComputedStyle = () => ({ position: 'fixed' });

// Collaborators the toggle calls into; irrelevant to the behavior under test.
const closeProfileDropdown = () => {};
const closeWsDropdown = () => {};
const closeModelDropdown = () => {};
const closeReasoningDropdown = () => {};
const _syncToolsetsChip = () => {};
const _populateToolsetsDropdown = () => {};
const _renderToolsetsPresetSections = () => {};
const _loadToolsetsCatalog = () => ({ then: () => {} });
const _applySessionToolsets = () => {};
const showToast = () => {};
const t = (k) => k;

const runner = new Function(
  '$', 'document', 'window', 'getComputedStyle', 'setTimeout',
  'closeProfileDropdown', 'closeWsDropdown', 'closeModelDropdown',
  'closeReasoningDropdown', '_syncToolsetsChip', '_populateToolsetsDropdown',
  '_renderToolsetsPresetSections', '_loadToolsetsCatalog',
  '_applySessionToolsets', 'showToast', 't',
  params.sources + '\nreturn {toggleToolsetsDropdown, closeToolsetsDropdown, outside: ' + params.outsideHandler + '};'
);
const api = runner(
  $, document, window, getComputedStyle, () => {},
  closeProfileDropdown, closeWsDropdown, closeModelDropdown,
  closeReasoningDropdown, _syncToolsetsChip, _populateToolsetsDropdown,
  _renderToolsetsPresetSections, _loadToolsetsCatalog,
  _applySessionToolsets, showToast, t
);

// Seed a stale inline offset the way the pre-fix positioning routine would.
dd.style.left = '137px';

api.toggleToolsetsDropdown();

const openedBy = chip.classList.contains('active') ? 'chip'
  : action.classList.contains('active') ? 'action' : null;

let closedByOutsideClick = null;
if (params.clickTarget) {
  const target = { closest: (sel) => (sel === '#' + params.clickTarget ? {} : null) };
  api.outside({ target });
  closedByOutsideClick = !dd.classList.contains('open');
}

console.log(JSON.stringify({
  open: dd.classList.contains('open'),
  openedBy,
  reparentedToBody: dd.parentNode === body,
  floating: dd.classList.contains('composer-toolsets-dropdown--floating'),
  inlineLeft: dd.style.left,
  chipAria: chip.getAttribute('aria-expanded') || null,
  actionAria: action.getAttribute('aria-expanded') || null,
  closedByOutsideClick,
}));
"""
    r = subprocess.run([NODE, "-e", js], capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        raise RuntimeError(f"node failed: {r.stderr}")
    return json.loads(r.stdout.strip().splitlines()[-1])


class TestToolsetsEntryPoints:
    def test_opens_from_footer_chip_in_icons_stage(self):
        """`.cf-icons`: the icon chip opens the picker (pre-existing behavior)."""
        out = _run("icons")
        assert out["open"] is True, f"picker must open from the chip; got {out}"
        assert out["openedBy"] == "chip", f"chip should be the active trigger; got {out}"

    def test_opens_from_mobile_action_in_burger_stage(self):
        """`.cf-burger`: the panel action is the ONLY entry point and must work.

        Fails pre-fix: the toggle returned early on `chip.offsetParent === null`,
        which is exactly the state the chip is in during this stage.
        """
        out = _run("burger")
        assert out["open"] is True, (
            f"picker must open from the mobile config panel action; got {out}"
        )
        assert out["openedBy"] == "action", (
            f"the mobile action should be the active trigger; got {out}"
        )

    def test_aria_expanded_tracks_the_trigger_that_opened_it(self):
        """Whichever trigger opened the dropdown reports aria-expanded=true."""
        assert _run("icons")["chipAria"] == "true"
        assert _run("burger")["actionAria"] == "true"

    def test_phone_dropdown_escapes_the_footer_containing_block(self):
        """`.composer-footer` sets container-type:inline-size, which makes it the
        containing block for `position: fixed` descendants — a fixed dropdown left
        inside it resolves against the FOOTER, not the viewport, and lands below
        the fold (#6080). The picker must therefore be reparented to <body> and
        given viewport-relative coordinates, the same idiom as the model picker.

        Fails pre-fix: the dropdown stayed inside the footer and the routine wrote
        a footer-relative inline `left` over it.
        """
        for stage in ("icons", "burger"):
            out = _run(stage)
            assert out["reparentedToBody"] is True, (
                f"picker must escape the footer containing block in {stage}; got {out}"
            )
            assert out["floating"] is True, (
                f"picker must carry the floating modifier in {stage}; got {out}"
            )
            # Viewport-relative geometry, not a footer offset.
            assert out["inlineLeft"] == "8px", (
                f"left must be clamped to the viewport margin in {stage}; got {out}"
            )

    def test_tapping_the_mobile_action_is_not_an_outside_click(self):
        """The lifecycle must recognise the new trigger, or it closes instantly.

        Fails pre-fix: the handler only knew `#composerToolsetsChip` and
        `#composerToolsetsDropdown`, so the action counted as "outside".
        """
        out = _run("burger", click_target="composerMobileToolsetsAction")
        assert out["closedByOutsideClick"] is False, (
            f"clicking the mobile action must not close the picker; got {out}"
        )

    def test_outside_click_elsewhere_still_closes(self):
        """The guard must not become a blanket "never close"."""
        out = _run("burger", click_target="someUnrelatedThing")
        assert out["closedByOutsideClick"] is True, (
            f"a genuine outside click must still close the picker; got {out}"
        )
