"""Undated skills remain visible when the timeline also contains dated nodes."""
from agent.learning_graph_render import render_frames


def test_mixed_dated_and_undated_nodes_are_all_explorable():
    nodes = [
        {"id": "old", "kind": "skill", "label": "Old", "timestamp": 1700000000},
        {"id": "new", "kind": "skill", "label": "New", "timestamp": 1700086400},
        {"id": "unknown", "kind": "skill", "label": "Undated", "timestamp": None},
    ]
    output = render_frames({"nodes": nodes}, frames=2)
    assert output["count"] == output["frames"][-1]["visible"] == 3
    bucket = next(bucket for bucket in output["buckets"] if bucket["label"] == "undated")
    assert bucket["date"] == "unknown"
    assert [node["id"] for node in bucket["nodes"]] == ["unknown"]
    assert output["frames"][-1]["date"] == "unknown"


def test_undated_nodes_stay_separate_when_many_years_require_time_bins():
    nodes = [{"id": str(year), "kind": "skill", "timestamp": year * 31557600}
             for year in range(50)]
    nodes.append({"id": "without-date", "kind": "memory", "timestamp": None})
    output = render_frames({"nodes": nodes}, rows=14, frames=2)
    assert output["frames"][-1]["visible"] == len(nodes)
    bucket = next(bucket for bucket in output["buckets"] if bucket["label"] == "undated")
    assert [node["id"] for node in bucket["nodes"]] == ["without-date"]
    assert bucket["memories"] == 1
