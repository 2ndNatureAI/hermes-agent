"""Undated skills remain visible when the timeline also contains dated nodes."""
from agent.learning_graph_render import render_frames, render_graph


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
    assert output["frames"][-1]["date"] == "15 Nov 2023"
    assert [bucket["label"] for bucket in output["buckets"]] == ["undated", "14 Nov", "15 Nov"]
    rows = ["".join(run[0] for run in row) for row in output["frames"][-1]["grid"]]
    assert "◀ now" in next(row for row in rows if "15 Nov" in row)
    assert "◀ now" not in next(row for row in rows if "undated" in row)


def test_undated_nodes_stay_separate_when_many_years_require_time_bins():
    nodes = [{"id": str(year), "kind": "skill", "timestamp": year * 31557600}
             for year in range(50)]
    nodes.append({"id": "without-date", "kind": "memory", "timestamp": None})
    output = render_frames({"nodes": nodes}, rows=14, frames=2)
    assert output["frames"][-1]["visible"] == len(nodes)
    bucket = next(bucket for bucket in output["buckets"] if bucket["label"] == "undated")
    assert [node["id"] for node in bucket["nodes"]] == ["without-date"]
    assert bucket["memories"] == 1


def test_one_date_and_undated_never_fabricates_a_date():
    nodes = [{"id": "known", "kind": "skill", "timestamp": 1700000000},
             {"id": "unknown", "kind": "memory", "timestamp": None}]
    output = render_frames({"nodes": nodes}, frames=5)
    assert [(b["label"], b["date"]) for b in output["buckets"]] == [
        ("undated", "unknown"), ("14 Nov", "14 Nov 2023")]
    assert output["frames"][-1]["date"] == "14 Nov 2023"
    assert output["axis"] == {"start": "14 Nov 2023", "end": "14 Nov 2023"}
    assert output["frames"][1]["date"] == "unknown"
    rows = ["".join(run[0] for run in row) for row in output["frames"][-1]["grid"]]
    assert "◀ now" in next(row for row in rows if "14 Nov" in row)


def test_all_undated_has_one_unknown_bucket_without_now_or_epoch():
    nodes = [{"id": "a", "kind": "skill", "timestamp": None},
             {"id": "b", "kind": "memory", "timestamp": None}]
    output = render_frames({"nodes": nodes}, frames=2)
    assert [(b["label"], b["date"], b["total"]) for b in output["buckets"]] == [("undated", "unknown", 2)]
    assert output["axis"] == {"start": "unknown", "end": "unknown"}
    assert output["frames"][-1]["date"] == "unknown"
    assert "◀ now" not in "".join(run[0] for row in render_graph({"nodes": nodes})["grid"] for run in row)


def test_undated_memory_keeps_drilldown_body_without_becoming_trajectory_time():
    nodes = [{"id": "memory:profile:0", "kind": "memory", "timestamp": None},
             {"id": "dated", "kind": "skill", "timestamp": 1700000000}]
    payload = {"nodes": nodes, "memory": [{"source": "profile", "body": "synthetic body"}]}
    output = render_frames(payload, frames=3)
    assert output["buckets"][0]["nodes"][0]["body"] == "synthetic body"
    assert output["frames"][1]["visible"] == 1
    assert "✦" not in "".join(run[0] for run in output["frames"][1]["grid"][-1])


def test_unix_epoch_is_a_known_date():
    output = render_frames({"nodes": [{"id": "epoch", "kind": "skill", "timestamp": 0},
                                      {"id": "unknown", "kind": "skill", "timestamp": None}]}, frames=2)
    assert output["buckets"][1]["date"] == "1 Jan 1970"
    assert output["frames"][-1]["date"] == "1 Jan 1970"


def test_short_tui_frame_and_drilldown_share_the_same_buckets():
    nodes = [{"id": str(year), "kind": "skill", "timestamp": year * 31557600} for year in range(50)]
    nodes.append({"id": "undated", "kind": "memory", "timestamp": None})
    output = render_frames({"nodes": nodes}, rows=5, frames=2)
    chart = output["frames"][-1]["grid"][:-1]
    assert len(chart) == len(output["buckets"])
    for row, bucket in zip(chart, output["buckets"]):
        assert row[0][0].strip() == bucket["label"]


def test_empty_time_bins_do_not_take_the_latest_marker():
    nodes = [{"id": str(year), "kind": "skill", "timestamp": year * 31557600} for year in range(1, 13)]
    nodes.append({"id": "far", "kind": "skill", "timestamp": 100 * 31557600})
    frame = render_graph({"nodes": nodes}, rows=14, reveal=0.5)
    rows = ["".join(run[0] for run in row) for row in frame["grid"]]
    marked = [row for row in rows if "◀ now" in row]
    assert len(marked) == 1
    assert "  0  ◀ now" not in marked[0]
