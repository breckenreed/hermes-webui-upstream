"""The two context rings show the same number, so they must agree about it.

The composer ring and the ring on the burger config button render the same
percentage. They disagreed in three ways:

* with no last-prompt data the composer ring drew the honest "·" while the
  burger ring drew a green 0, claiming an empty context (#1436 again — the
  text badge the ring replaced got this right);
* the burger ring turned red only above 85 while every other surface showing
  that number turned red above 75; and
* its three colours were hardcoded hex values, identical in all 15 themes.

The tooltip's own lines were English-only while the compress button inside it
was translated, so those are pinned here too.
"""
from __future__ import annotations

import re
from pathlib import Path

from tests._ctx_indicator_harness import english_locale_strings, render_context_indicator

ROOT = Path(__file__).resolve().parents[1]
UI_JS = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")
CSS = (ROOT / "static" / "style.css").read_text(encoding="utf-8")
HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
I18N_JS = (ROOT / "static" / "i18n.js").read_text(encoding="utf-8")
BOOT_JS = (ROOT / "static" / "boot.js").read_text(encoding="utf-8")
SESSIONS_JS = (ROOT / "static" / "sessions.js").read_text(encoding="utf-8")

LOCALE_COUNT = 15
BURGER_RING_CIRCUMFERENCE = 87.96

CTX_KEYS = (
    "ctx_window_label", "ctx_next_model_label", "ctx_indicator_aria", "ctx_usage_aria",
    "ctx_usage_used", "ctx_usage_exceeded", "ctx_tokens_used", "ctx_tokens_of_window",
    "ctx_in_out", "ctx_est_window", "ctx_auto_compress_at", "ctx_estimated_cost",
)


# ── Both rings agree ─────────────────────────────────────────────────────────

def test_no_last_prompt_data_reads_as_unknown_on_both_rings():
    rendered = render_context_indicator(
        {"input_tokens": 120_000, "output_tokens": 4_000, "context_length": 200_000}
    )

    assert rendered["percent"] == "\N{MIDDLE DOT}"
    assert rendered["ring_number"] == "\N{MIDDLE DOT}", (
        "a 0 on the burger ring reads as 'context is empty', which is not what "
        "missing last-prompt data means"
    )
    assert float(rendered["ring_dashoffset"]) == BURGER_RING_CIRCUMFERENCE, (
        "the arc must be empty, not a full green circle"
    )
    assert rendered["ring_classes"] == []
    assert rendered["indicator_classes"] == []


def test_both_rings_turn_red_at_the_same_point():
    rendered = render_context_indicator(
        {"last_prompt_tokens": 160_000, "input_tokens": 160_000, "context_length": 200_000}
    )

    assert rendered["percent"] == "80"
    assert rendered["ring_number"] == "80"
    assert rendered["indicator_classes"] == ["ctx-high"]
    assert rendered["ring_classes"] == ["ctx-high"], (
        "80% used is past the high step everywhere else, so the burger ring "
        "cannot still be amber"
    )
    assert rendered["mobile_row_classes"] == ["ctx-high"]


def test_amber_step_matches_across_surfaces():
    rendered = render_context_indicator(
        {"last_prompt_tokens": 120_000, "input_tokens": 120_000, "context_length": 200_000}
    )

    assert rendered["percent"] == "60"
    assert rendered["indicator_classes"] == ["ctx-mid"]
    assert rendered["ring_classes"] == ["ctx-mid"]


def test_overflow_clamps_the_ring_but_not_the_tooltip():
    rendered = render_context_indicator(
        {"last_prompt_tokens": 260_000, "input_tokens": 260_000, "context_length": 200_000}
    )

    assert rendered["ring_number"] == "100"
    assert float(rendered["ring_dashoffset"]) == 0
    assert "130% used" in rendered["usage"]
    assert "context exceeded" in rendered["usage"]


def test_colour_steps_have_a_single_source():
    """One pair of constants drives every surface's colour and the compress copy."""
    assert "const CTX_USAGE_MID_PCT=50;" in UI_JS
    assert "const CTX_USAGE_HIGH_PCT=75;" in UI_JS
    for literal in ("pct>50&&pct<=75", "pct>75", "pct>=75?", "pct>=50?"):
        assert literal not in UI_JS.replace(" ", ""), (
            f"open-coded threshold {literal!r} must go through the shared constants"
        )


# ── Theme tokens, not hardcoded hex ──────────────────────────────────────────

def test_burger_ring_colour_comes_from_the_theme():
    for hardcoded in ("#22c55e", "#f97316", "#ef4444"):
        assert hardcoded not in UI_JS, (
            f"{hardcoded} ignores all {LOCALE_COUNT}-theme palettes; the ring must "
            "take its colour from a class"
        )
        assert hardcoded not in HTML, "the ring markup must not preset a colour either"

    assert ".composer-mobile-ctx-arc{stroke:var(--success);" in CSS
    assert ".composer-mobile-ctx-ring.ctx-mid .composer-mobile-ctx-arc{stroke:var(--warning);}" in CSS
    assert ".composer-mobile-ctx-ring.ctx-high .composer-mobile-ctx-arc{stroke:var(--error);}" in CSS


def test_ring_updates_do_not_write_a_stroke_attribute():
    rendered = render_context_indicator(
        {"last_prompt_tokens": 160_000, "input_tokens": 160_000, "context_length": 200_000}
    )

    assert rendered["ring_stroke_attr"] is None, (
        "an inline stroke attribute would pin one colour across every theme"
    )


# ── The tooltip speaks the user's language ───────────────────────────────────

def test_every_locale_defines_the_context_meter_strings():
    for key in CTX_KEYS:
        assert I18N_JS.count(f"    {key}: ") == LOCALE_COUNT, (
            f"{key} is missing from at least one locale bundle"
        )
    # The compress button inside the same tooltip had a locale gap of its own.
    for key in ("ctx_compress_hint", "ctx_compress_action"):
        assert I18N_JS.count(f"    {key}: ") == LOCALE_COUNT


def test_tooltip_lines_are_not_english_literals_in_the_source():
    for literal in ("Auto-compress at ", "Estimated cost: $", "'Context window'",
                    "'Estimated next model context'", "% used (context exceeded)"):
        assert literal not in UI_JS, (
            f"{literal!r} must come from the locale bundle, not the source"
        )
    assert 'data-i18n="ctx_window_label"' in HTML
    assert 'data-i18n-aria-label="ctx_indicator_aria"' in HTML


def test_rendered_tooltip_resolves_every_key():
    rendered = render_context_indicator({
        "last_prompt_tokens": 160_000, "input_tokens": 160_000, "output_tokens": 4_000,
        "context_length": 200_000, "threshold_tokens": 180_000, "estimated_cost": 1.5,
    })

    for field in ("label", "usage", "tokens", "threshold", "cost", "compress"):
        assert "MISSING_I18N_KEY" not in (rendered[field] or ""), (
            f"{field} rendered an unresolved key: {rendered[field]!r}"
        )
    assert rendered["usage"] == "Context window: 80% used (20% left)"
    assert rendered["threshold"] == "Auto-compress at 180.0k (90%)"


def test_english_bundle_uses_numbered_placeholders():
    english = english_locale_strings()
    for key, expected in (("ctx_usage_used", 3), ("ctx_usage_exceeded", 2),
                          ("ctx_tokens_of_window", 3), ("ctx_in_out", 2)):
        found = {int(i) for i in re.findall(r"\{(\d+)\}", english[key])}
        assert found == set(range(expected)), (
            f"{key} must consume placeholders 0..{expected - 1}, got {sorted(found)}"
        )


# ── One assembler for the counter half of the payload ────────────────────────

def test_every_hydration_path_uses_the_shared_counters():
    """The model-change path used to omit the cache fields and blank the tooltip."""
    assert BOOT_JS.count("_ctxIndicatorUsageCounters(") == 1
    assert SESSIONS_JS.count("_ctxIndicatorUsageCounters(") == 2

    start = UI_JS.index("function _ctxIndicatorUsageCounters")
    end = UI_JS.index("// Context usage indicator in composer footer", start)
    helper = UI_JS[start:end]
    for field in ("input_tokens", "output_tokens", "estimated_cost",
                  "cache_read_tokens", "cache_write_tokens", "cache_hit_percent"):
        assert f"{field}:pick(" in helper, f"{field} must be assembled centrally"
