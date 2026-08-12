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
- adding a dataset and logical variable;
- adding an image sequence as a representation of that variable;
- adding text and archival-replica records; and
- displaying, listing, and removing the ACA.

Run it with:

```sh
python examples/example_campaign_api.py
```

The script creates `example_api.aca` in the repository root. At the end, it asks
whether to remove that archive.

## Command-line example

The command-line example uses a small generated ADIOS dataset and PNG sequence.
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
