"""Exercise the standalone architecture diagram generator as a real CLI."""

import json
import subprocess
import sys
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[2] / "skills/creative/architecture-diagram/scripts/render_diagram.py"


def render(tmp_path, data, *extra):
    source = tmp_path / "diagram.json"
    output = tmp_path / "diagram.html"
    source.write_text(json.dumps(data), encoding="utf-8")
    result = subprocess.run([sys.executable, str(SCRIPT), str(source), str(output), *extra],
                            capture_output=True, text=True, encoding="utf-8")
    return result, output


def test_renders_accessible_offline_diagram(tmp_path):
    result, output = render(tmp_path, {
        "title": "System & data",
        "nodes": [{"id": "web", "label": "Browser <public>"},
                  {"id": "api", "label": "API", "description": "Processes requests"}],
        "edges": [{"from": "web", "to": "api", "label": "HTTPS"}],
    }, "--theme", "light")
    assert result.returncode == 0, result.stderr
    html = output.read_text(encoding="utf-8")
    assert 'role="img"' in html and "System &amp; data" in html
    assert "Browser &lt;public&gt;" in html and "Processes requests" in html
    assert "HTTPS" in html and "<script" not in html.lower()
    assert "https://" not in html and "http://" not in html


def test_rejects_unknown_edge_without_creating_output(tmp_path):
    result, output = render(tmp_path, {"nodes": [{"id": "a", "label": "A"}],
                                       "edges": [{"from": "a", "to": "missing"}]})
    assert result.returncode != 0 and "missing" in result.stderr
    assert not output.exists()


def test_duplicate_ids_are_rejected(tmp_path):
    result, output = render(tmp_path, {"nodes": [{"id": "a", "label": "A"},
                                                   {"id": "a", "label": "B"}]})
    assert result.returncode != 0 and "duplicate" in result.stderr.lower()
    assert not output.exists()


def test_existing_output_is_not_overwritten(tmp_path):
    output = tmp_path / "diagram.html"
    output.write_text("keep", encoding="utf-8")
    result, _ = render(tmp_path, {"nodes": [{"id": "a", "label": "A"}]})
    assert result.returncode != 0
    assert output.read_text(encoding="utf-8") == "keep"


def test_untrusted_labels_and_ids_are_inert(tmp_path):
    payload = '</text><script>fetch("https://example.invalid")</script>'
    result, output = render(tmp_path, {"title": payload,
        "nodes": [{"id": payload, "label": payload}, {"id": "b", "label": "B"}],
        "edges": [{"from": payload, "to": "b", "label": payload}]})
    assert result.returncode == 0, result.stderr
    html = output.read_text(encoding="utf-8")
    assert "<script" not in html.lower()
    assert "</text><script>" not in html.lower()
    assert "&lt;script&gt;" in html


def test_invalid_theme_is_rejected_before_output(tmp_path):
    result, output = render(tmp_path, {"nodes": [{"id": "a", "label": "A"}]},
                            "--theme", 'url("https://example.invalid")')
    assert result.returncode != 0
    assert not output.exists()


def test_columns_create_vertical_or_wider_grid(tmp_path):
    graph = {"nodes": [{"id": str(i), "label": str(i)} for i in range(4)]}
    result, output = render(tmp_path, graph, "--columns", "1")
    assert result.returncode == 0 and 'viewBox="0 0 380 760"' in output.read_text(encoding="utf-8")
    output.unlink()
    result, output = render(tmp_path, graph, "--columns", "4")
    assert result.returncode == 0 and 'viewBox="0 0 1280 250"' in output.read_text(encoding="utf-8")


def test_invalid_columns_do_not_create_output(tmp_path):
    result, output = render(tmp_path, {"nodes": [{"id": "a", "label": "A"}]}, "--columns", "0")
    assert result.returncode != 0 and not output.exists()


def test_long_component_labels_are_multiline_and_preserve_full_text(tmp_path):
    import xml.etree.ElementTree as ET

    label = 'Permission aware context engine with reviewable audit records'
    result, output = render(tmp_path, {'nodes': [{'id': 'engine', 'label': label}]})
    assert result.returncode == 0, result.stderr
    document = output.read_text(encoding='utf-8')
    svg_text = document[document.index('<svg '):document.index('</svg>') + len('</svg>')]
    svg = ET.fromstring(svg_text)
    spans = svg.findall('.//tspan')
    assert 2 <= len(spans) <= 3
    assert ' '.join(span.text for span in spans) == label
    assert all(len(span.text) <= 27 for span in spans)
    assert label in document


def test_extreme_component_label_has_visible_ellipsis_and_full_accessible_label(tmp_path):
    label = 'Long component description ' * 12
    result, output = render(tmp_path, {'nodes': [{'id': 'long', 'label': label}]})
    assert result.returncode == 0, result.stderr
    document = output.read_text(encoding='utf-8')
    assert '…</tspan>' in document
    assert label.strip() in document


def test_wide_unbroken_labels_wrap_before_overflow(tmp_path):
    import xml.etree.ElementTree as ET

    label = 'W' * 27
    result, output = render(tmp_path, {'nodes': [{'id': 'wide', 'label': label}]})
    assert result.returncode == 0, result.stderr
    document = output.read_text(encoding='utf-8')
    svg_text = document[document.index('<svg '):document.index('</svg>') + len('</svg>')]
    lines = [span.text for span in ET.fromstring(svg_text).findall('.//tspan')]
    assert len(lines) == 2
    assert ''.join(lines) == label
    assert all(len(line) <= 14 for line in lines)
