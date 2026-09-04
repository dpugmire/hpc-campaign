import uuid
from pathlib import Path

from examples.provenance_workflow import build_example
from hpc_campaign import Manager
from hpc_campaign.prov_mapping import HPC
from tools.benchmark_provenance_scale import Scenario, build_document, measure


def test_end_to_end_example_builds_a_reopenable_scientific_graph(tmp_path: Path):
    export_path = build_example(tmp_path)

    manager = Manager(archive="provenance-example.aca", campaign_store=str(tmp_path))
    manager.open()
    document = manager.prov_document("campaign-provenance")
    asserted_types = {
        asserted_type for record in document.get_records() for asserted_type in record.get_asserted_types()
    }

    # This protects the example's intended teaching path, not just its syntax:
    # all three processing stages must reach persistent canonical PROV.
    assert len(list(document.get_records())) == 41
    assert HPC["Reduction"] in asserted_types
    assert HPC["QuantityOfInterest"] in asserted_types
    assert HPC["Visualization"] in asserted_types
    assert export_path.is_file()
    manager.close()


def test_scale_model_matches_materialized_record_count():
    scenario = Scenario(
        runs=2,
        variables_per_run=3,
        reductions_per_variable=1,
        qois_per_variable=2,
        visualizations_per_run=1,
    )
    document = build_document(
        scenario,
        # A fixed UUID makes the benchmark graph deterministic across runs.
        uuid.UUID("8c13a4ce-3662-43cd-a03b-2fdfd53bd65e"),
    )

    assert len(list(document.get_records())) == scenario.expected_records
    assert scenario.products == 26


def test_small_scale_measurement_exercises_storage_round_trip():
    result = measure(
        Scenario(
            runs=1,
            variables_per_run=1,
            reductions_per_variable=1,
            qois_per_variable=1,
            visualizations_per_run=0,
        ),
        repeats=1,
    )

    # The benchmark is versioned code, so a fast CI-sized case verifies every
    # measured phase without turning performance numbers into brittle limits.
    assert result.records == result.scenario.expected_records
    assert result.canonical_bytes > result.gzip_bytes > 0
    assert result.sqlite_bytes > 0
    assert result.sqlite_rewrite_seconds >= 0
