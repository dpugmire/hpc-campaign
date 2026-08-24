"""Build and query a provenance workflow containing every supported action."""

from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image

from hpc_campaign import Manager, VariableRef, VariableSpec
from hpc_campaign.info import format_info

REPO_ROOT = Path(__file__).resolve().parents[1]
SAMPLE_DATASET = REPO_ROOT / "data" / "onearray.h5"


def build_workflow(
    campaign_store: str | Path,
    archive_name: str = "provenance_workflow.aca",
) -> tuple[Manager, dict[str, VariableRef]]:
    """Create pressure -> reduction -> projection -> QoI -> visualization."""
    manager = Manager(archive_name, campaign_store=str(campaign_store))
    manager.open(create=True, truncate=True)
    manager.data(SAMPLE_DATASET, name="output")

    # The HDF5 variable ``data`` is the source entity and the explicit primary
    # value for the scientific definition ``pressure`` in this run.
    pressure = manager.add_variable(
        run="run-001",
        dataset="output",
        variable="data",
        definition="pressure",
        primary=True,
    )

    # Method-specific details describe the action. MGARD is not a variable kind.
    reduction = manager.add_activity(
        action="reduction",
        inputs={"source": pressure},
        outputs={
            "result": VariableSpec(
                run="run-001",
                dataset="products",
                variable="pressure-reduced",
                definition="pressure",
            )
        },
        action_spec={"method": "mgard", "error_bound": 1e-4},
    )
    reduced = reduction.outputs["result"]

    projection = manager.add_activity(
        action="projection",
        inputs={"source": reduced},
        outputs={
            "result": VariableSpec(
                run="run-001",
                dataset="products",
                variable="pressure-midplane",
                definition="pressure",
            )
        },
        action_spec={"plane": "z", "coordinate": 0.5},
    )
    projected = projection.outputs["result"]

    # QoIs may be fields or scalars and may introduce new scientific
    # definitions. These two scalar diagnostics come from one logical operation,
    # so they are recorded as role-qualified outputs of one activity.
    qoi = manager.add_activity(
        action="quantity_of_interest",
        inputs={"source": projected},
        outputs={
            "mean": VariableSpec(
                run="run-001",
                dataset="qoi",
                variable="pressure-mean",
                definition="pressure_mean",
            ),
            "maximum": VariableSpec(
                run="run-001",
                dataset="qoi",
                variable="pressure-maximum",
                definition="pressure_maximum",
            ),
        },
        action_spec={"region": "midplane"},
    )

    # The generated frames stand in for renderer output. Both immediate inputs
    # get one compact mapping: frame N uses source step 5*N.
    frames = [Image.new("RGB", (128, 96), color=color) for color in ("midnightblue", "royalblue", "lightskyblue")]
    image = manager.add_image_sequence(
        run="run-001",
        dataset="visualizations",
        variable="pressure-summary",
        definition="pressure",
        images=frames,
        inputs={
            "color": projected,
            "annotation": qoi.outputs["maximum"],
        },
        source_steps={
            "color": {"start": 0, "count": 3, "stride": 5},
            "annotation": {"start": 0, "count": 3, "stride": 5},
        },
        action_spec={"colormap": "viridis"},
        store=True,
        thumbnail=(64, 64),
    )

    references = {
        "pressure": pressure,
        "reduced": reduced,
        "projected": projected,
        "mean": qoi.outputs["mean"],
        "maximum": qoi.outputs["maximum"],
        "image": image,
    }
    return manager, references


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaign-store", type=Path, default=REPO_ROOT)
    parser.add_argument("--archive", default="provenance_workflow.aca")
    args = parser.parse_args()

    manager, references = build_workflow(args.campaign_store, args.archive)
    info = manager.info()

    print(format_info(info))
    print("Workflow paths from the image to root source entities:")
    for path in info.paths_to_root_sources(references["image"]):
        print("  " + " <- ".join(f"{ref.run}/{ref.dataset}/{ref.variable}" for ref in path))

    visualizations = info.derived_variables_from(references["pressure"], action="visualization")
    print("Visualizations derived from pressure:")
    for product in visualizations:
        print(f"  {product.run}/{product.dataset}/{product.variable}")

    manager.close()
    print(f"Created {args.campaign_store / args.archive}")


if __name__ == "__main__":
    main()
