"""Topics: groups of closely related entities, each with a short report.

Entities and the relationships between them form a weighted graph. Leiden
community detection splits it into groups that are linked more densely inside
than to each other, and the model writes a report on each group of at least
``MIN_TOPIC_SIZE`` entities. Reports answer broad questions, such as what the
conversations were mostly about, that no single message answers.

Entities recorded as the same thing count as one node, and entities with no
relationships are left out, since they cannot belong to a group.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

import networkx as nx

from chatlore.extraction import ExtractionError
from chatlore.llm import ChatMessage

REPORT_PROMPT_VERSION = "1"
"""Bump when the report prompt changes in a way that should rewrite every report."""

MIN_TOPIC_SIZE = 3
MAX_TOPIC_SIZE = 40
"""Larger groups are split again on their own, as hierarchical Leiden does in GraphRAG."""
MAX_MEMBERS_SHOWN = 30
MAX_LINKS_SHOWN = 30
MAX_FINDINGS = 5
SEED = 7
"""Leiden starts from a random order; a fixed seed keeps topics the same run to run."""

REPORT_SYSTEM_PROMPT = """\
You maintain a knowledge graph built from one person's conversations with AI
assistants. Below is one group of closely related entities, with their types,
summaries, and the relationships between them. Write a short report on what
this group is about in these conversations.
- title: a few words naming the topic.
- summary: two or three sentences, at most 80 words.
- findings: up to five facts worth remembering, one sentence each.
Use only what is given.

Reply with only a JSON object in this shape:
{"title": "...", "summary": "...", "findings": ["..."]}"""


def _text(value: object) -> str:
    return " ".join(value.split()) if isinstance(value, str) else ""


@dataclass(frozen=True, slots=True)
class TopicReport:
    title: str
    summary: str
    findings: tuple[str, ...]

    def to_json(self) -> str:
        return json.dumps(
            {"title": self.title, "summary": self.summary, "findings": list(self.findings)},
            ensure_ascii=False,
            separators=(",", ":"),
        )

    @classmethod
    def from_json(cls, value: str) -> TopicReport:
        data = json.loads(value)
        return cls(data["title"], data["summary"], tuple(data["findings"]))

    @property
    def text(self) -> str:
        """What full-text search reads for this topic."""
        return " ".join([self.title, self.summary, *self.findings])


@dataclass(frozen=True, slots=True)
class Member:
    name: str
    type: str
    summary: str
    mentions: int


@dataclass(frozen=True, slots=True)
class Link:
    source: str
    target: str
    description: str
    weight: int


def _leiden(graph: nx.Graph[str], nodes: set[str], max_size: int) -> list[set[str]]:
    """Split ``nodes`` into communities, splitting again any larger than ``max_size``."""
    part = graph.subgraph(nodes)
    if part.number_of_edges() == 0:
        return [nodes]
    groups = nx.community.leiden_communities(part, weight="weight", seed=SEED, metric="modularity")
    if len(groups) <= 1:
        return [nodes]
    return [
        piece
        for group in groups
        for piece in (
            [set(group)] if len(group) <= max_size else _leiden(graph, set(group), max_size)
        )
    ]


def find_topics(
    entities: Iterable[str],
    links: Iterable[tuple[str, str, int]],
    same: Iterable[tuple[str, str]] = (),
    max_size: int = MAX_TOPIC_SIZE,
) -> list[list[str]]:
    """Group entity ids into topics, largest first, each sorted by id.

    ``links`` are weighted relationships and ``same`` pairs of entities recorded
    as one thing. Only groups of at least ``MIN_TOPIC_SIZE`` entities count, and a
    group larger than ``max_size`` is split again as long as Leiden finds a split,
    so one well-connected entity cannot pull half the graph into one topic.
    """
    known = set(entities)
    graph: nx.Graph[str] = nx.Graph()
    graph.add_nodes_from(sorted(known))
    for first, second in same:
        if first in known and second in known:
            graph.add_edge(first, second)
    merged = {node: min(group) for group in nx.connected_components(graph) for node in group}

    weighted: nx.Graph[str] = nx.Graph()
    for source, target, weight in links:
        if source not in merged or target not in merged:
            continue
        first, second = merged[source], merged[target]
        if first == second:
            continue
        previous = weighted.get_edge_data(first, second, {"weight": 0})["weight"]
        weighted.add_edge(first, second, weight=previous + weight)
    if weighted.number_of_edges() == 0:
        return []

    members: dict[str, list[str]] = {}
    for node, root in merged.items():
        members.setdefault(root, []).append(node)
    groups = _leiden(weighted, set(weighted.nodes), max_size)
    topics = [sorted(node for root in group for node in members[root]) for group in groups]
    topics = [topic for topic in topics if len(topic) >= MIN_TOPIC_SIZE]
    return sorted(topics, key=lambda topic: (-len(topic), topic[0]))


def report_messages(members: Sequence[Member], links: Sequence[Link]) -> list[ChatMessage]:
    """The request for one topic's report, showing its best-connected parts first."""
    shown = sorted(members, key=lambda member: (-member.mentions, member.name))
    strongest = sorted(links, key=lambda link: (-link.weight, link.source, link.target))
    lines = ["### Entities"]
    lines += [
        f"- {member.name} ({member.type}): {member.summary}" for member in shown[:MAX_MEMBERS_SHOWN]
    ]
    if len(shown) > MAX_MEMBERS_SHOWN:
        lines.append(f"- and {len(shown) - MAX_MEMBERS_SHOWN} more")
    if strongest:
        lines += ["", "### Relationships"]
        lines += [
            f"- {link.source} -> {link.target}: {link.description}"
            for link in strongest[:MAX_LINKS_SHOWN]
        ]
    return [ChatMessage("system", REPORT_SYSTEM_PROMPT), ChatMessage("user", "\n".join(lines))]


def parse_report(answer: str) -> TopicReport:
    """Read the model's report; an answer without a title or summary is unreadable."""
    try:
        data = json.loads(answer)
    except json.JSONDecodeError as error:
        raise ExtractionError(f"the answer is not JSON: {error}") from error
    if not isinstance(data, dict):
        raise ExtractionError("the answer is not a JSON object")
    title, summary = _text(data.get("title")), _text(data.get("summary"))
    if not title or not summary:
        raise ExtractionError("the report has no title or summary")
    findings = data.get("findings")
    kept = [_text(item) for item in findings] if isinstance(findings, list) else []
    return TopicReport(title, summary, tuple(item for item in kept if item)[:MAX_FINDINGS])
