#!/usr/bin/env python3
"""Build a small simulation-to-visualization provenance campaign.

The payloads are intentionally plain text placeholders. HPC Campaign records
where products live; the PROV graph records how the logical scientific
products are related. Real applications register their ADIOS, HDF5, image, or
other datasets before making the same provenance calls shown here.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from hpc_campaign import Manager, VariableSpec


def _register_placeholder(manager: Manager, payload_dir: Path, name: str) -> None:
    """Register one stand-in for a payload produced outside the campaign."""

    path = payload_dir / f"{name}.txt"
    path.write_text(f"placeholder payload for {name}\n", encoding="utf-8")
    manager.text(path, name=name, store=True)


def build_example(  # pylint: disable=too-many-locals
    campaign_store: Path,
    archive: str = "provenance-example.aca",
) -> Path:
    """Create and export the complete example, refusing to replace an ACA."""

    campaign_store.mkdir(parents=True, exist_ok=True)
    archive_path = campaign_store / archive
    if archive_path.exists():
        raise FileExistsError(f"refusing to replace existing campaign: {archive_path}")

    payload_dir = campaign_store / f"{Path(archive).stem}-payloads"
    payload_dir.mkdir(parents=True, exist_ok=True)

    manager = Manager(archive=archive, campaign_store=str(campaign_store))
    manager.open(create=True)
    try:
        for dataset in ("simulation-output", "reduced-products", "qoi-products", "visualizations"):
            _register_placeholder(manager, payload_dir, dataset)

        # A simulation Activity generates the source pressure variable.
        xgc = manager.add_agent("software", "XGC", version="example")
        run_plan = manager.add_plan(
            "XGC input configuration",
            location="inputs/run-001.yaml",
        )
        run = manager.add_run("run-001", agent=xgc, plan=run_plan)
        pressure = manager.add_variable(
            run=run,
            dataset="simulation-output",
            variable="P",
            definition="pressure",
            units="Pa",
            coordinate_system="boozer",
        )

        # Reduction creates a new logical variable; the source remains a
        # separate Entity and is linked through a qualified Derivation.
        mgard = manager.add_agent("software", "MGARD", version="example")
        reduced = manager.add_activity(
            "reduction",
            inputs={"source": pressure},
            outputs={
                "result": VariableSpec(
                    run=run,
                    dataset="reduced-products",
                    variable="P_reduced",
                    definition="pressure",
                    units="Pa",
                    coordinate_system="boozer",
                )
            },
            action_spec={"method": "mgard", "error_bound": 0.001},
            agent=mgard,
        )

        # A quantity of interest may be an array or a scalar. Here it is a
        # scalar total derived from the reduced pressure product.
        analysis = manager.add_agent("software", "QoI analysis", version="example")
        total_pressure = manager.add_activity(
            "quantity_of_interest",
            inputs={"pressure": reduced.outputs["result"]},
            outputs={
                "total": VariableSpec(
                    run=run,
                    dataset="qoi-products",
                    variable="total_pressure",
                    definition="total_pressure",
                    units="Pa",
                )
            },
            action_spec={"operation": "sum"},
            agent=analysis,
        )

        # Context such as a Fides data model is used by the visualization but
        # is deliberately not a scientific derivation parent.
        paraview = manager.add_agent("software", "ParaView", version="example")
        fides = manager.add_plan(
            "Fides data model",
            location="visualization/fides.json",
        )
        image = manager.add_activity(
            "visualization",
            inputs={
                "color": reduced.outputs["result"],
                "annotation": total_pressure.outputs["total"],
            },
            context={"data_model": fides},
            outputs={
                "image": VariableSpec(
                    run=run,
                    dataset="visualizations",
                    variable="pressure.png",
                    definition="pressure_visualization",
                )
            },
            action_spec={"representation": "isosurface", "colormap": "viridis"},
            agent=paraview,
            plan=fides,
        )

        export_path = campaign_store / "provenance-example.json"
        manager.export_prov("campaign-provenance", export_path)
        print(f"Campaign: {archive_path}")
        print(f"Final image logical variable: {image.outputs['image']}")
        print(f"PROV-JSON export: {export_path}")
        return export_path
    finally:
        manager.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "campaign_store",
        type=Path,
        help="directory in which to create the example ACA and payloads",
    )
    parser.add_argument("--archive", default="provenance-example.aca")
    args = parser.parse_args()
    build_example(args.campaign_store, args.archive)


if __name__ == "__main__":
    main()
