import json
from pathlib import Path

import pytest
from PIL import Image

from hpc_campaign import Manager, VariableRef, VariableSpec
from hpc_campaign.manager import _apply_activity_manifest, _apply_variable_manifest
from hpc_campaign.manager_args import ArgParser

REPO_ROOT = Path(__file__).resolve().parents[1]
SAMPLE_DATASET = REPO_ROOT / "data" / "onearray.h5"


def open_campaign(tmp_path: Path, name: str = "variables.aca") -> Manager:
    """Create a fresh campaign with one physical dataset for source variables."""
    manager = Manager(name, campaign_store=str(tmp_path))
    manager.open(create=True, truncate=True)
    manager.data(SAMPLE_DATASET, name="output")
    return manager


def add_payloads(manager: Manager, tmp_path: Path, names: list[str]) -> list[str]:
    """Create small embedded payload datasets for chunk and mapping tests."""
    for name in names:
        path = tmp_path / f"{name}.txt"
        path.write_text(name, encoding="utf-8")
        manager.text(path, name=name, store=True)
    return names


def add_pressure(manager: Manager, *, run: str = "run-1", primary: bool = True) -> VariableRef:
    """Register the common root entity used by graph-oriented tests."""
    return manager.add_variable(
        run=run,
        dataset="output",
        variable="pressure",
        definition="pressure",
        primary=primary,
    )


def test_source_variable_has_stable_identity_and_explicit_primary_binding(tmp_path: Path):
    """Primary status is a per-run binding, not a property inferred from names."""
    manager = open_campaign(tmp_path)
    pressure = add_pressure(manager)

    stored = manager.info().find_variable("output", "pressure", run="run-1")
    assert pressure == VariableRef("run-1", "output", "pressure")
    assert stored.reference == pressure
    assert stored.uuid
    assert stored.definition == "pressure"
    assert stored.primary
    assert stored.generated_by is None
    assert manager.info().primary_variables(run="run-1", definition="pressure") == [stored]
    assert manager.cur.execute("pragma foreign_keys").fetchone()[0] == 1


def test_primary_binding_rejects_a_second_entity_for_the_same_definition(tmp_path: Path):
    """A run has at most one primary data product for each scientific definition."""
    manager = open_campaign(tmp_path)
    add_pressure(manager)
    alternate = manager.add_variable(
        run="run-1",
        dataset="output",
        variable="pressure-copy",
        definition="pressure",
    )

    with pytest.raises(ValueError, match="different primary variable"):
        manager.set_primary_variable(alternate)


def test_same_physical_dataset_can_supply_variables_to_multiple_runs(tmp_path: Path):
    """Run-qualified variable identity permits shared input or aggregate datasets."""
    manager = open_campaign(tmp_path)
    first = add_pressure(manager, run="run-1")
    second = add_pressure(manager, run="run-2")

    assert first == VariableRef("run-1", "output", "pressure")
    assert second == VariableRef("run-2", "output", "pressure")
    assert len(manager.info().primary_variables(definition="pressure")) == 2
    assert (
        manager.cur.execute(
            "select count(*) from sqlite_master where type = 'table' and name = 'dataset_run'"
        ).fetchone()[0]
        == 0
    )


def test_definition_normalizes_code_specific_names_without_declaring_run_requirements(tmp_path: Path):
    """Definitions group observed products but do not require every definition in every run."""
    manager = open_campaign(tmp_path)
    manager.add_variable(run="code-a", dataset="output", variable="P", definition="pressure")
    manager.add_variable(run="code-a", dataset="output", variable="T", definition="temperature")
    manager.add_variable(run="code-b", dataset="output", variable="press", definition="pressure")
    manager.add_variable(run="code-c", dataset="output", variable="P", definition="Pressure")

    variables = list(manager.info().variables.values())
    pressure_names = {item.variable for item in variables if item.definition == "pressure"}
    temperature_runs = {item.run for item in variables if item.definition == "temperature"}
    assert pressure_names == {"P", "press"}
    assert temperature_runs == {"code-a"}
    assert {item.run for item in variables if item.definition == "Pressure"} == {"code-c"}


def test_pressure_reduce_visualize_workflow_is_reconstructed_from_activities(tmp_path: Path):
    """The workflow is implicit in activity inputs/outputs and needs no workflow row."""
    manager = open_campaign(tmp_path)
    pressure = add_pressure(manager)
    reduced = manager.add_activity(
        action="reduction",
        inputs={"source": pressure},
        outputs={
            "result": VariableSpec(
                run="run-1",
                dataset="output",
                variable="pressure-reduced",
                definition="pressure",
            )
        },
        action_spec={"method": "mgard", "error_bound": 1e-4},
    ).outputs["result"]
    image = manager.add_activity(
        action="visualization",
        inputs={"color": reduced},
        outputs={
            "image": VariableSpec(
                run="run-1",
                dataset="visualizations",
                variable="pressure-image",
                definition="pressure",
            )
        },
        action_spec={"colormap": "viridis"},
    ).outputs["image"]

    info = manager.info()
    assert [activity.action for activity in info.activities.values()] == ["reduction", "visualization"]
    assert info.paths_to_root_sources(image) == [[image, reduced, pressure]]
    assert [item.reference for item in info.root_sources(image)] == [pressure]
    assert [item.reference for item in info.derived_variables_from(pressure)] == [reduced, image]
    assert [item.reference for item in info.derived_variables_from(pressure, action="visualization")] == [image]
    assert manager.cur.execute("pragma foreign_key_check").fetchall() == []


def test_visualization_can_use_two_variables_with_distinct_roles(tmp_path: Path):
    """Two activity_input rows describe how two variables contribute to one image."""
    manager = open_campaign(tmp_path)
    pressure = add_pressure(manager)
    temperature = manager.add_variable(run="run-1", dataset="output", variable="temperature")
    result = manager.add_activity(
        action="visualization",
        inputs={"color": pressure, "contours": temperature},
        outputs={
            "image": VariableSpec(
                run="run-1",
                dataset="visualizations",
                variable="overlay",
            )
        },
    )

    activity = manager.info().find_activity(result.activity)
    assert [(item.role, item.reference) for item in activity.inputs] == [
        ("color", pressure),
        ("contours", temperature),
    ]
    assert [(item.role, item.reference) for item in activity.outputs] == [
        ("image", result.outputs["image"]),
    ]


@pytest.mark.parametrize(
    ("action", "category"),
    [
        ("reduction", "transformation"),
        ("projection", "transformation"),
        ("quantity_of_interest", "analysis"),
        ("visualization", "presentation"),
    ],
)
def test_controlled_activity_vocabulary_records_categories(tmp_path: Path, action: str, category: str):
    """Every supported action resolves to its fixed structural category."""
    manager = open_campaign(tmp_path, f"{action}.aca")
    pressure = add_pressure(manager)
    result = manager.add_activity(
        action=action,
        inputs={"source": pressure},
        outputs={"result": VariableSpec(run="run-1", dataset="products", variable=action)},
    )

    stored = manager.info().find_activity(result.activity)
    assert stored.action == action
    assert stored.category == category


def test_unknown_activity_action_is_rejected_without_mutating_the_graph(tmp_path: Path):
    """Producer-defined action names cannot bypass the controlled vocabulary."""
    manager = open_campaign(tmp_path)
    pressure = add_pressure(manager)

    with pytest.raises(ValueError, match="unsupported action"):
        manager.add_activity(
            action="feature_detection",
            inputs={"source": pressure},
            outputs={"result": VariableSpec(run="run-1", dataset="products", variable="features")},
        )

    assert manager.info().activities == {}
    with pytest.raises(LookupError):
        manager.info().find_variable("products", "features", run="run-1")


def test_activity_can_generate_multiple_role_qualified_outputs(tmp_path: Path):
    """One activity row can generate several entities without duplicating the action."""
    manager = open_campaign(tmp_path)
    pressure = add_pressure(manager)
    result = manager.add_activity(
        action="quantity_of_interest",
        inputs={"source": pressure},
        outputs={
            "mean": VariableSpec(run="run-1", dataset="qoi", variable="pressure-mean"),
            "maximum": VariableSpec(run="run-1", dataset="qoi", variable="pressure-max"),
        },
        action_spec={"region": "domain"},
    )

    activity = manager.info().find_activity(result.activity)
    assert set(result.outputs) == {"mean", "maximum"}
    assert [item.role for item in activity.outputs] == ["mean", "maximum"]
    for reference in result.outputs.values():
        stored = manager.info().find_variable(reference.dataset, reference.variable, reference.run)
        assert stored.generated_by == result.activity


def test_sequential_qoi_activities_create_field_and_scalar_definitions(tmp_path: Path):
    """A field QoI and a later scalar integral remain distinct provenance operations."""
    manager = open_campaign(tmp_path)
    pressure = add_pressure(manager)
    temperature = manager.add_variable(
        run="run-1",
        dataset="output",
        variable="temperature",
        definition="temperature",
    )
    flux = manager.add_activity(
        action="quantity_of_interest",
        inputs={"pressure": pressure, "temperature": temperature},
        outputs={
            "field": VariableSpec(
                run="run-1",
                dataset="qoi",
                variable="flux",
                definition="flux",
            )
        },
        action_spec={"operation": "flux"},
    ).outputs["field"]
    total_flux = manager.add_activity(
        action="quantity_of_interest",
        inputs={"flux": flux},
        outputs={
            "total": VariableSpec(
                run="run-1",
                dataset="qoi",
                variable="total-flux",
                definition="total_flux",
            )
        },
        action_spec={"operation": "integral", "domain": "boundary"},
    ).outputs["total"]

    info = manager.info()
    assert info.find_variable("qoi", "flux", run="run-1").definition == "flux"
    assert info.find_variable("qoi", "total-flux", run="run-1").definition == "total_flux"
    assert info.paths_to_root_sources(total_flux) == [
        [total_flux, flux, pressure],
        [total_flux, flux, temperature],
    ]


def test_action_specs_are_json_checked_and_content_deduplicated(tmp_path: Path):
    """Equal immutable action specifications share one compact database row."""
    manager = open_campaign(tmp_path)
    pressure = add_pressure(manager)
    spec = {"method": "mgard", "error_bound": 1e-4}
    for suffix in ("a", "b"):
        manager.add_activity(
            action="reduction",
            inputs={"source": pressure},
            outputs={"result": VariableSpec(run="run-1", dataset="output", variable=f"reduced-{suffix}")},
            action_spec=spec,
        )

    assert manager.cur.execute("select count(*) from action_spec").fetchone()[0] == 1
    with pytest.raises(ValueError, match="JSON-compatible"):
        manager.add_activity(
            action="reduction",
            inputs={"source": pressure},
            outputs={"result": VariableSpec(run="run-1", dataset="output", variable="bad-spec")},
            action_spec={"bad": object()},
        )
    with pytest.raises(LookupError):
        manager.info().find_variable("output", "bad-spec", run="run-1")


def test_step_mappings_use_identity_stride_and_explicit_encodings(tmp_path: Path):
    """Common mappings remain compact while irregular mappings stay lossless."""
    manager = open_campaign(tmp_path)
    source = add_pressure(manager)
    payloads = add_payloads(
        manager,
        tmp_path,
        [f"{kind}-{index}" for kind in ("identity", "stride", "explicit") for index in range(3)],
    )
    mappings = {
        "identity": [0, 1, 2],
        "stride": {"start": 0, "count": 3, "stride": 5},
        "explicit": [1, 4, 10],
    }
    expected_steps = {"identity": [0, 1, 2], "stride": [0, 5, 10], "explicit": [1, 4, 10]}
    results = {}
    for offset, (name, steps) in enumerate(mappings.items()):
        results[name] = manager.add_activity(
            action="visualization",
            inputs={"source": source},
            outputs={
                "image": VariableSpec(
                    run="run-1",
                    dataset="images",
                    variable=name,
                    chunks=payloads[offset * 3 : offset * 3 + 3],
                )
            },
            source_steps=steps,
        )

    info = manager.info()
    for name, result in results.items():
        mapping = info.find_activity(result.activity).inputs[0].step_mappings[0]
        assert mapping.encoding == name
        assert [mapping.source_step(index) for index in range(3)] == expected_steps[name]
    # Three output sequences produce three compact rows, not nine per-step rows.
    assert manager.cur.execute("select count(*) from activity_input_step_mapping").fetchone()[0] == 3


def test_multi_input_source_steps_are_stored_per_role(tmp_path: Path):
    """Each input role has its own compact mapping to the same output sequence."""
    manager = open_campaign(tmp_path)
    pressure = add_pressure(manager)
    temperature = manager.add_variable(run="run-1", dataset="output", variable="temperature")
    payloads = add_payloads(manager, tmp_path, ["overlay-0", "overlay-1"])
    result = manager.add_activity(
        action="visualization",
        inputs={"color": pressure, "contours": temperature},
        outputs={
            "image": VariableSpec(
                run="run-1",
                dataset="images",
                variable="overlay",
                chunks=payloads,
            )
        },
        source_steps={"color": [20, 25], "contours": [20, 20]},
    )

    activity = manager.info().find_activity(result.activity)
    by_role = {item.role: item.step_mappings[0] for item in activity.inputs}
    assert [by_role["color"].source_step(index) for index in (0, 1)] == [20, 25]
    assert [by_role["contours"].source_step(index) for index in (0, 1)] == [20, 20]


def test_multi_output_source_steps_use_an_outer_output_role_mapping(tmp_path: Path):
    """Each output sequence can map independently to the same activity input."""
    manager = open_campaign(tmp_path)
    pressure = add_pressure(manager)
    add_payloads(manager, tmp_path, ["mean-0", "mean-1", "max-0", "max-1"])
    result = manager.add_activity(
        action="quantity_of_interest",
        inputs={"source": pressure},
        outputs={
            "mean": VariableSpec(
                run="run-1",
                dataset="qoi",
                variable="mean",
                chunks=["mean-0", "mean-1"],
            ),
            "maximum": VariableSpec(
                run="run-1",
                dataset="qoi",
                variable="maximum",
                chunks=["max-0", "max-1"],
            ),
        },
        source_steps={
            "mean": {"source": {"start": 0, "count": 2, "stride": 5}},
            "maximum": {"source": [2, 9]},
        },
    )

    mappings = manager.info().find_activity(result.activity).inputs[0].step_mappings
    by_output = {mapping.output_variable_id: mapping for mapping in mappings}
    for role, expected in (("mean", [0, 5]), ("maximum", [2, 9])):
        variable_id = manager.info().find_variable("qoi", role, run="run-1").id
        mapping = by_output[variable_id]
        assert [mapping.source_step(index) for index in (0, 1)] == expected


def test_activity_append_reuses_generator_and_adds_one_mapping_batch(tmp_path: Path):
    """Appending images extends the existing product and generating activity."""
    manager = open_campaign(tmp_path)
    source = add_pressure(manager)
    add_payloads(manager, tmp_path, ["frame-0", "frame-1", "frame-2"])
    initial = manager.add_activity(
        action="visualization",
        inputs={"source": source},
        outputs={
            "image": VariableSpec(
                run="run-1",
                dataset="images",
                variable="pressure",
                chunks=["frame-0", "frame-1"],
            )
        },
        action_spec={"colormap": "viridis"},
        source_steps={"start": 0, "count": 2, "stride": 5},
    )
    appended = manager.add_activity(
        action="visualization",
        inputs={"source": source},
        outputs={
            "image": VariableSpec(
                run="run-1",
                dataset="images",
                variable="pressure",
                chunks=["frame-2"],
                append=True,
            )
        },
        source_steps=[10],
    )

    assert appended.activity == initial.activity
    stored = manager.info().find_variable("images", "pressure", run="run-1")
    assert [chunk.chunk_index for chunk in stored.chunks] == [0, 1, 2]
    mappings = manager.info().find_activity(initial.activity).inputs[0].step_mappings
    assert [(item.output_start, item.count) for item in mappings] == [(0, 2), (2, 1)]


def test_failed_multi_output_write_rolls_back_earlier_output(tmp_path: Path):
    """Outputs and the activity are one transaction even when a later output fails."""
    manager = open_campaign(tmp_path)
    pressure = add_pressure(manager)

    with pytest.raises(ValueError, match="already exists"):
        manager.add_activity(
            action="reduction",
            inputs={"source": pressure},
            outputs={
                "temporary": VariableSpec(run="run-1", dataset="products", variable="temporary"),
                "conflict": VariableSpec(run="run-1", dataset="output", variable="pressure"),
            },
        )

    with pytest.raises(LookupError):
        manager.info().find_variable("products", "temporary", run="run-1")
    assert manager.info().activities == {}


def test_cross_run_activity_has_no_single_run_owner(tmp_path: Path):
    """An activity spanning runs remains valid but does not claim one run ID."""
    manager = open_campaign(tmp_path)
    manager.data(SAMPLE_DATASET, name="run-2-output")
    left = add_pressure(manager, run="run-1")
    right = manager.add_variable(run="run-2", dataset="run-2-output", variable="temperature")
    result = manager.add_activity(
        action="visualization",
        inputs={"color": left, "contours": right},
        outputs={"image": VariableSpec(run="run-1", dataset="images", variable="cross-run")},
    )

    activity = manager.info().find_activity(result.activity)
    assert activity.run is None
    assert activity.run_id is None


def test_delete_requires_cascade_for_downstream_products(tmp_path: Path):
    """Deletion reports and protects the complete downstream activity graph."""
    manager = open_campaign(tmp_path)
    pressure = add_pressure(manager)
    reduced = manager.add_activity(
        action="reduction",
        inputs={"source": pressure},
        outputs={"result": VariableSpec(run="run-1", dataset="output", variable="reduced")},
    ).outputs["result"]
    image = manager.add_activity(
        action="visualization",
        inputs={"source": reduced},
        outputs={"image": VariableSpec(run="run-1", dataset="images", variable="image")},
    ).outputs["image"]

    impact = manager.variable_delete_impact(pressure)
    assert impact.dependent_variables == (reduced, image)
    with pytest.raises(ValueError, match="still referenced"):
        manager.delete_variable(pressure)
    manager.delete_variable(pressure, cascade=True)
    assert manager.info().variables == {}
    assert manager.info().activities == {}


def test_image_sequence_records_visualization_and_every_fifth_step(tmp_path: Path):
    """The image helper ingests payloads but delegates provenance to add_activity."""
    manager = open_campaign(tmp_path)
    pressure = add_pressure(manager)
    image_paths = []
    for index in range(2):
        path = tmp_path / f"frame-{index}.png"
        Image.new("RGB", (4, 3), color=(index * 20, 0, 0)).save(path)
        image_paths.append(path)

    sequence = manager.add_image_sequence(
        run="run-1",
        dataset="images",
        variable="pressure",
        definition="pressure",
        images=image_paths,
        inputs={"source": pressure},
        source_steps={"start": 0, "count": 2, "stride": 5},
        action_spec={"colormap": "viridis"},
        store=True,
    )

    info = manager.info()
    stored = info.find_variable(sequence.dataset, sequence.variable, sequence.run)
    activity = info.find_activity(stored.generated_by)
    mapping = activity.inputs[0].step_mappings[0]
    assert activity.action == "visualization"
    assert activity.action_spec == {"colormap": "viridis"}
    assert [mapping.source_step(index) for index in (0, 1)] == [0, 5]
    assert len(stored.chunks) == 2


def test_image_sequence_validates_homogeneity_before_ingesting_payloads(tmp_path: Path):
    """A mixed-resolution sequence fails without leaving image datasets or provenance rows."""
    manager = open_campaign(tmp_path)
    pressure = add_pressure(manager)
    first = tmp_path / "first.png"
    second = tmp_path / "second.png"
    Image.new("RGB", (4, 3), color="red").save(first)
    Image.new("RGB", (8, 3), color="blue").save(second)

    with pytest.raises(ValueError, match="same resolution"):
        manager.add_image_sequence(
            run="run-1",
            dataset="images",
            variable="invalid",
            images=[first, second],
            inputs={"source": pressure},
            store=True,
        )

    assert manager.info().activities == {}
    assert manager.cur.execute("select count(*) from dataset where fileformat = 'IMAGE'").fetchone()[0] == 0


def test_in_memory_image_requires_embedded_storage(tmp_path: Path):
    """An in-memory image has no external replica path and therefore requires store=True."""
    manager = open_campaign(tmp_path)
    pressure = add_pressure(manager)

    with pytest.raises(ValueError, match="In-memory images require store=True"):
        manager.add_image_sequence(
            run="run-1",
            dataset="images",
            variable="memory",
            images=[Image.new("RGB", (4, 3))],
            inputs={"source": pressure},
        )


def test_image_payload_ingestion_rolls_back_when_activity_input_is_missing(tmp_path: Path):
    """The outer image transaction removes payloads when provenance creation later fails."""
    manager = open_campaign(tmp_path)
    add_pressure(manager)
    path = tmp_path / "frame.png"
    Image.new("RGB", (4, 3), color="red").save(path)
    missing = VariableRef("run-1", "output", "missing")

    with pytest.raises(LookupError, match="Logical variable not found"):
        manager.add_image_sequence(
            run="run-1",
            dataset="images",
            variable="invalid",
            images=[path],
            inputs={"source": missing},
            store=True,
        )

    assert manager.cur.execute("select count(*) from dataset where fileformat = 'IMAGE'").fetchone()[0] == 0
    assert manager.info().activities == {}


def test_append_cannot_change_immutable_action_spec(tmp_path: Path):
    """Appending chunks cannot reinterpret the action that generated earlier chunks."""
    manager = open_campaign(tmp_path)
    pressure = add_pressure(manager)
    add_payloads(manager, tmp_path, ["frame-a", "frame-b"])
    manager.add_activity(
        action="visualization",
        inputs={"source": pressure},
        outputs={
            "image": VariableSpec(
                run="run-1",
                dataset="images",
                variable="sequence",
                chunks=["frame-a"],
            )
        },
        action_spec={"colormap": "viridis"},
        source_steps=[0],
    )

    with pytest.raises(ValueError, match="cannot change an action specification"):
        manager.add_activity(
            action="visualization",
            inputs={"source": pressure},
            outputs={
                "image": VariableSpec(
                    run="run-1",
                    dataset="images",
                    variable="sequence",
                    chunks=["frame-b"],
                    append=True,
                )
            },
            action_spec={"colormap": "plasma"},
            source_steps=[5],
        )

    assert len(manager.info().find_variable("images", "sequence", run="run-1").chunks) == 1


def test_stable_uuids_survive_close_and_reopen(tmp_path: Path):
    """Run, entity, and activity UUIDs remain stable across Manager sessions."""
    archive_name = "stable-identities.aca"
    manager = open_campaign(tmp_path, archive_name)
    pressure = add_pressure(manager)
    result = manager.add_activity(
        action="projection",
        inputs={"source": pressure},
        outputs={"result": VariableSpec(run="run-1", dataset="products", variable="slice")},
    )
    before = manager.info()
    identities = (
        before.runs[before.find_variable("output", "pressure", run="run-1").run_id].uuid,
        before.find_variable("output", "pressure", run="run-1").uuid,
        before.find_activity(result.activity).uuid,
    )
    manager.close()

    reopened = Manager(archive_name, campaign_store=str(tmp_path))
    reopened.open(create=False)
    after = reopened.info()
    assert (
        after.runs[after.find_variable("output", "pressure", run="run-1").run_id].uuid,
        after.find_variable("output", "pressure", run="run-1").uuid,
        after.find_activity(result.activity).uuid,
    ) == identities


def test_logical_namespace_can_hold_products_from_multiple_runs(tmp_path: Path):
    """Only physical datasets are single-run; a shared logical namespace can span runs."""
    manager = open_campaign(tmp_path)
    manager.data(SAMPLE_DATASET, name="run-2-output")
    run_one = add_pressure(manager, run="run-1")
    run_two = manager.add_variable(run="run-2", dataset="run-2-output", variable="pressure")
    for run, source in (("run-1", run_one), ("run-2", run_two)):
        manager.add_activity(
            action="projection",
            inputs={"source": source},
            outputs={"result": VariableSpec(run=run, dataset="products", variable="pressure-slice")},
        )

    info = manager.info()
    assert info.find_variable("products", "pressure-slice", run="run-1").run == "run-1"
    assert info.find_variable("products", "pressure-slice", run="run-2").run == "run-2"


def test_compact_descriptor_is_unambiguous_when_the_input_role_is_start(tmp_path: Path):
    """Descriptor key names do not collide with a user-selected input role."""
    manager = open_campaign(tmp_path)
    pressure = add_pressure(manager)
    add_payloads(manager, tmp_path, ["role-start-0", "role-start-1"])
    result = manager.add_activity(
        action="visualization",
        inputs={"start": pressure},
        outputs={
            "image": VariableSpec(
                run="run-1",
                dataset="images",
                variable="role-start",
                chunks=["role-start-0", "role-start-1"],
            )
        },
        source_steps={"start": 0, "count": 2, "stride": 5},
    )

    mapping = manager.info().find_activity(result.activity).inputs[0].step_mappings[0]
    assert [mapping.source_step(index) for index in (0, 1)] == [0, 5]


def test_obsolete_provenance_manifest_fields_fail_loudly(tmp_path: Path):
    """Old direct-edge fields cannot be silently ignored and lose provenance."""
    manager = open_campaign(tmp_path)

    with pytest.raises(ValueError, match="unsupported field.*derived_from"):
        _apply_variable_manifest(
            manager,
            {
                "dataset": "output",
                "variable": "pressure",
                "derived_from": {"dataset": "output", "variable": "temperature"},
            },
        )


def test_variable_and_activity_manifests_use_run_qualified_references(tmp_path: Path):
    """CLI manifest helpers expose the same explicit entity/activity structure."""
    manager = open_campaign(tmp_path)
    pressure = _apply_variable_manifest(
        manager,
        {
            "run": "run-1",
            "dataset": "output",
            "variable": "pressure",
            "definition": "pressure",
            "primary": True,
        },
    )
    result = _apply_activity_manifest(
        manager,
        {
            "action": "reduction",
            "inputs": {"source": {"run": "run-1", "dataset": "output", "variable": "pressure"}},
            "outputs": {
                "result": {
                    "run": "run-1",
                    "dataset": "output",
                    "variable": "pressure-reduced",
                    "definition": "pressure",
                }
            },
            "action_spec": {"method": "zfp"},
        },
    )

    assert pressure == VariableRef("run-1", "output", "pressure")
    assert manager.info().find_activity(result.activity).action_spec == {"method": "zfp"}


def test_activity_command_is_parsed_as_a_separate_cli_command():
    """The command splitter must not consume an activity manifest as another command's input."""
    parser = ArgParser(args=["demo.aca", "activity", "activity.json"], prog="hpc_campaign manager")
    assert parser.parse_next_command()
    assert parser.args.command == "activity"
    assert parser.args.manifest == "activity.json"


def test_activity_manifest_output_is_json_serializable(tmp_path: Path):
    """The manifest shape remains ordinary JSON rather than Python-only objects."""
    manifest = {
        "action": "projection",
        "inputs": {"source": {"run": "run-1", "dataset": "output", "variable": "pressure"}},
        "outputs": {"result": {"run": "run-1", "dataset": "products", "variable": "pressure-slice"}},
        "source_steps": {"start": 0, "count": 1, "stride": 1},
    }
    path = tmp_path / "activity.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    assert json.loads(path.read_text(encoding="utf-8")) == manifest
