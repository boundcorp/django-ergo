"""The structured content a model may return for an illustration.

The model never writes markup. It picks typed visual components (timeline,
status board, flow, diagram, comparison, cards, stats, chart, prose) and fills
them with short strings that cite message lines of the chat snapshot. The
renderer in ``illustration_render`` owns every tag, class, colour and SVG
coordinate. Figures are only drawn after they are checked against the cited
message text (see ``verified_figures``).
"""

from __future__ import annotations

import re
from typing import Annotated, Literal

from pydantic import BaseModel, Field, model_validator

State = Literal["done", "in_progress", "next", "blocked", "decision", "open"]
NodeKind = Literal["actor", "system", "data", "decision", "artifact"]


class BlockBase(BaseModel):
    topic: str = Field(
        default="",
        max_length=24,
        description="The id of the selected topic this block serves ('custom' for the free-form request).",
    )
    heading: str = Field(max_length=100)
    intro: str = Field(default="", max_length=300, description="One sentence saying what to look at.")
    evidence_refs: list[int] = Field(default_factory=list, max_length=8)


class ProseBlock(BlockBase):
    type: Literal["prose"]
    body: str = Field(max_length=900)


class TimelineItem(BaseModel):
    when: str = Field(default="", max_length=40, description="Date, time or stage label; only if the thread says so.")
    title: str = Field(max_length=90)
    detail: str = Field(default="", max_length=300)
    state: State = "done"
    evidence_refs: list[int] = Field(default_factory=list, max_length=4)


class TimelineBlock(BlockBase):
    type: Literal["timeline"]
    items: list[TimelineItem] = Field(min_length=2, max_length=10)


class StatusItem(BaseModel):
    title: str = Field(max_length=90)
    detail: str = Field(default="", max_length=300)
    state: State
    evidence_refs: list[int] = Field(default_factory=list, max_length=4)


class StatusBoardBlock(BlockBase):
    type: Literal["status_board"]
    items: list[StatusItem] = Field(min_length=2, max_length=14)


class FlowStep(BaseModel):
    title: str = Field(max_length=70)
    detail: str = Field(default="", max_length=260)
    state: State | None = None
    evidence_refs: list[int] = Field(default_factory=list, max_length=4)


class FlowBlock(BlockBase):
    type: Literal["flow"]
    steps: list[FlowStep] = Field(min_length=2, max_length=8)


class DiagramNode(BaseModel):
    id: str = Field(max_length=24, pattern=r"^[A-Za-z0-9_-]+$")
    label: str = Field(max_length=40)
    detail: str = Field(default="", max_length=140)
    kind: NodeKind = "system"


class DiagramEdge(BaseModel):
    source: str = Field(max_length=24)
    target: str = Field(max_length=24)
    label: str = Field(default="", max_length=30)


class DiagramBlock(BlockBase):
    type: Literal["diagram"]
    nodes: list[DiagramNode] = Field(min_length=2, max_length=10)
    edges: list[DiagramEdge] = Field(min_length=1, max_length=16)

    @model_validator(mode="after")
    def _edges_join_nodes(self):
        ids = [node.id for node in self.nodes]
        if len(set(ids)) != len(ids):
            msg = "diagram node ids must be unique"
            raise ValueError(msg)
        for edge in self.edges:
            if edge.source not in ids or edge.target not in ids:
                msg = f"diagram edge {edge.source}->{edge.target} must use node ids"
                raise ValueError(msg)
        return self


class ComparisonRow(BaseModel):
    label: str = Field(max_length=60)
    cells: list[str] = Field(min_length=2, max_length=4)
    evidence_refs: list[int] = Field(default_factory=list, max_length=4)


class ComparisonBlock(BlockBase):
    type: Literal["comparison"]
    columns: list[str] = Field(min_length=2, max_length=4, description="Option names, one per cell column.")
    rows: list[ComparisonRow] = Field(min_length=2, max_length=8)

    @model_validator(mode="after")
    def _cells_match_columns(self):
        for row in self.rows:
            if len(row.cells) != len(self.columns):
                msg = f"comparison row {row.label!r} needs {len(self.columns)} cells"
                raise ValueError(msg)
        return self


class Card(BaseModel):
    title: str = Field(max_length=70)
    body: str = Field(max_length=300)
    tag: str = Field(default="", max_length=24)
    state: State | None = None


class CardsBlock(BlockBase):
    type: Literal["cards"]
    cards: list[Card] = Field(min_length=2, max_length=6)


class Stat(BaseModel):
    label: str = Field(max_length=60)
    value: str = Field(max_length=24, description="Copied exactly from the cited message, e.g. '42%' or '3 days'.")
    note: str = Field(default="", max_length=120)
    evidence_ref: int = Field(description="The message line that states this value.")


class StatsBlock(BlockBase):
    type: Literal["stats"]
    stats: list[Stat] = Field(min_length=1, max_length=4)


class ChartPoint(BaseModel):
    label: str = Field(max_length=30)
    value: float = Field(description="A number copied exactly from the cited message.")
    evidence_ref: int = Field(description="The message line that states this number.")


class ChartBlock(BlockBase):
    type: Literal["chart"]
    chart: Literal["bar", "line"] = "bar"
    unit: str = Field(default="", max_length=16)
    points: list[ChartPoint] = Field(min_length=2, max_length=12)


Block = Annotated[
    ProseBlock
    | TimelineBlock
    | StatusBoardBlock
    | FlowBlock
    | DiagramBlock
    | ComparisonBlock
    | CardsBlock
    | StatsBlock
    | ChartBlock,
    Field(discriminator="type"),
]

MAX_BLOCKS = 14


class IllustrationSpec(BaseModel):
    title: str = Field(max_length=100)
    kicker: str = Field(default="", max_length=60, description="Short eyebrow, e.g. 'Plan · rollout'.")
    summary: str = Field(max_length=500, description="Two sentences: the answer, then why it matters.")
    takeaways: list[str] = Field(default_factory=list, max_length=4, description="Up to four one-line key points.")
    blocks: list[Block] = Field(min_length=1, max_length=MAX_BLOCKS)
    caveats: list[str] = Field(default_factory=list, max_length=5)


def spec_model(topic_ids: list[str], blocks_min: int, visuals_min: int) -> type[IllustrationSpec]:
    """A spec that rejects thin output, so the model is told to add what's missing.

    The bar scales with the selected topics and the amount of evidence; see
    ``illustrate.depth_for``. A thread with little to say gets a low bar rather
    than invented content.
    """
    wanted = set(topic_ids)

    class DepthCheckedSpec(IllustrationSpec):
        @model_validator(mode="after")
        def _deep_enough(self):
            if len(self.blocks) < blocks_min:
                msg = f"Use at least {blocks_min} blocks; got {len(self.blocks)}."
                raise ValueError(msg)
            visuals = [block for block in self.blocks if block.type != "prose"]
            if len(visuals) < visuals_min:
                msg = f"Use at least {visuals_min} visual blocks (anything but prose); got {len(visuals)}."
                raise ValueError(msg)
            missing = wanted - {block.topic for block in self.blocks}
            if missing:
                msg = f"No block serves topic(s) {sorted(missing)}; set each block's topic."
                raise ValueError(msg)
            return self

    DepthCheckedSpec.__name__ = "IllustrationSpec"
    return DepthCheckedSpec


_NUMBER = re.compile(r"-?\d[\d,]*(?:\.\d+)?")


def numbers_in(text: str) -> set[float]:
    found = set()
    for token in _NUMBER.findall(text):
        try:
            found.add(float(token.replace(",", "")))
        except ValueError:
            continue
    return found


def stat_is_sourced(value: str, source: str) -> bool:
    """A stat is only shown when its figure appears in the message it cites."""
    wanted = numbers_in(value)
    if wanted:
        return wanted <= numbers_in(source)
    return bool(value.strip()) and value.strip().casefold() in source.casefold()
