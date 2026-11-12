#!/usr/bin/env python3
"""Render a small architecture graph as a self-contained HTML/SVG document."""

from __future__ import annotations

import argparse
import html
import json
import math
import sys
import unicodedata
from pathlib import Path


def _text(value: object, field: str, *, required: bool = True) -> str:
    if not isinstance(value, str) or (required and not value.strip()):
        raise ValueError(f"{field} must be a non-empty string")
    if len(value) > 500:
        raise ValueError(f"{field} exceeds 500 characters")
    return value.strip()


def _label_width(text: str) -> float:
    # Approximate sans-serif glyph widths in ems so wide letters and CJK wrap sooner.
    return sum(
        0 if unicodedata.combining(ch) else
        1.0 if unicodedata.east_asian_width(ch) in {"W", "F"} or ch in "MWmw@%" else
        0.3 if ch in "ilI1.,:;!| '" or ch == chr(34) else
        0.75 if ch.isupper() else 0.6
        for ch in text
    )


def _label_lines(label: str) -> list[str]:
    limit = 14.0  # approximately 210px at the diagram's 15px font size
    lines: list[str] = []
    line = ""
    for word in label.split():
        candidate = f"{line} {word}" if line else word
        if _label_width(candidate) <= limit:
            line = candidate
            continue
        if line:
            lines.append(line)
            line = ""
        while word and _label_width(word) > limit:
            end = 1
            while end < len(word) and _label_width(word[:end + 1]) <= limit:
                end += 1
            lines.append(word[:end])
            word = word[end:]
        line = word
    if line:
        lines.append(line)
    if len(lines) > 3:
        final = lines[2]
        while final and _label_width(final + "…") > limit:
            final = final[:-1]
        lines = [*lines[:2], final.rstrip() + "…"]
    return lines


def render(graph: object, theme: str, columns: int = 3) -> str:
    if not isinstance(graph, dict):
        raise ValueError("diagram must be a JSON object")
    title = _text(graph.get("title", "Architecture diagram"), "title")
    nodes = graph.get("nodes")
    edges = graph.get("edges", [])
    if not isinstance(nodes, list) or not 1 <= len(nodes) <= 60:
        raise ValueError("nodes must be a list containing 1 to 60 nodes")
    if not isinstance(edges, list) or len(edges) > 150:
        raise ValueError("edges must be a list containing at most 150 edges")

    positions = {}
    rendered_nodes = []
    accessible_nodes = []
    if not 1 <= columns <= 6:
        raise ValueError("columns must be between 1 and 6")
    cols = min(columns, len(nodes))
    width, height = cols * 300 + 80, math.ceil(len(nodes) / cols) * 170 + 80
    for index, node in enumerate(nodes):
        if not isinstance(node, dict):
            raise ValueError("each node must be an object")
        identifier = _text(node.get("id"), "node id")
        label = _text(node.get("label"), f"label for {identifier}")
        description = _text(node.get("description", ""), "description", required=False)
        if identifier in positions:
            raise ValueError(f"duplicate node id: {identifier}")
        x, y = 40 + (index % cols) * 300, 40 + (index // cols) * 170
        positions[identifier] = (x, y, label)
        label_html = html.escape(label)
        lines = _label_lines(label)
        baseline = y + 43 - 9 * (len(lines) - 1)
        spans = "".join(
            f'<tspan x="{x + 120}" dy="{0 if line_number == 0 else 18}">{html.escape(line)}</tspan>'
            for line_number, line in enumerate(lines)
        )
        rendered_nodes.append(
            f'<g><title>{label_html}</title><rect x="{x}" y="{y}" width="240" height="76" rx="8"/>'
            f'<text x="{x + 120}" y="{baseline}" text-anchor="middle">{spans}</text></g>'
        )
        details = f": {html.escape(description)}" if description else ""
        accessible_nodes.append(f"<li>{label_html}{details}</li>")

    rendered_edges = []
    rendered_edge_labels = []
    accessible_edges = []
    for edge in edges:
        if not isinstance(edge, dict):
            raise ValueError("each edge must be an object")
        source = _text(edge.get("from"), "edge from")
        target = _text(edge.get("to"), "edge to")
        label = _text(edge.get("label", ""), "edge label", required=False)
        if source not in positions or target not in positions:
            raise ValueError(f"edge references missing node: {source} -> {target}")
        x1, y1, name1 = positions[source]
        x2, y2, name2 = positions[target]
        if source == target:
            path = f"M {x1 + 165} {y1} C {x1 + 200} {y1 - 35}, {x1 + 40} {y1 - 35}, {x1 + 75} {y1}"
            lx, ly = x1 + 120, y1 - 24
        else:
            dx, dy = x2 - x1, y2 - y1
            step = min(120 / abs(dx) if dx else float("inf"), 38 / abs(dy) if dy else float("inf"))
            start_x, start_y = x1 + 120 + dx * step, y1 + 38 + dy * step
            end_x, end_y = x2 + 120 - dx * step, y2 + 38 - dy * step
            path = f"M {start_x:.1f} {start_y:.1f} L {end_x:.1f} {end_y:.1f}"
            lx, ly = (start_x + end_x) / 2, (min(y1, y2) - 12 if dy == 0 else (start_y + end_y) / 2 - 6)
        rendered_edges.append(f'<path d="{path}" marker-end="url(#arrow)"/>')
        if label:
            rendered_edge_labels.append(f'<text class="edge-label" x="{lx:.1f}" y="{ly:.1f}" text-anchor="middle">{html.escape(label)}</text>')
        suffix = f" ({html.escape(label)})" if label else ""
        accessible_edges.append(f"<li>{html.escape(name1)} to {html.escape(name2)}{suffix}</li>")

    colors = ("#ffffff", "#172033", "#eff6ff", "#254a7b") if theme == "light" else ("#111827", "#f3f4f6", "#1f2937", "#93c5fd")
    bg, fg, box, accent = colors
    safe_title = html.escape(title)
    return f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{safe_title}</title><style>
body{{font:16px system-ui,sans-serif;background:{bg};color:{fg};margin:2rem}}
svg{{display:block;width:100%;height:auto;max-width:{width}px}} rect{{fill:{box};stroke:{accent};stroke-width:2}}
path{{fill:none;stroke:{accent};stroke-width:2}} marker path{{fill:{accent};stroke:none}} text{{fill:{fg};font:15px system-ui,sans-serif}}
.edge-label{{font-size:12px;paint-order:stroke;stroke:{bg};stroke-width:5px;stroke-linejoin:round}}
details{{max-width:60rem;margin-top:1rem}} li{{margin:.4rem 0}}
</style></head><body><h1>{safe_title}</h1>
<svg role="img" aria-labelledby="diagram-title diagram-desc" viewBox="0 0 {width} {height}">
<title id="diagram-title">{safe_title}</title><desc id="diagram-desc">Architecture components and their connections. A text list follows the diagram.</desc>
<defs><marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z"/></marker></defs>
{''.join(rendered_edges)}{''.join(rendered_nodes)}{''.join(rendered_edge_labels)}</svg>
<details><summary>Components and connections</summary><h2>Components</h2><ul>{''.join(accessible_nodes)}</ul>
<h2>Connections</h2><ul>{''.join(accessible_edges) if accessible_edges else '<li>None specified</li>'}</ul></details>
</body></html>'''


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="JSON graph specification")
    parser.add_argument("output", type=Path, help="new HTML file to create")
    parser.add_argument("--theme", choices=("dark", "light"), default="dark")
    parser.add_argument("--columns", type=int, choices=range(1, 7), default=3,
                        help="grid columns, from 1 to 6 (default: 3)")
    args = parser.parse_args()
    try:
        data = json.loads(args.input.read_text(encoding="utf-8"))
        document = render(data, args.theme, args.columns)
        with args.output.open("x", encoding="utf-8", newline="\n") as output:
            output.write(document)
    except (OSError, ValueError, RecursionError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"Created {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
