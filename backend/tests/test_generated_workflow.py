from __future__ import annotations

from app.api.agents import coerce_generated_workflow


def test_coerce_generated_workflow_accepts_react_flow_nodes() -> None:
    workflow = coerce_generated_workflow(
        {
            "nodes": [
                {
                    "id": "trigger",
                    "type": "default",
                    "data": {"kind": "Trigger", "label": "Inbound airport call"},
                    "position": {"x": "80", "y": "80"},
                },
                {
                    "id": "agent step",
                    "type": "agent",
                    "name": "Answer traveler questions",
                },
                {"id": "qa-1", "type": "QA", "label": "Score the call"},
            ],
            "edges": [
                {
                    "source": "trigger",
                    "target": "agent step",
                    "data": {"condition": "sessionComplete"},
                }
            ],
        }
    )
    types = {node["type"] for node in workflow["nodes"]}
    assert {"Trigger", "Agent", "Handoff", "End"} <= types
    assert "QA" not in types
    trigger = next(node for node in workflow["nodes"] if node["type"] == "Trigger")
    assert trigger["label"] == "Inbound airport call"
    assert trigger["position"] == {"x": 80.0, "y": 80.0}
    assert any(edge.get("condition") == "sessionComplete" for edge in workflow["edges"])


def test_coerce_generated_workflow_builds_a_draft_when_nodes_are_missing() -> None:
    workflow = coerce_generated_workflow({"nodes": [], "edges": []})
    types = [node["type"] for node in workflow["nodes"]]
    assert types[:2] == ["Trigger", "Agent"]
    assert "Handoff" in types
    assert "End" in types
    assert workflow["edges"]
