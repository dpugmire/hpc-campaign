import json
from hashlib import sha1
from pathlib import Path

import adios2
import numpy as np
import pytest
from PIL import Image

from hpc_campaign import ChunkSpec, Manager, VariableRef
from hpc_campaign.manager import main as manager_main


def add_payloads(manager: Manager, tmp_path: Path, names: list[str]) -> None:
    """Create embedded text payloads that can be referenced by variable chunks."""
    for name in names:
        path = tmp_path / f"{name}.bin"
        path.write_bytes(name.encode("utf-8"))
        manager.text(path, name=name, store=True)


def write_direct_variables(path: Path, steps: int = 3) -> None:
    """Write matching ADIOS variables whose step counts permit identity mapping."""
    with adios2.Stream(str(path), "w") as stream:
        for step in range(steps):
            stream.begin_step()
            base = np.arange(4, dtype=np.float32) + step
            stream.write("pressure", base, base.shape, [0], base.shape)
            for suffix in ("1e-5", "1e-4", "1e-3"):
                stream.write(f"pressure-mgard-{suffix}", base, base.shape, [0], base.shape)
            stream.end_step()


def test_primary_and_standalone_representation_variables(tmp_path: Path):
    """Primary and standalone variables retain their distinct graph semantics."""
    manager = Manager("variables.aca", campaign_store=str(tmp_path))
    manager.open(create=True)
    manager.data(Path(__file__).parents[1] / "data" / "onearray.h5", name="output")

    primary = manager.add_variable(dataset="output", variable="temp")
    standalone = manager.add_variable(
        dataset="output",
        variable="quality-mask",
        representation_kind="mask",
        representation_metadata={"meaning": ["invalid", "valid"]},
    )

    info = manager.info()
    assert primary == VariableRef("output", "temp")
    assert info.find_variable("output", "temp").parents == []
    standalone_info = info.find_variable(standalone.dataset, standalone.variable)
    assert standalone_info.representation_kind == "mask"
    assert standalone_info.representation_metadata == {"meaning": ["invalid", "valid"]}


def test_same_file_direct_representations_infer_verified_identity_steps(tmp_path: Path):
    """Direct ADIOS representations infer identity steps only after verification."""
    dataset_path = tmp_path / "output.bp"
    write_direct_variables(dataset_path)
    manager = Manager("direct.aca", campaign_store=str(tmp_path))
    manager.open(create=True)
    manager.data(dataset_path, name="output.bp")
    pressure = manager.add_variable(dataset="output.bp", variable="pressure")
    representations = [
        manager.add_variable(
            dataset="output.bp",
            variable=f"pressure-mgard-{suffix}",
            representation_of=pressure,
            representation_kind="mgard",
        )
        for suffix in ("1e-5", "1e-4", "1e-3")
    ]

    # Equal ADIOS step counts make a positional one-to-one mapping verifiable.
    info = manager.info()
    mgard_representations = info.representations_of(pressure, representation_kind="mgard")
    assert [item.reference for item in mgard_representations] == representations
    assert all(info.find_variable(item.dataset, item.variable).parents[0].identity_steps for item in representations)

    add_payloads(manager, tmp_path, ["unexpected-chunk"])
    with pytest.raises(ValueError, match="direct self-describing"):
        manager.add_variable(
            dataset=pressure.dataset,
            variable=pressure.variable,
            chunks=["unexpected-chunk"],
            append=True,
        )


def test_multilevel_and_multi_parent_graph_queries(tmp_path: Path):
    """Graph queries traverse multilevel and labeled multi-parent relationships."""
    manager = Manager("graph.aca", campaign_store=str(tmp_path))
    manager.open(create=True)
    add_payloads(manager, tmp_path, ["pressure-data", "temperature-data", "mgard-data", "overlay-image"])

    pressure = manager.add_variable(dataset="output", variable="pressure", chunks=["pressure-data"])
    temperature = manager.add_variable(dataset="output", variable="temperature", chunks=["temperature-data"])
    mgard = manager.add_variable(
        dataset="output",
        variable="pressure-mgard",
        chunks=["mgard-data"],
        representation_of=pressure,
        representation_kind="mgard",
        source_steps=[0],
    )
    overlay = manager.add_variable(
        dataset="visualizations",
        variable="overlay",
        chunks=["overlay-image"],
        representation_of={"color": mgard, "contours": temperature},
        representation_kind="image",
        source_steps={"color": [10], "contours": [5]},
    )

    # The overlay has two roots through paths of different depths.
    info = manager.info()
    overlay_info = info.find_variable("visualizations", "overlay")
    assert [(parent.label, parent.reference) for parent in overlay_info.parents] == [
        ("color", mgard),
        ("contours", temperature),
    ]
    assert overlay_info.chunks[0].source_steps == {"color": 10, "contours": 5}
    assert {root.reference for root in info.primary_ancestors(overlay)} == {pressure, temperature}
    assert [item.reference for item in info.representations_of(pressure)] == [mgard, overlay]
    assert [item.reference for item in info.representations_of(pressure, representation_kind="image")] == [overlay]
    assert info.paths_to_roots(overlay) == [
        [overlay, mgard, pressure],
        [overlay, temperature],
    ]


def test_transactional_append_assigns_dense_indices_and_rolls_back(tmp_path: Path):
    """Appending assigns dense indices and failed appends leave no partial rows."""
    manager = Manager("append.aca", campaign_store=str(tmp_path))
    manager.open(create=True)
    add_payloads(manager, tmp_path, ["source", "frame-0", "frame-1", "frame-2"])
    source = manager.add_variable(dataset="output", variable="pressure", chunks=["source"])
    sequence = manager.add_variable(
        dataset="visualizations",
        variable="pressure-images",
        chunks=["frame-0"],
        representation_of=source,
        representation_kind="image",
        source_steps=[0],
    )

    manager.add_variable(
        dataset=sequence.dataset,
        variable=sequence.variable,
        chunks=["frame-1", "frame-2"],
        source_steps=[5, 10],
        append=True,
    )
    chunks = manager.info().find_variable(sequence.dataset, sequence.variable).chunks
    assert [chunk.chunk_index for chunk in chunks] == [0, 1, 2]
    assert [chunk.source_steps["pressure"] for chunk in chunks] == [0, 5, 10]

    # Reusing a payload is rejected inside the same transaction as the append.
    with pytest.raises(ValueError, match="already present"):
        manager.add_variable(
            dataset=sequence.dataset,
            variable=sequence.variable,
            chunks=["frame-1"],
            source_steps=[15],
            append=True,
        )
    chunks_after_failure = manager.info().find_variable(sequence.dataset, sequence.variable).chunks
    assert [chunk.payload_dataset_name for chunk in chunks_after_failure] == ["frame-0", "frame-1", "frame-2"]


def test_chunk_indices_steps_and_metadata_are_validated(tmp_path: Path):
    """Invalid chunk indices, source-step counts, and metadata are atomic failures."""
    manager = Manager("validation.aca", campaign_store=str(tmp_path))
    manager.open(create=True)
    add_payloads(manager, tmp_path, ["source", "one", "two"])
    source = manager.add_variable(dataset="output", variable="pressure", chunks=["source"])

    with pytest.raises(ValueError, match="unique"):
        manager.add_variable(
            dataset="representations",
            variable="bad-indices",
            chunks=[ChunkSpec("one", 3), ChunkSpec("two", 3)],
            representation_of=source,
            source_steps=[0, 1],
        )
    with pytest.raises(ValueError, match="expected 2"):
        manager.add_variable(
            dataset="representations",
            variable="bad-steps",
            chunks=["one", "two"],
            representation_of=source,
            source_steps=[0],
        )
    with pytest.raises(ValueError, match="JSON-compatible"):
        manager.add_variable(
            dataset="representations",
            variable="bad-metadata",
            chunks=["one"],
            representation_metadata={"not-json": object()},
        )
    invalid_variables = {"bad-indices", "bad-steps", "bad-metadata"}
    assert all(item.variable not in invalid_variables for item in manager.info().variables.values())


def test_dangling_parents_and_cycles_are_rejected(tmp_path: Path):
    """Relationship validation rejects missing parents and graph cycles."""
    manager = Manager("cycles.aca", campaign_store=str(tmp_path))
    manager.open(create=True)
    add_payloads(manager, tmp_path, ["a-data", "b-data", "c-data"])
    a = manager.add_variable(dataset="graph", variable="a", chunks=["a-data"])

    with pytest.raises(LookupError, match="Logical variable not found"):
        manager.add_variable(
            dataset="graph",
            variable="dangling",
            chunks=["b-data"],
            representation_of=VariableRef("graph", "missing"),
        )

    b = manager.add_variable(dataset="graph", variable="b", chunks=["b-data"], representation_of=a)
    c = manager.add_variable(dataset="graph", variable="c", chunks=["c-data"], representation_of=b)
    with pytest.raises(ValueError, match="cycle"):
        manager.set_variable_relationships(a, c)
    assert manager.info().find_variable("graph", "a").parents == []


def test_preferred_preview_resolves_to_a_logical_variable(tmp_path: Path):
    """Preferred previews resolve to logical-variable references."""
    manager = Manager("preview.aca", campaign_store=str(tmp_path))
    manager.open(create=True)
    add_payloads(manager, tmp_path, ["preview-payload", "sequence-payload"])
    preview = manager.add_variable(
        dataset="visualizations",
        variable="preview",
        chunks=["preview-payload"],
        representation_kind="image",
    )
    sequence = manager.add_variable(
        dataset="visualizations",
        variable="sequence",
        chunks=["sequence-payload"],
        representation_kind="image",
        preferred_preview=preview,
    )
    assert manager.info().find_variable(sequence.dataset, sequence.variable).preferred_preview == preview


def test_image_sequence_globs_are_naturally_sorted(tmp_path: Path):
    """Globbed image frames use natural ordering and preserve image metadata."""
    manager = Manager("images.aca", campaign_store=str(tmp_path))
    manager.open(create=True)
    add_payloads(manager, tmp_path, ["source"])
    source = manager.add_variable(dataset="output", variable="pressure", chunks=["source"])
    image_dir = tmp_path / "frames"
    image_dir.mkdir()
    paths = [image_dir / "frame10.png", image_dir / "frame2.png", image_dir / "frame1.png"]
    for index, path in enumerate(paths):
        Image.new("RGB", (8, 6), color=(index, 0, 0)).save(path)

    sequence = manager.add_image_sequence(
        dataset="visualizations",
        variable="pressure-images",
        images=str(image_dir / "frame*.png"),
        representation_of=source,
        source_steps=[1, 2, 10],
        representation_metadata={"visualization": "slice"},
        thumbnail=(4, 4),
    )

    info = manager.info(list_replicas=True)
    sequence_info = info.find_variable(sequence.dataset, sequence.variable)
    # Payload names contain stable path hashes, allowing order checks without
    # depending on the rest of the internal naming convention.
    naturally_sorted = [image_dir / "frame1.png", image_dir / "frame2.png", image_dir / "frame10.png"]
    expected_tokens = [sha1(str(path.resolve()).encode("utf-8")).hexdigest()[:24] for path in naturally_sorted]
    assert [chunk.chunk_index for chunk in sequence_info.chunks] == [0, 1, 2]
    assert [chunk.source_steps["pressure"] for chunk in sequence_info.chunks] == [1, 2, 10]
    assert all(
        token in chunk.payload_dataset_name for token, chunk in zip(expected_tokens, sequence_info.chunks, strict=True)
    )
    assert sequence_info.representation_kind == "image"
    assert sequence_info.representation_metadata == {"visualization": "slice"}
    payloads = [info.datasets[chunk.payload_dataset_id] for chunk in sequence_info.chunks]
    assert all(len(payload.replicas) == 2 for payload in payloads)
    assert all(any(replica.flags.embedded for replica in payload.replicas.values()) for payload in payloads)


def test_image_sequence_validates_homogeneity_before_writing(tmp_path: Path):
    """Image sequences reject mixed geometry or encoding before mutating the ACA."""
    manager = Manager("image-validation.aca", campaign_store=str(tmp_path))
    manager.open(create=True)
    first = tmp_path / "first.png"
    different_size = tmp_path / "different-size.png"
    different_encoding = tmp_path / "different-encoding.jpg"
    Image.new("RGB", (8, 6), color="red").save(first)
    Image.new("RGB", (9, 6), color="green").save(different_size)
    Image.new("RGB", (8, 6), color="blue").save(different_encoding)

    with pytest.raises(ValueError, match="same resolution"):
        manager.add_image_sequence(
            dataset="visualizations",
            variable="bad-size",
            images=[first, different_size],
        )
    with pytest.raises(ValueError, match="same encoding"):
        manager.add_image_sequence(
            dataset="visualizations",
            variable="bad-encoding",
            images=[first, different_encoding],
        )
    # Batch validation happens before payloads or logical variables are written.
    assert manager.info().variables == {}

    sequence = manager.add_image_sequence(
        dataset="visualizations",
        variable="sequence",
        images=first,
    )
    with pytest.raises(ValueError, match="same resolution"):
        manager.add_image_sequence(
            dataset=sequence.dataset,
            variable=sequence.variable,
            images=different_size,
            append=True,
        )
    with pytest.raises(ValueError, match="same encoding"):
        manager.add_image_sequence(
            dataset=sequence.dataset,
            variable=sequence.variable,
            images=different_encoding,
            append=True,
        )
    assert len(manager.info().find_variable(sequence.dataset, sequence.variable).chunks) == 1


def test_in_memory_images_are_embedded_and_can_append(tmp_path: Path):
    """In-memory images require embedding and support homogeneous appends."""
    manager = Manager("memory-images.aca", campaign_store=str(tmp_path))
    manager.open(create=True)
    first = Image.new("RGBA", (7, 5), color="red")
    second = Image.new("RGBA", (7, 5), color="blue")

    with pytest.raises(ValueError, match="require store=True"):
        manager.add_image_sequence(
            dataset="visualizations",
            variable="memory-sequence",
            images=[first],
        )

    sequence = manager.add_image_sequence(
        dataset="visualizations",
        variable="memory-sequence",
        images=[first],
        store=True,
        thumbnail=(3, 3),
    )
    manager.add_image_sequence(
        dataset=sequence.dataset,
        variable=sequence.variable,
        images=[second],
        store=True,
        thumbnail=(3, 3),
        append=True,
    )

    info = manager.info(list_replicas=True)
    sequence_info = info.find_variable(sequence.dataset, sequence.variable)
    assert [chunk.chunk_index for chunk in sequence_info.chunks] == [0, 1]
    payloads = [info.datasets[chunk.payload_dataset_id] for chunk in sequence_info.chunks]
    assert all(any(replica.flags.embedded for replica in payload.replicas.values()) for payload in payloads)
    assert all(len(payload.replicas) == 2 for payload in payloads)


def test_image_sequence_preferred_preview(tmp_path: Path):
    """Image-sequence previews use the same logical-variable link as generic data."""
    manager = Manager("image-preview.aca", campaign_store=str(tmp_path))
    manager.open(create=True)
    preview_path = tmp_path / "preview.png"
    sequence_path = tmp_path / "sequence.png"
    Image.new("RGB", (8, 8), color="white").save(preview_path)
    Image.new("RGB", (8, 8), color="black").save(sequence_path)
    preview = manager.add_image_sequence(
        dataset="visualizations",
        variable="preview",
        images=preview_path,
    )
    sequence = manager.add_image_sequence(
        dataset="visualizations",
        variable="sequence",
        images=sequence_path,
        preferred_preview=preview,
    )
    assert manager.info().find_variable(sequence.dataset, sequence.variable).preferred_preview == preview


def test_variable_and_payload_deletion_are_guarded(tmp_path: Path):
    """Deletion reports dependencies, protects payloads, and cascades explicitly."""
    manager = Manager("deletion.aca", campaign_store=str(tmp_path))
    manager.open(create=True)
    add_payloads(manager, tmp_path, ["source-payload", "image-payload", "preview-payload"])
    source = manager.add_variable(dataset="output", variable="pressure", chunks=["source-payload"])
    preview = manager.add_variable(
        dataset="visualizations",
        variable="preview",
        chunks=["preview-payload"],
        representation_kind="image",
    )
    image = manager.add_variable(
        dataset="visualizations",
        variable="image",
        chunks=["image-payload"],
        representation_of=source,
        representation_kind="image",
        preferred_preview=preview,
    )

    impact = manager.variable_delete_impact(source)
    assert impact.dependent_representations == (image,)
    with pytest.raises(ValueError, match="still referenced"):
        manager.delete_variable(source)
    preview_impact = manager.variable_delete_impact(preview)
    assert preview_impact.preview_users == (image,)
    with pytest.raises(ValueError, match="still referenced"):
        manager.delete_variable(preview)
    with pytest.raises(ValueError, match="referenced by a logical variable"):
        manager.delete_name("source-payload")

    # Cascading from the source removes graph descendants but not an independent
    # variable used only as the descendant's preferred preview.
    manager.delete_variable(source, cascade=True)
    info = manager.info()
    assert all(item.reference not in {source, image} for item in info.variables.values())
    assert info.find_variable(preview.dataset, preview.variable).reference == preview


def test_variable_and_image_sequence_cli_manifests(tmp_path: Path):
    """CLI JSON manifests create generic variables and image sequences."""
    archive_name = "manifest.aca"
    manager = Manager(archive_name, campaign_store=str(tmp_path))
    manager.open(create=True)
    add_payloads(manager, tmp_path, ["source-payload"])
    manager.close()

    variable_manifest = tmp_path / "variable.json"
    variable_manifest.write_text(
        json.dumps({"dataset": "output", "variable": "pressure", "chunks": ["source-payload"]}),
        encoding="utf-8",
    )
    manager_main(
        args=["--campaign_store", str(tmp_path), archive_name, "variable", str(variable_manifest)],
        prog="hpc_campaign manager",
    )

    image_path = tmp_path / "frame.png"
    Image.new("RGB", (8, 8), color="purple").save(image_path)
    image_manifest = tmp_path / "image-sequence.json"
    image_manifest.write_text(
        json.dumps(
            {
                "dataset": "visualizations",
                "variable": "pressure-image",
                "images": [str(image_path)],
                "representation_of": {"dataset": "output", "variable": "pressure"},
                "source_steps": [0],
            }
        ),
        encoding="utf-8",
    )
    manager_main(
        args=["--campaign_store", str(tmp_path), archive_name, "image-sequence", str(image_manifest)],
        prog="hpc_campaign manager",
    )

    reopened = Manager(archive_name, campaign_store=str(tmp_path))
    info = reopened.info()
    image_info = info.find_variable("visualizations", "pressure-image")
    assert image_info.parents[0].reference == VariableRef("output", "pressure")


def test_image_ingestion_rolls_back_when_variable_validation_fails(tmp_path: Path):
    """A late relationship error rolls back image payload ingestion as one unit."""
    manager = Manager("image-rollback.aca", campaign_store=str(tmp_path))
    manager.open(create=True)
    image_path = tmp_path / "frame.png"
    Image.new("RGB", (8, 8), color="orange").save(image_path)
    before = {item.name for item in manager.info().datasets.values()}

    with pytest.raises(LookupError, match="not found or deleted"):
        manager.add_image_sequence(
            dataset="visualizations",
            variable="invalid",
            images=image_path,
            representation_of=VariableRef("missing", "source"),
            store=True,
        )

    assert {item.name for item in manager.info().datasets.values()} == before
    assert manager.info().variables == {}


def test_source_step_labels_and_append_parents_are_immutable(tmp_path: Path):
    """Source-step labels must match parents, which appends cannot redefine."""
    manager = Manager("parent-validation.aca", campaign_store=str(tmp_path))
    manager.open(create=True)
    add_payloads(manager, tmp_path, ["one", "two", "three", "frame", "new-frame"])
    one = manager.add_variable(dataset="output", variable="one", chunks=["one"])
    two = manager.add_variable(dataset="output", variable="two", chunks=["two"])
    three = manager.add_variable(dataset="output", variable="three", chunks=["three"])

    with pytest.raises(ValueError, match="labels do not match"):
        manager.add_variable(
            dataset="visualizations",
            variable="bad-labels",
            chunks=["frame"],
            representation_of={"color": one, "contours": two},
            source_steps={"color": [0], "wrong": [0]},
        )

    sequence = manager.add_variable(
        dataset="visualizations",
        variable="sequence",
        chunks=["frame"],
        representation_of={"color": one, "contours": two},
        source_steps={"color": [0], "contours": [0]},
    )
    with pytest.raises(ValueError, match="cannot change"):
        manager.add_variable(
            dataset=sequence.dataset,
            variable=sequence.variable,
            chunks=["new-frame"],
            representation_of={"color": one, "contours": three},
            source_steps={"color": [1], "contours": [1]},
            append=True,
        )
    assert len(manager.info().find_variable(sequence.dataset, sequence.variable).chunks) == 1

    # Existing temporal mappings cannot be silently reinterpreted for new parents.
    with pytest.raises(ValueError, match="requires replacement source_steps"):
        manager.set_variable_relationships(sequence, {"color": one, "contours": three})
    manager.set_variable_relationships(
        sequence,
        {"color": one, "contours": three},
        source_steps={"color": [2], "contours": [3]},
    )
    updated = manager.info().find_variable(sequence.dataset, sequence.variable)
    assert [(parent.label, parent.reference) for parent in updated.parents] == [
        ("color", one),
        ("contours", three),
    ]
    assert updated.chunks[0].source_steps == {"color": 2, "contours": 3}


def test_schema_version_and_specialized_public_apis_are_retired(tmp_path: Path):
    """New archives expose only the 0.7 unified schema and public API surface."""
    manager = Manager("schema.aca", campaign_store=str(tmp_path))
    manager.open(create=True)
    assert manager.info().archive.version == "0.7"
    assert int(manager.cur.execute("pragma foreign_keys").fetchone()[0]) == 1
    table_names = {
        str(row[0]) for row in manager.cur.execute("select name from sqlite_master where type = 'table'").fetchall()
    }
    assert {
        "logical_variable",
        "variable_representation_edge",
        "variable_chunk",
        "variable_chunk_source_step",
    }.issubset(table_names)
    assert not {
        "representation",
        "representation_source",
        "visualization_sequence",
        "visualization_variable",
    }.intersection(table_names)
    for method_name in (
        "image",
        "scalar_field_data",
        "gaussian_splat_data",
        "create_representation",
        "visualization_sequence",
    ):
        assert not hasattr(manager, method_name)
