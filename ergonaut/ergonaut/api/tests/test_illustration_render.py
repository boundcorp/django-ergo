"""Structural tests for illustration pages: they must stay visual, sourced and safe.

These pin behaviour a reader can see (components, citations, omitted figures,
escaping), not CSS text.
"""

import os
import re
from datetime import UTC, datetime
from html.parser import HTMLParser

import pytest
from pydantic import ValidationError

from ergonaut.api.illustrate import depth_for
from ergonaut.api.illustration_render import render_illustration
from ergonaut.api.illustration_spec import IllustrationSpec, spec_model
from ergonaut.api.tests import illustration_fixtures as fx

WHEN = datetime(2026, 10, 16, 20, 30, tzinfo=UTC)
VISUAL = ["timeline", "board", "flow", "dia-svg", "cmp", "cards", "stats", "hbar"]


ALLOWED_TAGS = set(
    "html head meta title style body main header div h1 h2 h3 h4 p ul ol li span a nav aside section article footer q b small "
    "dl dt dd table caption thead tbody tr th td details summary svg defs marker path g rect text polyline line circle".split()
)


def attributes(html: str) -> list[tuple[str, str]]:
    """Every (tag, attribute name) in the page, as a browser would parse it."""
    found = []

    class Collector(HTMLParser):
        def handle_starttag(self, tag, attrs):
            found.append((tag, ""))
            found.extend((tag, name) for name, _ in attrs)

    Collector().feed(html)
    return found


def render(spec=None, topics=None, evidence=None, **kwargs):
    return render_illustration(
        spec or fx.build_spec(),
        topics=fx.TOPICS if topics is None else topics,
        evidence=fx.EVIDENCE if evidence is None else evidence,
        as_of_line=fx.AS_OF_LINE,
        generated_at=WHEN,
        **kwargs,
    )


def page(**kwargs) -> str:
    return render(**kwargs).html.decode()


def test_representative_page_is_a_designed_visual_page_not_text():
    html = page()
    used = [name for name in VISUAL if re.search(rf'class="[^"]*\b{name}\b', html)]
    # Timeline, status board, flow, diagram, comparison, cards, stats and a bar chart all render.
    assert used == VISUAL
    assert html.count("<svg") >= 1
    assert html.count("<h1>") == 1
    assert len(html) > 20_000
    # Prose is the exception: most panels carry a visual component, and the hero has a digest.
    panels = html.count('<section class="panel">')
    prose_only = len(
        re.findall(r'<section class="panel"><h\d>[^<]*</h\d>(?:<p class="intro">[^<]*</p>)?<div class="body"><p>', html)
    )
    assert panels >= 9
    assert prose_only <= panels // 4
    assert "The short version" in html
    assert re.search(r'<ol class="take">|<aside class="take">', html)


def test_each_selected_topic_gets_a_numbered_band_and_nav_entry():
    html = page()
    for number, topic in enumerate(fx.TOPICS, 1):
        assert f'<div class="eyebrow">{number:02d} / {topic["title"]}</div>' in html
        assert f'href="#topic-{topic["id"]}"' in html
        assert f'id="topic-{topic["id"]}"' in html
    levels = [int(level) for level in re.findall(r"<h([1-6])[ >]", html)]
    assert levels[0] == 1
    assert all(b - a <= 1 for a, b in zip(levels, levels[1:], strict=False)), "heading levels skip"


def test_single_topic_page_has_no_topic_bands():
    spec = fx.spec_dict()
    spec["blocks"] = [block for block in spec["blocks"] if block["topic"] == "status"]
    html = page(spec=IllustrationSpec.model_validate(spec), topics=fx.TOPICS[1:2])
    assert 'class="band"' not in html
    assert "<h2>Status by item</h2>" in html


def test_citations_link_to_verbatim_sources_and_drop_unknown_lines():
    spec = fx.spec_dict()
    spec["blocks"][0]["evidence_refs"] = [2556, 999]
    spec["blocks"][0]["items"][0]["evidence_refs"] = [31337]
    rendered = render(spec=IllustrationSpec.model_validate(spec))
    html = rendered.html.decode()
    assert "#999" not in html and "#31337" not in html
    targets = set(re.findall(r'href="#src-(\d+)"', html))
    assert targets == {str(line) for line in rendered.cited}
    for line in targets:
        assert f'<li id="src-{line}">' in html
    # The excerpt is the stored message, not model text.
    assert "I want it to look designed, not like a memo." in html
    assert set(map(int, targets)) <= {m["line"] for m in fx.EVIDENCE}


def test_unsourced_figures_are_omitted_with_an_explicit_note():
    rendered = render()
    html = rendered.html.decode()
    # Sourced: 3 and 8 days appear in message 2565, so they are shown.
    assert 'class="v">3 days</dd>' in html
    assert 'class="v">8 days</dd>' in html
    assert re.search(r'<span class="val">8 days</span>', html)
    # Not sourced: 420 ms is in no message; the line chart's 41/38 are not in message 2562.
    assert "420 ms" not in html
    assert "polyline" not in html
    assert "Generation latency over time" in html and "No figures to show" in html
    assert any("Generation latency over time" in note for note in rendered.omitted)
    assert 'class="caveats"' in html and "left out" in html


def test_figures_never_appear_when_no_message_states_numbers():
    evidence = [{"line": line, "role": "user", "text": "No numbers here."} for line in (1, 2)]
    spec = fx.spec_dict()
    for block in spec["blocks"]:
        for key in ("points",):
            for point in block.get(key, []):
                point["evidence_ref"] = 1
        for stat in block.get("stats", []):
            stat["evidence_ref"] = 1
    html = page(spec=IllustrationSpec.model_validate(spec), evidence=evidence)
    assert 'class="hbar"' not in html and "polyline" not in html and 'class="stat"' not in html
    assert html.count("No figures to show") == 3


def test_sourced_line_chart_renders_with_its_values_as_a_table():
    evidence = [{"line": 5, "role": "user", "text": "Latency by day: Mon 41 s, Tue 38 s, Wed 35 s."}]
    spec = fx.spec_dict()
    spec["blocks"] = [
        {
            "type": "chart",
            "topic": "status",
            "heading": "Latency",
            "chart": "line",
            "unit": "s",
            "points": [
                {"label": day, "value": value, "evidence_ref": 5}
                for day, value in (("Mon", 41), ("Tue", 38), ("Wed", 35))
            ],
        }
    ]
    html = page(spec=IllustrationSpec.model_validate(spec), topics=fx.TOPICS[1:2], evidence=evidence)
    assert html.count("<circle") == 3 and "polyline" in html
    assert '<th scope="row">Tue</th><td>38 s</td>' in html


def test_model_text_is_escaped_in_every_component():
    attack = "<script>alert(1)</script><img src=x onerror=alert(2)>"
    short = "<img src=x onerror=1>"  # for fields capped near 30 characters
    spec = fx.spec_dict()
    for block in spec["blocks"]:
        block["heading"] = block["intro"] = attack
        for item in block.get("items", []) + block.get("steps", []) + block.get("cards", []):
            item["title"] = attack
        for node in block.get("nodes", []):
            node["label"], node["detail"] = short, attack
        for row in block.get("rows", []):
            row["label"] = attack
            row["cells"] = [attack] * len(row["cells"])
        for stat in block.get("stats", []):
            stat["label"] = attack
        for point in block.get("points", []):
            point["label"] = short
        if "columns" in block:
            block["columns"] = [short] * len(block["columns"])
        if "body" in block:
            block["body"] = attack
        if "unit" in block:
            block["unit"] = "<b onclick=1>"
    spec["title"] = spec["summary"] = spec["kicker"] = attack
    spec["takeaways"] = [attack]
    spec["caveats"] = [attack]
    evidence = [{**message, "text": attack} for message in fx.EVIDENCE]
    html = page(spec=IllustrationSpec.model_validate(spec), evidence=evidence, model=attack)
    assert "<script" not in html.lower().replace("&lt;script", "")
    assert "<img" not in html and "<b onclick" not in html
    assert not [name for tag, name in attributes(html) if name.startswith("on")]
    assert {tag for tag, _ in attributes(html)} <= ALLOWED_TAGS
    assert html.count("&lt;script&gt;") > 20


def test_page_is_self_contained_and_links_stay_in_page():
    html = page()
    assert "<script" not in html and "javascript:" not in html
    assert not re.search(r"(?:src|href)=\"(?!#)", html)
    assert "url(http" not in html and "@import" not in html
    assert "<meta name='viewport' content='width=device-width,initial-scale=1'>" in html
    assert "prefers-color-scheme:dark" in html


def test_states_and_diagrams_have_text_not_just_colour():
    html = page()
    for label in ("Done", "In progress", "Blocked", "Needs a decision", "Next"):
        assert f"</span> {label}</span>" in html
    # Diagram: an accessible name that lists the connections, plus a stacked text version for phones.
    assert re.search(
        r'<svg[^>]*role="img"[^>]*aria-label="Who talks to whom: Chat thread \+ files → Suggestion call \(reads\)', html
    )
    assert 'class="dia-stages"' in html and "Connections as text" in html
    # Tables: row/column headers, and per-cell labels so the phone layout can stack them.
    assert '<th scope="col">Typed components</th>' in html and 'data-label="Typed components"' in html


def test_diagram_handles_cycles_skipped_layers_and_long_chains():
    nodes = [{"id": f"n{i}", "label": f"Step {i}"} for i in range(8)]
    edges = [{"source": f"n{i}", "target": f"n{i + 1}"} for i in range(7)]
    edges += [{"source": "n0", "target": "n3", "label": "shortcut"}, {"source": "n7", "target": "n0", "label": "again"}]
    spec = fx.spec_dict()
    spec["blocks"] = [{"type": "diagram", "topic": "plan", "heading": "Loop", "nodes": nodes, "edges": edges}]
    html = page(spec=IllustrationSpec.model_validate(spec), topics=fx.TOPICS[2:3])
    assert html.count('class="node ') == 8
    assert 'class="edge back"' in html and 'class="edge skip"' in html
    assert html.count('<section class="stage">') == 8
    width = int(re.search(r'viewBox="0 0 (\d+) ', html).group(1))
    assert width < 700, "long chains run top to bottom instead of shrinking the text"


def test_thin_or_text_only_specs_are_rejected_so_the_model_must_add_visuals():
    topics = ["timeline", "status"]
    model = spec_model(topics, blocks_min=4, visuals_min=2)
    prose = {"type": "prose", "heading": "h", "body": "b"}
    text_only = {"title": "t", "summary": "s", "blocks": [{**prose, "topic": "timeline"}] * 5}
    with pytest.raises(ValidationError, match="visual blocks"):
        model.model_validate(text_only)
    one_topic = fx.spec_dict()
    one_topic["blocks"] = [block for block in one_topic["blocks"] if block["topic"] != "status"]
    with pytest.raises(ValidationError, match="status"):
        spec_model(["timeline", "status"], 1, 1).model_validate(one_topic)
    with pytest.raises(ValidationError, match="at least 20 blocks"):
        spec_model(["timeline", "status", "plan"], 20, 1).model_validate(fx.spec_dict())
    assert spec_model(["timeline", "status", "plan"], 9, 4).model_validate(fx.spec_dict())


def test_old_section_only_shape_is_not_a_valid_spec():
    legacy = {"title": "t", "summary": "s", "sections": [{"heading": "h", "body": "b"}]}
    with pytest.raises(ValidationError):
        IllustrationSpec.model_validate(legacy)


def test_diagram_edges_must_join_known_nodes():
    spec = fx.spec_dict()
    spec["blocks"] = [
        {
            "type": "diagram",
            "topic": "plan",
            "heading": "Bad",
            "nodes": [{"id": "a", "label": "A"}, {"id": "b", "label": "B"}],
            "edges": [{"source": "a", "target": "zzz"}],
        }
    ]
    with pytest.raises(ValidationError, match="node ids"):
        IllustrationSpec.model_validate(spec)


def test_depth_scales_with_topics_and_evidence_but_never_pads_thin_threads():
    thin = [{"line": 1, "role": "user", "text": "Make tacos?"}]
    rich = [{"line": i, "role": "user", "text": "x" * 800} for i in range(8)]
    assert depth_for(1, thin) == (3, 1)
    assert depth_for(1, rich) == (7, 2)
    assert depth_for(3, rich)[0] > depth_for(1, rich)[0]
    assert depth_for(3, rich) == (11, 4)
    assert depth_for(3, thin)[1] == 1


@pytest.mark.skipif(
    not os.environ.get("ERGONAUT_TEST_CHROMIUM"), reason="set ERGONAUT_TEST_CHROMIUM to a chrome binary"
)
@pytest.mark.parametrize("width", [360, 390, 768, 1280])
def test_page_does_not_overflow_horizontally(width, tmp_path):
    sync_api = pytest.importorskip("playwright.sync_api")
    path = tmp_path / "page.html"
    path.write_bytes(render().html)
    with sync_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path=os.environ["ERGONAUT_TEST_CHROMIUM"], args=["--no-sandbox"]
        )
        tab = browser.new_page(viewport={"width": width, "height": 800})
        tab.goto(path.as_uri())
        overflow = tab.evaluate("document.documentElement.scrollWidth - window.innerWidth")
        offenders = tab.evaluate(
            "[...document.querySelectorAll('body *')].filter(e => !e.closest('.tablewrap') && e.getBoundingClientRect().right > window.innerWidth + 1).map(e => e.className || e.tagName).slice(0, 5)"
        )
        browser.close()
    assert overflow <= 0, offenders
