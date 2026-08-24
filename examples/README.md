# HPC Campaign examples

Run these examples from the repository root after installing the project. If you
use the repository virtual environment, activate it first:

```sh
source .venv/bin/activate
```

## Python API example

The Python example uses the checked-in `data/onearray.h5` dataset and creates
three small images in memory. It demonstrates:

- creating an ACA;
- adding a dataset and an explicitly primary logical variable;
- recording a visualization activity and compact source-step mapping;
- adding text and archival-replica records; and
- displaying, listing, and removing the ACA.

Run it with:

```sh
python examples/example_campaign_api.py
```

The script creates `example_api.aca` in the repository root. At the end, it asks
whether to remove that archive.

## Complete provenance workflow

The complete workflow example covers every supported action and reconstructs
the workflow solely from activity inputs and outputs:

```text
pressure -> reduction -> projection -> quantity_of_interest -> visualization
```

It also demonstrates multiple QoI outputs, a two-input visualization, compact
every-fifth-step mappings, primary-variable lookup, root-source traversal, and
action-filtered downstream queries. The QoI outputs introduce the canonical
definitions `pressure_mean` and `pressure_maximum`; definitions describe
observed scientific quantities and need not be present in every run.

Run it with:

```sh
python examples/example_provenance_workflow.py
```

The script creates `provenance_workflow.aca` in the repository root and leaves
it in place for inspection. Use `--campaign-store` and `--archive` to choose a
different destination.

## Command-line example

The command-line example uses a small generated ADIOS dataset and PNG sequence.
It registers the ADIOS ``T`` variable as a provenance source, then records the
image sequence as the output of a visualization activity. The regular step
mapping is stored as one compact descriptor.
Generate those inputs first:

```sh
python tests/heat2d/heatSimulation.py data/heat.bp 10 15 3 1
python tests/heat2d/heatPlot.py -i data/heat.bp -o data/T
```

Then run:

```sh
examples/example_campaign_cli.sh
```

The script creates `example_cli.aca` in the repository root and leaves it in
place for inspection. It also creates `log.archive`; the input-generation
commands create `data/heat.bp` and `data/T*.png`.
