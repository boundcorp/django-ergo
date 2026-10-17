"""Deterministic HTML for illustration specs.

Every tag, class, colour and SVG coordinate is produced here; model text is
only ever inserted through ``html.escape``. The page is self-contained (no
scripts, no network), readable without colour (states carry a glyph and a
word) and reflows to a phone: grids wrap, wide diagrams fall back to stacked
stages, tables become cards.
"""

from __future__ import annotations

import textwrap
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from html import escape

from ergonaut.api.illustration_spec import (
    CardsBlock,
    ChartBlock,
    ComparisonBlock,
    DiagramBlock,
    FlowBlock,
    IllustrationSpec,
    ProseBlock,
    StatsBlock,
    StatusBoardBlock,
    TimelineBlock,
    numbers_in,
    stat_is_sourced,
)

EXCERPT_CHARS = 280

STATES = {
    "done": ("Done", "✓"),
    "in_progress": ("In progress", "◐"),
    "blocked": ("Blocked", "✕"),
    "decision": ("Needs a decision", "◆"),
    "next": ("Next", "→"),
    "open": ("Open question", "?"),
}

NODE_KINDS = {"actor": "Actor", "system": "System", "data": "Data", "decision": "Decision", "artifact": "Artifact"}

CSS = """
:root{color-scheme:light dark;--bg:#f5f7fb;--panel:#fff;--panel2:#eef2f8;--line:#d3dbe8;--ink:#14202f;--muted:#4b5b70;--accent:#0b7a6b;--accent-ink:#064c43;--hero:#dff3ee;--ok:#0b7a4b;--info:#1d5fb8;--violet:#5b3fb5;--bad:#b3261e;--warn:#8a5a00;--idle:#56657a;--shadow:0 18px 50px #1a2a4020}
@media (prefers-color-scheme:dark){:root{--bg:#0a101c;--panel:#111c2c;--panel2:#16263b;--line:#283951;--ink:#ecf3fd;--muted:#a8b8cd;--accent:#6ce5c3;--accent-ink:#9ff2da;--hero:#163851;--ok:#6ce5a0;--info:#8dbbfb;--violet:#b4a2fb;--bad:#ff9a92;--warn:#f2c474;--idle:#9fb0c6;--shadow:0 18px 50px #0007}}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:radial-gradient(circle at 88% 0,var(--hero) 0,transparent 38%),var(--bg);color:var(--ink);font:16px/1.55 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;overflow-wrap:anywhere}
main{max-width:1100px;margin:auto;padding:clamp(22px,5vw,44px) clamp(16px,4vw,28px) 80px}
a{color:var(--accent-ink)}
h1,h2,h3,h4,p,ul,ol,dl,dd,figure{margin:0}
.eyebrow{color:var(--accent-ink);font-size:.75rem;font-weight:800;letter-spacing:.16em;text-transform:uppercase}
.hero{display:grid;gap:clamp(20px,4vw,40px);margin:18px 0 34px}
@media (min-width:860px){.hero.has-take{grid-template-columns:minmax(0,1.15fr) minmax(0,.85fr);align-items:center}}
h1{font-size:clamp(2rem,6.4vw,3.7rem);line-height:1.04;letter-spacing:-.045em;margin:10px 0 16px}
.lead{font-size:clamp(1.05rem,2.6vw,1.2rem);color:var(--muted);max-width:44rem}
.chips{display:flex;flex-wrap:wrap;gap:8px;margin-top:18px;padding:0;list-style:none}
.chips li{border:1px solid var(--line);border-radius:99px;padding:4px 12px;font-size:.8rem;color:var(--muted);background:var(--panel)}
.take{background:var(--panel);border:1px solid var(--line);border-radius:18px;padding:20px 22px;box-shadow:var(--shadow)}
.take h2{font-size:.78rem;letter-spacing:.14em;text-transform:uppercase;color:var(--accent-ink);margin-bottom:10px}
.take ol{padding:0;margin:0;list-style:none;counter-reset:t}
.take li{counter-increment:t;display:grid;grid-template-columns:2rem 1fr;gap:6px;padding:9px 0;border-top:1px solid var(--line)}
.take li:first-child{border-top:0}
.take li::before{content:counter(t);font-weight:800;color:var(--accent-ink);font-size:1.05rem}
.toc{display:flex;flex-wrap:wrap;gap:8px;margin:0 0 8px;padding:0;list-style:none}
.toc a{display:inline-block;border:1px solid var(--line);border-radius:10px;padding:7px 13px;text-decoration:none;font-weight:650;font-size:.9rem;background:var(--panel)}
.band{margin:48px 0 18px}
.band h2{font-size:clamp(1.5rem,4.4vw,2.1rem);letter-spacing:-.03em;line-height:1.12;margin:6px 0 8px}
.band p{color:var(--muted);max-width:44rem}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:18px;padding:clamp(16px,3.6vw,26px);margin:14px 0}
.panel>h2,.panel>h3{font-size:clamp(1.2rem,3.4vw,1.5rem);letter-spacing:-.025em;line-height:1.2}
.panel>.intro{color:var(--muted);margin-top:6px;max-width:44rem}
.panel>.body{margin-top:16px}
.panel p+p{margin-top:.8em}
.cites{display:flex;flex-wrap:wrap;gap:4px 6px;margin-top:10px;font-size:.78rem;color:var(--muted);align-items:center}
.cites a{border:1px solid var(--line);border-radius:6px;padding:0 6px;text-decoration:none;color:var(--muted)}
.pill{display:inline-flex;align-items:center;gap:5px;border:1px solid currentColor;border-radius:99px;padding:1px 9px;font-size:.74rem;font-weight:750;white-space:nowrap;vertical-align:middle}
.s-done{color:var(--ok)}.s-in_progress{color:var(--info)}.s-next{color:var(--violet)}.s-blocked{color:var(--bad)}.s-decision{color:var(--warn)}.s-open{color:var(--idle)}
.ink{color:var(--ink)}
h4{font-size:1.02rem;line-height:1.3}
.sub{color:var(--muted);font-size:.93rem;margin-top:4px}
.timeline{list-style:none;padding:0 0 0 22px;margin:0;border-left:3px solid var(--line)}
.tl{position:relative;display:grid;gap:2px;padding:0 0 22px 18px}
.tl:last-child{padding-bottom:0}
.tl::before{content:"";position:absolute;left:-33px;top:4px;width:15px;height:15px;border-radius:50%;background:var(--panel);border:4px solid currentColor}
.tl-when{font-size:.74rem;font-weight:800;letter-spacing:.06em;text-transform:uppercase;color:var(--muted)}
.tl h4{display:flex;flex-wrap:wrap;gap:4px 10px;align-items:center;color:var(--ink)}
.seg{display:flex;height:12px;border-radius:99px;overflow:hidden;gap:2px;margin:2px 0 10px}
.seg span{background:currentColor;min-width:6px}
.legend{display:flex;flex-wrap:wrap;gap:6px 14px;list-style:none;padding:0;margin:0 0 18px;font-size:.86rem}
.board{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,230px),1fr));gap:12px}
.col{border:1px solid var(--line);border-top:4px solid currentColor;border-radius:12px;padding:12px 14px;background:var(--panel2)}
.col h4{margin-bottom:8px}
.col ul{list-style:none;padding:0;margin:0;display:grid;gap:10px}
.col li{color:var(--ink)}
.col li b{display:block}
.col li p{color:var(--muted);font-size:.92rem}
.flow{list-style:none;padding:0;margin:0;display:grid;gap:12px;grid-template-columns:repeat(auto-fit,minmax(min(100%,190px),1fr))}
.step{position:relative;background:var(--panel2);border:1px solid var(--line);border-top:4px solid var(--info);border-radius:12px;padding:14px;color:var(--ink)}
.step .n{display:inline-grid;place-items:center;width:1.7rem;height:1.7rem;border-radius:50%;background:var(--info);color:var(--bg);font-weight:800;font-size:.85rem;margin-bottom:8px}
.step p{color:var(--muted);font-size:.92rem;margin-top:4px}
.step:not(:last-child)::after{content:"↓";position:absolute;left:50%;bottom:-17px;transform:translateX(-50%);color:var(--info);font-weight:800;background:var(--panel);line-height:1;padding:0 4px;z-index:1}
@media (min-width:900px){.flow.row{grid-template-columns:repeat(var(--cols),minmax(0,1fr))}.flow.row .step:not(:last-child)::after{content:"→";left:auto;right:-14px;bottom:auto;top:50%;transform:translateY(-50%)}}
.dia-svg{display:none}
.dia-stages{display:grid;gap:12px}
.stage{border:1px solid var(--line);border-radius:12px;padding:12px 14px;background:var(--panel2)}
.stage>h4{font-size:.75rem;letter-spacing:.12em;text-transform:uppercase;color:var(--muted);margin-bottom:6px}
.stage ul{list-style:none;padding:0;margin:0;display:grid;gap:10px}
.stage small{color:var(--muted)}
.stage p{font-size:.92rem;color:var(--muted)}
.dia-text{margin-top:12px;font-size:.92rem}
.dia-text summary{cursor:pointer;font-weight:650}
.dia-text ul{margin:8px 0 0;padding-left:1.2rem}
@media (min-width:760px){.dia-svg{display:block}.dia-stages{display:none}}
@media (max-width:759px){.dia-text{display:none}}
.dia-svg svg{display:block;width:100%;height:auto;margin:auto}
.dia-svg .el.v{text-anchor:start}
.dia-svg .node rect{fill:var(--panel2);stroke:var(--info);stroke-width:2}
.dia-svg .node.k-actor rect{stroke:var(--accent-ink);rx:40}
.dia-svg .node.k-data rect{stroke:var(--violet);stroke-dasharray:6 4}
.dia-svg .node.k-decision rect{stroke:var(--warn);stroke-width:3}
.dia-svg .node.k-artifact rect{stroke:var(--ok)}
.dia-svg text{fill:var(--ink);font:600 14px system-ui,sans-serif;text-anchor:middle}
.dia-svg .nk{fill:var(--muted);font:700 10px system-ui,sans-serif;letter-spacing:.1em}
.dia-svg .edge{fill:none;stroke:var(--info);stroke-width:2.2}
.dia-svg .edge.back,.dia-svg .edge.skip{stroke-dasharray:5 4}
.dia-svg .el{font:600 12px system-ui,sans-serif;fill:var(--muted);paint-order:stroke;stroke:var(--panel);stroke-width:5px;stroke-linejoin:round}
.dia-svg marker path{fill:var(--info)}
.tablewrap{overflow-x:auto;-webkit-overflow-scrolling:touch}
table{border-collapse:collapse;width:100%;font-size:.95rem}
caption{text-align:left;color:var(--muted);padding-bottom:8px;font-size:.88rem}
th,td{padding:10px 12px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}
thead th{font-size:.78rem;letter-spacing:.08em;text-transform:uppercase;color:var(--accent-ink);border-bottom:2px solid var(--line)}
tbody th{font-weight:700;min-width:8rem}
@media (max-width:600px){table.cmp,table.cmp tbody,table.cmp tr,table.cmp th,table.cmp td{display:block}table.cmp thead{position:absolute;left:-9999px}table.cmp tr{border:1px solid var(--line);border-radius:12px;margin-bottom:12px;padding:6px 12px;background:var(--panel2)}table.cmp th,table.cmp td{border:0;padding:6px 0}table.cmp td::before{content:attr(data-label);display:block;font-size:.72rem;font-weight:800;letter-spacing:.08em;text-transform:uppercase;color:var(--accent-ink)}}
.cards{display:grid;gap:12px;grid-template-columns:repeat(auto-fit,minmax(min(100%,240px),1fr))}
.card{background:var(--panel2);border:1px solid var(--line);border-radius:14px;padding:16px}
.card p{color:var(--muted);font-size:.94rem;margin-top:6px}
.card .pill{margin-bottom:8px}
.stats{display:grid;gap:12px;grid-template-columns:repeat(auto-fit,minmax(min(100%,200px),1fr))}
.stat{background:var(--panel2);border:1px solid var(--line);border-radius:14px;padding:16px;display:flex;flex-direction:column}
.stat dt{color:var(--muted);font-size:.85rem;order:2;margin-top:2px}
.stat .v{font-size:clamp(1.8rem,6vw,2.6rem);font-weight:850;letter-spacing:-.04em;line-height:1.1;color:var(--accent-ink);order:1}
.stat .note{font-size:.85rem;color:var(--muted);order:3;margin-top:6px}
.hbar{list-style:none;padding:0;margin:0;display:grid;gap:12px}
.hbar li{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:4px 12px;align-items:baseline}
.hbar .track{grid-column:1/-1;height:14px;background:var(--panel2);border-radius:7px;overflow:hidden}
.hbar .fill{display:block;height:100%;background:linear-gradient(90deg,var(--accent),var(--info));border-radius:7px}
.hbar .val{font-weight:800;font-variant-numeric:tabular-nums}
.linechart svg{display:block;width:100%;height:auto;margin-bottom:12px}
.linechart .grid{stroke:var(--line);stroke-width:1}
.linechart .ln{fill:none;stroke:var(--accent);stroke-width:3;stroke-linejoin:round}
.linechart circle{fill:var(--panel);stroke:var(--accent);stroke-width:3}
.linechart text{fill:var(--muted);font:600 13px system-ui,sans-serif}
.missing{border:2px dashed var(--warn);border-radius:14px;padding:16px 18px;background:repeating-linear-gradient(135deg,transparent 0 10px,#8a5a0012 10px 20px)}
.missing h4{color:var(--warn);font-size:.8rem;letter-spacing:.1em;text-transform:uppercase;margin-bottom:6px}
.caveats{margin:40px 0 0;border-left:4px solid var(--warn);background:var(--panel);border-radius:0 14px 14px 0;padding:16px 20px}
.caveats h2{font-size:.8rem;letter-spacing:.12em;text-transform:uppercase;color:var(--warn)}
.caveats ul{margin:8px 0 0;padding-left:1.2rem}
.sources{margin-top:40px}
.sources h2{font-size:1.2rem;margin-bottom:4px}
.sources ol{list-style:none;padding:0;margin:14px 0 0;display:grid;gap:10px}
.sources li{border:1px solid var(--line);border-radius:12px;padding:10px 14px;background:var(--panel);scroll-margin-top:16px}
.sources li:target{outline:3px solid var(--accent);}
.sources .who{font-size:.76rem;font-weight:800;letter-spacing:.06em;text-transform:uppercase;color:var(--muted)}
.sources q{display:block;margin-top:2px;font-size:.93rem;quotes:none}
footer{margin-top:44px;padding-top:18px;border-top:1px solid var(--line);color:var(--muted);font-size:.84rem}
@media print{body{background:#fff}.panel,.take{box-shadow:none}}
"""


@dataclass
class Rendered:
    html: bytes
    cited: list[int] = field(default_factory=list)
    omitted: list[str] = field(default_factory=list)
    blocks: int = 0


def _e(value) -> str:
    return escape(str(value), quote=True)


def _paragraphs(text: str) -> str:
    return "".join(f"<p>{_e(part.strip())}</p>" for part in text.split("\n\n") if part.strip())


def _fmt(value: float) -> str:
    return f"{value:,.0f}" if float(value).is_integer() else f"{value:,.2f}".rstrip("0").rstrip(".")


class _Page:
    """State shared by the component renderers: what may be cited, what was."""

    def __init__(self, evidence: list[dict]):
        self.messages = {message["line"]: message for message in evidence}
        self.cited: set[int] = set()
        self.omitted: list[str] = []
        self.serial = 0

    def refs(self, refs) -> list[int]:
        seen: list[int] = []
        for ref in refs or []:
            if ref in self.messages and ref not in seen:
                seen.append(ref)
        return seen

    def cites(self, refs) -> str:
        valid = self.refs(refs)
        self.cited.update(valid)
        if not valid:
            return ""
        links = "".join(f'<a href="#src-{n}">#{n}</a>' for n in valid)
        return f'<span class="cites"><span>Sources</span>{links}</span>'

    def sourced(self, value: str, line: int) -> bool:
        message = self.messages.get(line)
        return message is not None and stat_is_sourced(value, message["text"])

    def next_id(self) -> int:
        self.serial += 1
        return self.serial


def _pill(state: str) -> str:
    label, glyph = STATES[state]
    return f'<span class="pill s-{state}"><span aria-hidden="true">{glyph}</span> {label}</span>'


def _timeline(page: _Page, block: TimelineBlock, lvl: int) -> str:
    items = "".join(
        f'<li class="tl s-{item.state}">'
        + (f'<span class="tl-when">{_e(item.when)}</span>' if item.when else "")
        + f"<h{lvl}>{_e(item.title)} {_pill(item.state)}</h{lvl}>"
        + (f'<p class="sub">{_e(item.detail)}</p>' if item.detail else "")
        + page.cites(item.evidence_refs)
        + "</li>"
        for item in block.items
    )
    return f'<ol class="timeline">{items}</ol>'


def _status_board(page: _Page, block: StatusBoardBlock, lvl: int) -> str:
    counts = Counter(item.state for item in block.items)
    order = [state for state in STATES if counts[state]]
    summary = ", ".join(f"{counts[state]} {STATES[state][0].lower()}" for state in order)
    segments = "".join(f'<span class="s-{state}" style="flex-grow:{counts[state]}"></span>' for state in order)
    legend = "".join(f"<li>{_pill(state)} <b>{counts[state]}</b></li>" for state in order)
    columns = ""
    for state in order:
        entries = "".join(
            f"<li><b>{_e(item.title)}</b>"
            + (f"<p>{_e(item.detail)}</p>" if item.detail else "")
            + page.cites(item.evidence_refs)
            + "</li>"
            for item in block.items
            if item.state == state
        )
        columns += (
            f'<section class="col s-{state}"><h{lvl} class="ink">{_pill(state)}</h{lvl}><ul>{entries}</ul></section>'
        )
    return (
        f'<div class="seg" role="img" aria-label="Items listed here: {_e(summary)}">{segments}</div>'
        f'<ul class="legend" aria-hidden="true">{legend}</ul><div class="board">{columns}</div>'
    )


def _flow(page: _Page, block: FlowBlock, lvl: int) -> str:
    steps = "".join(
        '<li class="step">'
        + f'<span class="n" aria-hidden="true">{index}</span>'
        + f"<h{lvl}>{_e(step.title)}</h{lvl}>"
        + (f"<p>{_e(step.detail)}</p>" if step.detail else "")
        + (f"<p>{_pill(step.state)}</p>" if step.state else "")
        + page.cites(step.evidence_refs)
        + "</li>"
        for index, step in enumerate(block.steps, 1)
    )
    row = " row" if len(block.steps) <= 4 else ""
    return f'<ol class="flow{row}" style="--cols:{min(len(block.steps), 4)}">{steps}</ol>'


_NODE_W, _NODE_H, _PAD = 148, 84, 16
_MAX_ACROSS = 5  # more layers than this and the diagram runs top to bottom, so text stays legible
_MARGIN = 36  # room for edges that skip a layer (leading side) or loop back (trailing side)


def _ranks(block: DiagramBlock) -> tuple[dict[str, int], set[tuple[str, str]]]:
    """Longest-path layers; edges that would close a cycle are drawn as back edges."""
    ids = [node.id for node in block.nodes]
    out: dict[str, list[str]] = {node: [] for node in ids}
    for edge in block.edges:
        out[edge.source].append(edge.target)
    state: dict[str, int] = {}
    back: set[tuple[str, str]] = set()

    def visit(node: str) -> None:
        state[node] = 1
        for target in out[node]:
            if state.get(target) == 1:
                back.add((node, target))
            elif target not in state:
                visit(target)
        state[node] = 2

    for node in ids:
        if node not in state:
            visit(node)
    rank = dict.fromkeys(ids, 0)
    forward = [(e.source, e.target) for e in block.edges if (e.source, e.target) not in back and e.source != e.target]
    for _ in ids:
        for source, target in forward:
            rank[target] = max(rank[target], rank[source] + 1)
    return rank, back


def _wrap(text: str, width: int, lines: int) -> list[str]:
    wrapped = textwrap.wrap(text, width, break_long_words=True) or [""]
    if len(wrapped) > lines:
        wrapped = [*wrapped[: lines - 1], textwrap.shorten(" ".join(wrapped[lines - 1 :]), width, placeholder="…")]
    return wrapped


def _diagram(page: _Page, block: DiagramBlock, lvl: int) -> str:
    rank, back = _ranks(block)
    columns: dict[int, list] = {}
    for node in block.nodes:
        columns.setdefault(rank[node.id], []).append(node)
    layers = max(columns) + 1
    widest = max(len(nodes) for nodes in columns.values())
    down = layers > _MAX_ACROSS
    # "layer" runs along the flow, "lane" across it. Edges that skip a layer arc round the
    # leading side and loops round the trailing side, so no edge is drawn through a node.
    loops = [e for e in block.edges if (e.source, e.target) in back or e.source == e.target]
    skips = [e for e in block.edges if e not in loops and rank[e.target] - rank[e.source] > 1]
    lead, trail = (_MARGIN if skips else 0), (_MARGIN if loops else 0)
    layer_step = (_NODE_H + 64) if down else (_NODE_W + 100)
    lane_step = (_NODE_W + 36) if down else (_NODE_H + 24)
    lane_size = _NODE_W if down else _NODE_H
    layer_size = _NODE_H if down else _NODE_W
    lanes_extent = widest * lane_step - (lane_step - lane_size)
    layers_extent = layers * layer_step - (layer_step - layer_size)
    across_total = _PAD * 2 + lead + lanes_extent + trail
    along_total = _PAD * 2 + layers_extent
    width, height = (across_total, along_total) if down else (along_total, across_total)
    pos: dict[str, tuple[float, float]] = {}
    for layer, nodes in columns.items():
        extent = len(nodes) * lane_step - (lane_step - lane_size)
        for index, node in enumerate(nodes):
            along = _PAD + layer * layer_step
            across = _PAD + lead + (lanes_extent - extent) / 2 + index * lane_step
            pos[node.id] = (across, along) if down else (along, across)
    far_trail = _PAD + lead + lanes_extent + trail - 10  # where loops run
    far_lead = 8  # where skips run
    marker = f"arrow-{page.next_id()}"
    paths = ""
    for edge in block.edges:
        sx, sy = pos[edge.source]
        tx, ty = pos[edge.target]
        label = None  # (x, y, class); left off where it would sit on a node
        if edge in loops or edge in skips:
            lane = far_trail if edge in loops else far_lead
            if down:
                y1, y2 = sy + _NODE_H / 2, ty + _NODE_H / 2
                x1, x2 = (sx + _NODE_W, tx + _NODE_W + 2) if edge in loops else (sx, tx - 2)
                d = f"M{x1:.1f} {y1:.1f}C{lane:.1f} {y1:.1f} {lane:.1f} {y2:.1f} {x2:.1f} {y2:.1f}"
            else:
                x1, x2 = sx + _NODE_W / 2, tx + _NODE_W / 2
                y1, y2 = (sy + _NODE_H, ty + _NODE_H + 2) if edge in loops else (sy, ty - 2)
                d = f"M{x1:.1f} {y1:.1f}C{x1:.1f} {lane:.1f} {x2:.1f} {lane:.1f} {x2:.1f} {y2:.1f}"
                peak = (y1 + y2) / 8 + 0.75 * lane
                label = ((x1 + x2) / 2, peak + (12 if edge in loops else -5), "el")
            cls = "edge back" if edge in loops else "edge skip"
        elif down:
            x1, y1, x2, y2 = sx + _NODE_W / 2, sy + _NODE_H, tx + _NODE_W / 2, ty - 2
            mid = (y1 + y2) / 2
            d = f"M{x1:.1f} {y1:.1f}C{x1:.1f} {mid:.1f} {x2:.1f} {mid:.1f} {x2:.1f} {y2:.1f}"
            cls, label = "edge", ((x1 + x2) / 2 + 8, mid + 4, "el v")
        else:
            x1, y1, x2, y2 = sx + _NODE_W, sy + _NODE_H / 2, tx - 2, ty + _NODE_H / 2
            mid = (x1 + x2) / 2
            d = f"M{x1:.1f} {y1:.1f}C{mid:.1f} {y1:.1f} {mid:.1f} {y2:.1f} {x2:.1f} {y2:.1f}"
            cls, label = "edge", (mid, (y1 + y2) / 2 - 7, "el")
        paths += f'<path class="{cls}" d="{d}" marker-end="url(#{marker})"/>'
        if edge.label and label:
            short = textwrap.shorten(edge.label, 14, placeholder="…")
            paths += f'<text class="{label[2]}" x="{label[0]:.1f}" y="{label[1]:.1f}">{_e(short)}</text>'
    nodes_svg = ""
    for node in block.nodes:
        x, y = pos[node.id]
        lines = _wrap(node.label, 17, 3)
        first = y + 52 - (len(lines) - 1) * 9
        text = "".join(
            f'<text x="{x + _NODE_W / 2:.1f}" y="{first + i * 18:.1f}">{_e(line)}</text>'
            for i, line in enumerate(lines)
        )
        nodes_svg += (
            f'<g class="node k-{node.kind}"><title>{_e(node.label)}{": " + _e(node.detail) if node.detail else ""}</title>'
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{_NODE_W}" height="{_NODE_H}" rx="14"/>'
            f'<text class="nk" x="{x + _NODE_W / 2:.1f}" y="{y + 17:.1f}">{NODE_KINDS[node.kind].upper()}</text>{text}</g>'
        )
    names = {node.id: node.label for node in block.nodes}
    relations = [f"{names[e.source]} → {names[e.target]}" + (f" ({e.label})" if e.label else "") for e in block.edges]
    svg = (
        f'<svg viewBox="0 0 {width} {height:.0f}" style="max-width:{width}px" role="img" '
        f'aria-label="{_e(block.heading)}: {_e("; ".join(relations))}">'
        f'<defs><marker id="{marker}" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="8" markerHeight="8" '
        f'orient="auto-start-reverse"><path d="M0 0L10 5L0 10z"/></marker></defs>{paths}{nodes_svg}</svg>'
    )
    outgoing: dict[str, list[str]] = {node.id: [] for node in block.nodes}
    for edge in block.edges:
        arrow = "↩" if edge in loops else "→"
        outgoing[edge.source].append(f"{arrow} {names[edge.target]}" + (f" — {edge.label}" if edge.label else ""))
    stages = ""
    for layer in sorted(columns):
        entries = ""
        for node in columns[layer]:
            entries += (
                f"<li><b>{_e(node.label)}</b> <small>({NODE_KINDS[node.kind].lower()})</small>"
                + (f"<p>{_e(node.detail)}</p>" if node.detail else "")
                + "".join(f"<p>{_e(line)}</p>" for line in outgoing[node.id])
                + "</li>"
            )
        stages += f'<section class="stage"><h{lvl}>Stage {layer + 1}</h{lvl}><ul>{entries}</ul></section>'
    text_list = "".join(f"<li>{_e(line)}</li>" for line in relations)
    return (
        f'<div class="dia-svg">{svg}</div><div class="dia-stages">{stages}</div>'
        f'<details class="dia-text"><summary>Connections as text</summary><ul>{text_list}</ul></details>'
    )


def _comparison(page: _Page, block: ComparisonBlock, lvl: int) -> str:
    head = "".join(f'<th scope="col">{_e(column)}</th>' for column in block.columns)
    rows = ""
    for row in block.rows:
        cells = "".join(
            f'<td data-label="{_e(column)}">{_e(cell)}</td>'
            for column, cell in zip(block.columns, row.cells, strict=True)
        )
        rows += f'<tr><th scope="row">{_e(row.label)}{page.cites(row.evidence_refs)}</th>{cells}</tr>'
    return (
        '<div class="tablewrap" tabindex="0" role="region" aria-label="'
        + _e(block.heading)
        + f'"><table class="cmp"><thead><tr><th scope="col">Criterion</th>{head}</tr></thead><tbody>{rows}</tbody></table></div>'
    )


def _cards(page: _Page, block: CardsBlock, lvl: int) -> str:
    cards = "".join(
        '<article class="card">'
        + (
            _pill(card.state)
            if card.state
            else (f'<span class="pill s-open">{_e(card.tag)}</span>' if card.tag else "")
        )
        + f"<h{lvl}>{_e(card.title)}</h{lvl}><p>{_e(card.body)}</p></article>"
        for card in block.cards
    )
    return f'<div class="cards">{cards}</div>'


def _missing(page: _Page, heading: str, what: str) -> str:
    page.omitted.append(f"{what} “{heading}” was left out: its figures are not stated in the cited messages.")
    return (
        '<aside class="missing"><h4>No figures to show</h4>'
        f"<p>The cited messages don’t contain the numbers this {_e(what)} needs, so none are drawn.</p></aside>"
    )


def _stats(page: _Page, block: StatsBlock, lvl: int) -> str:
    good = [stat for stat in block.stats if page.sourced(stat.value, stat.evidence_ref)]
    if not good:
        return _missing(page, block.heading, "figures panel")
    tiles = "".join(
        f'<div class="stat"><dt>{_e(stat.label)}</dt><dd class="v">{_e(stat.value)}</dd>'
        + (f'<dd class="note">{_e(stat.note)}</dd>' if stat.note else "")
        + f'<dd class="note">{page.cites([stat.evidence_ref])}</dd></div>'
        for stat in good
    )
    if len(good) < len(block.stats):
        page.omitted.append(
            f"{len(block.stats) - len(good)} figure(s) in “{block.heading}” were left out: not stated in the cited messages."
        )
    return f'<dl class="stats">{tiles}</dl>'


def _chart(page: _Page, block: ChartBlock, lvl: int) -> str:
    points = []
    for point in block.points:
        message = page.messages.get(point.evidence_ref)
        if message and point.value in numbers_in(message["text"]):
            points.append(point)
    if len(points) < 2:
        return _missing(page, block.heading, "chart")
    if len(points) < len(block.points):
        page.omitted.append(
            f"{len(block.points) - len(points)} point(s) in “{block.heading}” were left out: not stated in the cited messages."
        )
    unit = f" {_e(block.unit)}" if block.unit else ""
    kind = block.chart if all(p.value >= 0 for p in points) else "line"
    if kind == "bar":
        top = max(p.value for p in points) or 1
        rows = "".join(
            f'<li><span>{_e(p.label)}</span><span class="val">{_fmt(p.value)}{unit}</span>'
            f'<span class="track" aria-hidden="true"><span class="fill" style="width:{max(p.value / top * 100, 1):.1f}%"></span></span>'
            f"{page.cites([p.evidence_ref])}</li>"
            for p in points
        )
        return f'<ul class="hbar">{rows}</ul>'
    low, high = min(0, min(p.value for p in points)), max(p.value for p in points)
    span = (high - low) or 1
    w, h, left, right, top, bottom = 560, 260, 54, 20, 18, 44
    xs = [left + (w - left - right) * i / (len(points) - 1) for i in range(len(points))]
    ys = [top + (h - top - bottom) * (1 - (p.value - low) / span) for p in points]
    grid = "".join(
        f'<line class="grid" x1="{left}" x2="{w - right}" y1="{y:.1f}" y2="{y:.1f}"/>'
        f'<text x="{left - 8}" y="{y + 4:.1f}" text-anchor="end">{_fmt(low + span * frac)}</text>'
        for frac in (0, 0.5, 1)
        for y in [top + (h - top - bottom) * (1 - frac)]
    )
    stride = max(1, len(points) // 6)
    labels = "".join(
        f'<text x="{x:.1f}" y="{h - 16}" text-anchor="middle">{_e(textwrap.shorten(p.label, 12, placeholder="…"))}</text>'
        for i, (x, p) in enumerate(zip(xs, points, strict=True))
        if i % stride == 0
    )
    line = " ".join(f"{x:.1f},{y:.1f}" for x, y in zip(xs, ys, strict=True))
    dots = "".join(
        f'<circle cx="{x:.1f}" cy="{y:.1f}" r="5"><title>{_e(p.label)}: {_fmt(p.value)}{unit}</title></circle>'
        for x, y, p in zip(xs, ys, points, strict=True)
    )
    rows = "".join(
        f'<tr><th scope="row">{_e(p.label)}</th><td>{_fmt(p.value)}{unit}</td><td>{page.cites([p.evidence_ref])}</td></tr>'
        for p in points
    )
    return (
        f'<div class="linechart"><svg viewBox="0 0 {w} {h}" role="img" aria-label="{_e(block.heading)}: line chart, values listed in the table below">'
        f'{grid}<polyline class="ln" points="{line}"/>{dots}{labels}</svg>'
        f'<div class="tablewrap"><table><caption>Values</caption><thead><tr><th scope="col">Point</th><th scope="col">Value</th><th scope="col">Source</th></tr></thead><tbody>{rows}</tbody></table></div></div>'
    )


_RENDERERS = {
    "timeline": _timeline,
    "status_board": _status_board,
    "flow": _flow,
    "diagram": _diagram,
    "comparison": _comparison,
    "cards": _cards,
    "stats": _stats,
    "chart": _chart,
}


def _block(page: _Page, block, lvl: int) -> str:
    head = f"<h{lvl}>{_e(block.heading)}</h{lvl}>" + (f'<p class="intro">{_e(block.intro)}</p>' if block.intro else "")
    if isinstance(block, ProseBlock):
        body = _paragraphs(block.body)
    else:
        body = _RENDERERS[block.type](page, block, lvl + 1)
    return f'<section class="panel">{head}<div class="body">{body}</div>{page.cites(block.evidence_refs)}</section>'


def render_illustration(
    spec: IllustrationSpec,
    *,
    topics: list[dict],
    evidence: list[dict],
    as_of_line: int,
    generated_at: datetime,
    model: str = "",
) -> Rendered:
    page = _Page(evidence)
    topic_ids = [topic["id"] for topic in topics] or ["custom"]
    titles = {topic["id"]: topic.get("title") or topic["id"].title() for topic in topics}
    titles.setdefault("custom", "Your request")
    purposes = {topic["id"]: topic.get("purpose", "") for topic in topics}
    grouped: dict[str, list] = {topic: [] for topic in topic_ids}
    for block in spec.blocks:
        grouped[block.topic if block.topic in grouped else topic_ids[0]].append(block)
    bands = [topic for topic in topic_ids if grouped[topic]]
    banded = len(bands) > 1
    lvl = 3 if banded else 2

    body = ""
    for number, topic in enumerate(bands, 1):
        if banded:
            body += (
                f'<div class="band" id="topic-{_e(topic)}"><div class="eyebrow">{number:02d} / {_e(titles[topic])}</div>'
                f"<h2>{_e(titles[topic])}</h2>"
                + (f"<p>{_e(purposes[topic])}</p>" if purposes.get(topic) else "")
                + "</div>"
            )
        body += "".join(_block(page, block, lvl) for block in grouped[topic])

    takeaways = [t for t in spec.takeaways if t.strip()]
    take = (
        '<aside class="take"><h2>The short version</h2><ol>'
        + "".join(f"<li><span>{_e(t)}</span></li>" for t in takeaways)
        + "</ol></aside>"
        if takeaways
        else ""
    )
    lines = [m["line"] for m in evidence]
    chips = [f"Snapshot as of message #{as_of_line}"]
    if lines:
        chips.append(f"{len(lines)} messages read (#{min(lines)}–#{max(lines)})")
    toc = (
        '<nav aria-label="Topics"><ul class="toc">'
        + "".join(f'<li><a href="#topic-{_e(topic)}">{_e(titles[topic])}</a></li>' for topic in bands)
        + "</ul></nav>"
        if banded
        else ""
    )
    caveats = [*spec.caveats, *page.omitted]
    caveat_html = (
        '<section class="caveats"><h2>Limits of this illustration</h2><ul>'
        + "".join(f"<li>{_e(c)}</li>" for c in caveats)
        + "</ul></section>"
        if caveats
        else ""
    )
    cited = sorted(page.cited)
    sources = ""
    if cited:
        items = ""
        for line in cited:
            message = page.messages[line]
            text = " ".join(message["text"].split())
            if len(text) > EXCERPT_CHARS:
                text = text[: EXCERPT_CHARS - 1].rstrip() + "…"
            items += (
                f'<li id="src-{line}"><span class="who">#{line} · {_e(message["role"])}</span><q>{_e(text)}</q></li>'
            )
        sources = (
            '<section class="sources" aria-labelledby="sources-h"><h2 id="sources-h">Sources</h2>'
            f'<p class="sub">Quoted verbatim from the cited chat messages.</p><ol>{items}</ol></section>'
        )
    made = f"{generated_at:%Y-%m-%d %H:%M} UTC"
    by = f" by {_e(model)}" if model else ""
    document = (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<meta name='color-scheme' content='light dark'>"
        f"<title>{_e(spec.title)}</title><style>{CSS}</style></head><body><main>"
        f'<header class="hero{" has-take" if take else ""}"><div>'
        f'<div class="eyebrow">{_e(spec.kicker or "Illustration")}</div><h1>{_e(spec.title)}</h1>'
        f'<p class="lead">{_e(spec.summary)}</p>'
        f'<ul class="chips">{"".join(f"<li>{_e(chip)}</li>" for chip in chips)}</ul></div>{take}</header>'
        f"{toc}{body}{caveat_html}{sources}"
        f"<footer>Static snapshot generated {made}{by} from this chat only. It shows what those messages say, "
        "not live state; figures appear only when a cited message states them.</footer>"
        "</main></body></html>"
    )
    return Rendered(document.encode("utf-8"), cited, page.omitted, len(spec.blocks))
