import json
import sqlite3
from pathlib import Path

import numpy as np
import pytest

from hpc_campaign.info import format_info
from hpc_campaign.manager import Manager
from hpc_campaign.manager import main as manager_main
from hpc_campaign.types import DatasetType

repo_root = Path(__file__).resolve().parents[1]
data_dir = repo_root / "data"


def gaussian_frame(count: int = 3, shift: float = 0.0) -> dict[str, np.ndarray]:
    return {
        "centers": np.arange(2 * count, dtype=np.float32).reshape(count, 2) / 10 + shift,
        "log_scales": np.full((count, 2), -2.5, dtype=np.float32),
        "angles": np.linspace(0, 0.5, count, dtype=np.float32),
        "amplitudes": np.linspace(-1, 1, count, dtype=np.float32),
        "bias": np.asarray([0.125], dtype=np.float32),
    }


def normalized_splat_metadata(scale: float = 2.0) -> dict:
    return {
        "coordinate_space": "normalized",
        "coordinate_transform": {
            "type": "affine",
            "physical_from_stored": {"scale": scale, "offset": [1.0, -1.0]},
        },
        "value_space": "normalized",
        "value_transform": {
            "type": "affine",
            "physical_from_stored": {"scale": 4.0, "offset": 0.25},
        },
    }


def add_source_dataset(manager: Manager, name: str = "output") -> None:
    manager.data(str(data_dir / "onearray.h5"), name=name)


def embedded_payload(archive_path: Path, dataset_name: str) -> bytes:
    con = sqlite3.connect(archive_path)
    row = con.execute(
        """
        select f.data
        from dataset as d
        join replica as r on r.datasetid = d.rowid
        join repfiles as rf on rf.replicaid = r.rowid
        join file as f on f.fileid = rf.fileid
        where d.name = ?
        """,
        (dataset_name,),
    ).fetchone()
    con.close()
    assert row is not None
    return bytes(row[0])


def test_gaussian_splat_storage_and_binary_layout(tmp_path: Path):
    archive_name = "gaussian_storage.aca"
    dataset_name = "representations/pressure/splat.000010.raw"
    frame = gaussian_frame()

    manager = Manager(archive=archive_name, campaign_store=str(tmp_path))
    manager.gaussian_splat_data(frame, name=dataset_name, metadata=normalized_splat_metadata())

    info_data = manager.info(list_replicas=True, list_files=True)
    assert info_data.archive.version == "0.7"
    dataset = next(dataset for dataset in info_data.datasets.values() if dataset.name == dataset_name)
    assert dataset.file_format == "GAUSSIAN_SPLAT"
    assert dataset.metadata is not None
    assert dataset.metadata["kind"] == "gaussianSplat"
    assert dataset.metadata["model"] == "anisotropic-2d-scalar"
    assert dataset.metadata["count"] == 3
    assert dataset.metadata["dtype"] == "float32"
    assert dataset.metadata["layout"] == "structure-of-arrays"
    assert dataset.metadata["coordinate_order"] == ["x", "y"]
    assert dataset.metadata["kernel"] == "unnormalized-anisotropic-gaussian"
    assert dataset.metadata["coordinate_space"] == "normalized"
    assert dataset.metadata["value_space"] == "normalized"
    assert next(iter(dataset.replicas.values())).resolution is None

    expected = b"".join(
        np.ascontiguousarray(frame[name], dtype="<f4").reshape(-1).tobytes()
        for name in ("centers", "log_scales", "angles", "amplitudes", "bias")
    )
    assert embedded_payload(tmp_path / archive_name, dataset_name) == expected
    assert dataset.metadata["payload_bytes"] == len(expected)
    assert [component["name"] for component in dataset.metadata["components"]] == [
        "centers",
        "log_scales",
        "angles",
        "amplitudes",
        "bias",
    ]
    manager.close()


def test_gaussian_splat_requires_explicit_spaces_and_valid_shapes(tmp_path: Path):
    manager = Manager(archive="gaussian_validation.aca", campaign_store=str(tmp_path))
    frame = gaussian_frame()

    with pytest.raises(ValueError, match="coordinate_space"):
        manager.gaussian_splat_data(frame)

    with pytest.raises(ValueError, match="coordinate_transform requires a non-empty type"):
        manager.gaussian_splat_data(
            frame,
            metadata={
                "coordinate_space": "normalized",
                "coordinate_transform": {"scale": 2.0},
                "value_space": "physical",
            },
        )

    invalid = dict(frame)
    invalid["angles"] = np.zeros(4, dtype=np.float32)
    with pytest.raises(ValueError, match="angles must have shape"):
        manager.gaussian_splat_data(invalid, metadata=normalized_splat_metadata())

    invalid = dict(frame)
    invalid["amplitudes"] = frame["amplitudes"].copy()
    invalid["amplitudes"][0] = np.nan
    with pytest.raises(ValueError, match="finite"):
        manager.gaussian_splat_data(invalid, metadata=normalized_splat_metadata())


def test_representation_append_sparse_steps_metrics_and_default_field_name(tmp_path: Path):
    archive_name = "gaussian_representation.aca"
    manager = Manager(archive=archive_name, campaign_store=str(tmp_path))
    manager.open(create=True, truncate=True)
    add_source_dataset(manager)
    metadata = normalized_splat_metadata()
    manager.gaussian_splat_data(gaussian_frame(shift=0.0), name="splats/pressure.000010.raw", metadata=metadata)
    manager.gaussian_splat_data(gaussian_frame(shift=0.1), name="splats/pressure.000020.raw", metadata=metadata)

    repid = manager.create_representation(
        name="output/representations/pressure/gaussian",
        representation_format="GAUSSIAN_SPLAT",
        sources=[{"dataset": "output", "variable": "pressure"}],
        temporal_interpolation="linear",
    )
    first_item = manager.append_representation_item(
        representation=repid,
        dataset="splats/pressure.000010.raw",
        source_step=10,
        source_time=0.5,
        metrics=[{"name": "rmse", "value": 0.02, "units": "Pa", "norm": "L2"}],
    )
    second_item = manager.append_representation_item(
        representation="output/representations/pressure/gaussian",
        dataset="splats/pressure.000020.raw",
        source_step=20,
        source_time=1.5,
    )
    assert second_item > first_item
    manager.add_representation_metric(
        representation=repid,
        name="mean_rmse",
        value=0.015,
        units="Pa",
        norm="L2",
    )

    info_data = manager.info()
    representation = info_data.representations[repid]
    assert representation.field_name == "pressure"
    assert representation.representation_format == "GAUSSIAN_SPLAT"
    assert representation.temporal_interpolation == "linear"
    assert representation.parameter_correspondence == "stable-index"
    assert len(representation.items) == 2
    assert [item.logical_time for item in representation.items] == [0.5, 1.5]
    assert representation.items[0].source_selections == {"pressure": {"step": 10, "time": 0.5}}
    assert representation.items[1].source_selections == {"pressure": {"step": 20, "time": 1.5}}
    assert representation.items[0].metrics[0].name == "rmse"
    assert representation.metrics[0].name == "mean_rmse"

    output = format_info(info_data)
    assert "Data Representations:" in output
    assert "field=pressure format=GAUSSIAN_SPLAT" in output
    assert 'pressure: {"step": 10, "time": 0.5}' in output
    manager.close()


def test_multi_source_representation_requires_field_name_and_maps_sources_independently(tmp_path: Path):
    manager = Manager(archive="multi_source.aca", campaign_store=str(tmp_path))
    manager.open(create=True, truncate=True)
    add_source_dataset(manager, "fluid")
    add_source_dataset(manager, "thermal")
    field = np.arange(12, dtype=np.float32).reshape(3, 4)
    manager.scalar_field_data(field, name="derived/density.000100.raw")
    sources = [
        {"dataset": "fluid", "variable": "pressure", "label": "pressure"},
        {"dataset": "thermal", "variable": "temperature", "label": "temperature"},
    ]

    with pytest.raises(ValueError, match="field_name is required"):
        manager.create_representation(
            name="derived/density/scalar",
            representation_format="SCALAR_FIELD",
            sources=sources,
        )

    repid = manager.create_representation(
        name="derived/density/scalar",
        representation_format="SCALAR_FIELD",
        sources=sources,
        field_name="density",
    )
    manager.append_representation_item(
        representation=repid,
        dataset="derived/density.000100.raw",
        logical_time=2.0,
        source_selections={
            "pressure": {"step": 100, "time": 2.0},
            "temperature": {"step": 50, "time": 1.98},
        },
    )

    representation = manager.info().representations[repid]
    assert representation.field_name == "density"
    assert representation.items[0].source_selections["pressure"]["step"] == 100
    assert representation.items[0].source_selections["temperature"]["step"] == 50
    manager.close()


def test_representation_rejects_incompatible_gaussian_items(tmp_path: Path):
    manager = Manager(archive="gaussian_mismatch.aca", campaign_store=str(tmp_path))
    manager.open(create=True, truncate=True)
    add_source_dataset(manager)
    manager.gaussian_splat_data(
        gaussian_frame(count=3),
        name="splats/pressure.000000.raw",
        metadata=normalized_splat_metadata(),
    )
    manager.gaussian_splat_data(
        gaussian_frame(count=4),
        name="splats/pressure.000001.raw",
        metadata=normalized_splat_metadata(),
    )
    repid = manager.create_representation(
        name="pressure/gaussian",
        representation_format="GAUSSIAN_SPLAT",
        sources=[{"dataset": "output", "variable": "pressure"}],
        temporal_interpolation="linear",
    )
    manager.append_representation_item(repid, "splats/pressure.000000.raw", source_step=0)
    with pytest.raises(ValueError, match="not compatible"):
        manager.append_representation_item(repid, "splats/pressure.000001.raw", source_step=1)
    manager.close()


def test_representation_items_require_explicit_source_steps(tmp_path: Path):
    manager = Manager(archive="explicit_steps.aca", campaign_store=str(tmp_path))
    manager.open(create=True, truncate=True)
    add_source_dataset(manager)
    manager.gaussian_splat_data(
        gaussian_frame(),
        name="splats/pressure.000000.raw",
        metadata=normalized_splat_metadata(),
    )
    repid = manager.create_representation(
        name="pressure/gaussian",
        representation_format="GAUSSIAN_SPLAT",
        sources=[{"dataset": "output", "variable": "pressure"}],
    )

    with pytest.raises(ValueError, match="explicit step"):
        manager.append_representation_item(repid, "splats/pressure.000000.raw")

    manager.close()


def test_multi_source_items_require_logical_time_when_source_times_differ(tmp_path: Path):
    manager = Manager(archive="asynchronous_sources.aca", campaign_store=str(tmp_path))
    manager.open(create=True, truncate=True)
    add_source_dataset(manager, "fluid")
    add_source_dataset(manager, "thermal")
    manager.scalar_field_data(np.zeros((2, 2), dtype=np.float32), name="derived/density.000000.raw")
    repid = manager.create_representation(
        name="derived/density/scalar",
        representation_format="SCALAR_FIELD",
        field_name="density",
        sources=[
            {"dataset": "fluid", "variable": "pressure"},
            {"dataset": "thermal", "variable": "temperature"},
        ],
    )

    with pytest.raises(ValueError, match="logical_time is required"):
        manager.append_representation_item(
            repid,
            "derived/density.000000.raw",
            source_selections={
                "pressure": {"step": 10, "time": 1.0},
                "temperature": {"step": 4, "time": 0.9},
            },
        )

    manager.close()


def test_representation_cli_creates_gaussian_sequence(tmp_path: Path):
    archive_name = "gaussian_cli.aca"
    splat_path = tmp_path / "splat.npz"
    second_splat_path = tmp_path / "splat_second.npz"
    metadata_path = tmp_path / "splat.json"
    manifest_path = tmp_path / "representation.json"
    append_manifest_path = tmp_path / "representation_append.json"
    np.savez(splat_path, **gaussian_frame())
    np.savez(second_splat_path, **gaussian_frame(shift=0.1))
    metadata_path.write_text(json.dumps(normalized_splat_metadata()), encoding="utf-8")
    manifest_path.write_text(
        json.dumps(
            {
                "name": "output/representations/pressure/gaussian",
                "format": "GAUSSIAN_SPLAT",
                "sources": [{"dataset": "output", "variable": "pressure"}],
                "temporal_interpolation": "linear",
                "items": [
                    {
                        "dataset": "splats/pressure.000010.raw",
                        "source_step": 10,
                        "source_time": 0.5,
                    }
                ],
                "metrics": [{"name": "rmse", "value": 0.02, "norm": "L2"}],
            }
        ),
        encoding="utf-8",
    )
    append_manifest_path.write_text(
        json.dumps(
            {
                "name": "output/representations/pressure/gaussian",
                "items": [
                    {
                        "dataset": "splats/pressure.000020.raw",
                        "source_step": 20,
                        "source_time": 1.5,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    manager_main(
        args=[
            "--campaign_store",
            str(tmp_path),
            "--truncate",
            archive_name,
            "data",
            str(data_dir / "onearray.h5"),
            "--name",
            "output",
            "gaussian-splat",
            str(splat_path),
            "--name",
            "splats/pressure.000010.raw",
            "--metadata-json",
            str(metadata_path),
            "representation",
            str(manifest_path),
        ],
        prog="hpc_campaign manager",
    )
    manager_main(
        args=[
            "--campaign_store",
            str(tmp_path),
            archive_name,
            "gaussian-splat",
            str(second_splat_path),
            "--name",
            "splats/pressure.000020.raw",
            "--metadata-json",
            str(metadata_path),
            "representation",
            str(append_manifest_path),
        ],
        prog="hpc_campaign manager",
    )

    manager = Manager(archive=archive_name, campaign_store=str(tmp_path))
    info_data = manager.info(list_replicas=True, list_files=True)
    representation = next(iter(info_data.representations.values()))
    assert representation.field_name == "pressure"
    assert representation.items[0].dataset_name == "splats/pressure.000010.raw"
    assert representation.items[0].source_selections["pressure"] == {"step": 10, "time": 0.5}
    assert representation.items[1].dataset_name == "splats/pressure.000020.raw"
    assert representation.items[1].source_selections["pressure"] == {"step": 20, "time": 1.5}
    assert representation.metrics[0].name == "rmse"
    assert DatasetType.GAUSSIAN_SPLAT.value == 6
    manager.close()
