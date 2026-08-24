"""Smoke-test the user-facing example that demonstrates every action."""

import importlib.util
from pathlib import Path

EXAMPLE_PATH = Path(__file__).resolve().parents[1] / "examples" / "example_provenance_workflow.py"


def load_example_module():
    """Load the standalone example without making examples a Python package."""
    spec = importlib.util.spec_from_file_location("example_provenance_workflow", EXAMPLE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_complete_provenance_workflow_example(tmp_path: Path):
    """The example records all actions, output roles, mappings, and graph paths."""
    module = load_example_module()
    manager, references = module.build_workflow(tmp_path, "example.aca")
    info = manager.info()

    assert [activity.action for activity in info.activities.values()] == [
        "reduction",
        "projection",
        "quantity_of_interest",
        "visualization",
    ]
    assert [output.role for output in list(info.activities.values())[2].outputs] == ["mean", "maximum"]

    visualization = list(info.activities.values())[3]
    assert [activity_input.role for activity_input in visualization.inputs] == ["color", "annotation"]
    for activity_input in visualization.inputs:
        mapping = activity_input.step_mappings[0]
        assert [mapping.source_step(index) for index in range(3)] == [0, 5, 10]

    assert [root.reference for root in info.root_sources(references["image"])] == [references["pressure"]]
    assert [
        product.reference for product in info.derived_variables_from(references["pressure"], action="visualization")
    ] == [references["image"]]
    manager.close()
