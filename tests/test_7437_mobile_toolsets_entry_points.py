"""Behavioral tests for the collapsed-footer toolset control (PR #7437, issue #1431).

`.composer-toolsets-wrap` is revealed only by
`@container composer-footer (min-width: 1100px)`, which no phone reaches, so
session toolset restrictions had no mobile affordance at all — #1431 hid the
chip as a stopgap and left the redesign open.

This implements direction 2 from that issue, "surface only when active":

  * a default session shows no control in a collapsed footer, so it adds no
    width and cannot shift the collapse stage (which is what hides the context
    ring in `.cf-burger`);
  * once a restriction is set, the control appears — a 44px icon chip in
    `.cf-icons`, and a panel action plus a marker dot on the burger button in
    `.cf-burger`;
  * tapping it clears the restriction. There is deliberately no dropdown in a
    collapsed footer: choosing a specific toolset list stays a desktop
    affordance, with `/api/session/toolsets` unchanged for scripted callers.

The tests drive the real `_applyToolsetsChip()` and `activateToolsetsControl()`
sources from ui.js in a Node VM against a DOM stub, asserting observable
behavior rather than source strings. Against master they fail:
`activateToolsetsControl()` does not exist and the wrap is never marked, so a
collapsed footer can neither surface nor clear a restriction.
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
# Overridable so the suite can be pointed at master to confirm these tests fail
# before the change (AGENTS.md: "A test must fail before your fix and pass
# after it").
UI_JS_PATH = Path(os.environ.get("HERMES_TEST_UI_JS", REPO / "static" / "ui.js"))

pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")


def _slice_balanced(src: str, start: int) -> str:
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
    if not m:
        return ""
    return _slice_balanced(src, m.start())


def _run(stage: str, toolsets) -> dict:
    """Apply a toolset state in a given collapse stage, then activate the control.

    stage:    "icons" | "burger" | "desktop" (uncollapsed)
    toolsets: list -> a session restriction is active; None -> profile defaults
    """
    src = UI_JS_PATH.read_text(encoding="utf-8")
    apply_fn = _function(src, "_applyToolsetsChip")
    activate_fn = _function(src, "activateToolsetsControl")
    assert apply_fn, "_applyToolsetsChip() not found"
    payload = {
        "stage": stage,
        "toolsets": toolsets,
        "sources": "let _currentSessionToolsets = null;\n" + apply_fn + "\n" + activate_fn,
        "hasActivate": bool(activate_fn),
    }
    js = "const params = " + json.dumps(payload) + ";\n" + r"""
const stage = params.stage;

function makeEl(id) {
  const cls = new Set();
  return {
    id, style: {}, title: '', textContent: '',
    classList: {
      add: (c) => cls.add(c),
      remove: (c) => cls.delete(c),
      contains: (c) => cls.has(c),
      toggle: (c, on) => (on ? cls.add(c) : cls.delete(c)),
    },
  };
}

const wrap = makeEl('composerToolsetsWrap');
const chip = makeEl('composerToolsetsChip');
const label = makeEl('composerToolsetsLabel');
const action = makeEl('composerMobileToolsetsAction');
const actionLabel = makeEl('composerMobileToolsetsLabel');
const burgerBtn = makeEl('composerMobileConfigBtn');
const els = {
  composerToolsetsWrap: wrap, composerToolsetsChip: chip,
  composerToolsetsLabel: label, composerMobileToolsetsAction: action,
  composerMobileToolsetsLabel: actionLabel, composerMobileConfigBtn: burgerBtn,
};
const $ = (id) => els[id] || null;

const footerCls = new Set(
  stage === 'burger' ? ['cf-icons', 'cf-burger'] : stage === 'icons' ? ['cf-icons'] : []);
const footer = { classList: { contains: (c) => footerCls.has(c) } };
const document = { querySelector: (s) => (s === '.composer-footer' ? footer : null) };

let clearedToProfileDefaults = false;
let dropdownOpened = false;
const _applySessionToolsets = (v) => { if (v === null) clearedToProfileDefaults = true; };
const toggleToolsetsDropdown = () => { dropdownOpened = true; };
const t = (k) => k;
const S = { session: { session_id: 's1' } };

const runner = new Function(
  '$', 'document', 't', 'S', '_applySessionToolsets', 'toggleToolsetsDropdown',
  params.sources + '\nreturn { _applyToolsetsChip, activate: ' +
  (params.hasActivate ? 'activateToolsetsControl' : 'null') + ' };'
);
const api = runner($, document, t, S, _applySessionToolsets, toggleToolsetsDropdown);

api._applyToolsetsChip(params.toolsets);
// Visibility is decided by _applyToolsetsChip; activation is the tap itself.
const beforeTap = {
  wrapMarked: wrap.classList.contains('has-custom'),
  actionDisplay: action.style.display,
  burgerDot: burgerBtn.classList.contains('has-toolset-override'),
};
if (api.activate) api.activate();

console.log(JSON.stringify({
  wrapMarked: beforeTap.wrapMarked,
  actionDisplay: beforeTap.actionDisplay,
  burgerDot: beforeTap.burgerDot,
  clearedToProfileDefaults,
  dropdownOpened,
  hasActivate: params.hasActivate,
}));
"""
    r = subprocess.run([NODE, "-e", js], capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        raise RuntimeError(f"node failed: {r.stderr}")
    return json.loads(r.stdout.strip().splitlines()[-1])


class TestCollapsedToolsetControl:
    def test_default_session_shows_no_control_in_a_collapsed_footer(self):
        """No restriction -> no control, so the footer gains no width.

        This is what keeps the collapse stage — and with it the context ring
        that `.cf-burger` hides — exactly as master has it for the common case.
        """
        for stage in ("icons", "burger"):
            out = _run(stage, None)
            assert out["wrapMarked"] is False, f"wrap must not be marked in {stage}; got {out}"
            assert out["actionDisplay"] == "none", (
                f"panel action must stay hidden in {stage}; got {out}"
            )
            assert out["burgerDot"] is False, f"no marker without a restriction; got {out}"

    def test_active_restriction_surfaces_the_control(self):
        """A restriction makes it visible, and visibly marked, in both stages."""
        for stage in ("icons", "burger"):
            out = _run(stage, ["git", "web"])
            assert out["wrapMarked"] is True, f"wrap must be marked in {stage}; got {out}"
            assert out["actionDisplay"] != "none", (
                f"panel action must be revealed in {stage}; got {out}"
            )
            assert out["burgerDot"] is True, (
                f"burger button must carry the marker in {stage}; got {out}"
            )

    def test_tapping_clears_the_restriction_without_opening_a_picker(self):
        """The collapsed control is one-shot: clear, never a floating surface.

        No dropdown in a collapsed footer is the point — it is what keeps
        `.composer-footer`'s fixed-containing-block behaviour (#6080) off this
        path entirely.
        """
        for stage in ("icons", "burger"):
            out = _run(stage, ["git"])
            assert out["clearedToProfileDefaults"] is True, (
                f"tap must clear the restriction in {stage}; got {out}"
            )
            assert out["dropdownOpened"] is False, (
                f"tap must NOT open the picker in {stage}; got {out}"
            )

    def test_tapping_with_no_restriction_is_inert(self):
        """Nothing to clear -> no write. Guards against a stray reset call."""
        out = _run("icons", None)
        assert out["clearedToProfileDefaults"] is False, f"must not write; got {out}"
        assert out["dropdownOpened"] is False, f"must not open a picker; got {out}"

    def test_uncollapsed_footer_still_opens_the_full_picker(self):
        """Desktop is untouched: the wide footer keeps the dropdown it always had."""
        out = _run("desktop", ["git"])
        assert out["dropdownOpened"] is True, (
            f"uncollapsed footer must open the picker; got {out}"
        )
        assert out["clearedToProfileDefaults"] is False, (
            f"desktop must not clear on tap; got {out}"
        )
