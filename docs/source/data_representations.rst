Alternate Data Representations
==============================

Purpose and terminology
-----------------------

An HPC Campaign archive can retain the original simulation output and one or
more alternate representations of variables in that output. The original
ADIOS2 or HDF5 dataset is the ground truth. An alternate representation is a
different, usually smaller or more directly consumable, encoding of the same
scalar data.

``SCALAR_FIELD`` and ``GAUSSIAN_SPLAT`` are alternate scalar-data
representations. They are not images and they are not visualization types. A
consumer can render either representation with a selected colormap, compute
contours, sample it, or perform other field operations. In contrast, an
``IMAGE`` visualization sequence contains pixels produced by an earlier
rendering operation. Its colormap and other rendering choices are fixed.

This distinction is intentional:

* A source variable is identified by a campaign dataset and variable name.
* A representation records how its items correspond to steps of every source
  variable.
* A representation item is exactly one independently addressable output
  timestep.
* A visualization sequence is a fixed rendering and currently contains only
  ``IMAGE`` items.

The generic relationship is designed so future formats such as MGARD- or
ZFP-encoded fields can use the same provenance, time, and accuracy model.

Generic representation model
----------------------------

A representation has the following properties:

``name``
  A unique name in the campaign hierarchy.

``format``
  The homogeneous dataset format of all items. The formats currently supported
  by this relationship are ``SCALAR_FIELD`` and ``GAUSSIAN_SPLAT``.

``field_name``
  The scalar field produced by evaluating or decoding the representation. If
  there is exactly one source variable, its variable name is the default. A
  representation with multiple source variables must provide ``field_name``
  explicitly because there is no unambiguous default.

``sources``
  One or more ground-truth source variables. Each source contains a campaign
  dataset name, variable name, unique label within the representation, and
  optional metadata. A source label defaults to its variable name.

``temporal_interpolation``
  An explicit statement of how a consumer may evaluate times between stored
  items. ``none`` means that only stored items are defined. ``linear`` means
  the representation parameters may be linearly interpolated subject to the
  representation format's correspondence rules.

``parameter_correspondence``
  The identity rule for values or parameters across items. The default is
  ``grid-index`` for ``SCALAR_FIELD`` and ``stable-index`` for
  ``GAUSSIAN_SPLAT``. Stable Gaussian indices are a producer contract: Gaussian
  ``i`` in one keyframe must describe the same temporal track as Gaussian
  ``i`` in adjacent keyframes.

``metadata``
  Producer, provenance, fitting, or application-specific JSON metadata that
  applies to the representation as a whole.

Each item references one campaign dataset containing one complete timestep.
Item datasets must have the representation's format and must be structurally
compatible with the first item. Item order is dense by default even when the
source steps are sparse. For example, representation item orders 0, 1, and 2
may map to source steps 0, 10, and 25.

Every item records an explicit source selection for every source. At minimum,
each selection contains a non-negative ``step``. It may also contain a physical
``time`` and additional selection metadata, such as a spatial subset. The
item's ``logical_time`` is the time coordinate of the produced field. It
defaults to the common source time when all source selections provide the same
time. It must be supplied explicitly when multiple sources use different
physical times.

Multiple source variables
-------------------------

A derived field can depend on more than one source variable and those variables
can come from different campaign datasets. Source labels disambiguate the
per-item mappings:

.. code-block:: python

  repid = manager.create_representation(
      name="derived/density/scalar",
      representation_format="SCALAR_FIELD",
      field_name="density",
      sources=[
          {"dataset": "fluid", "variable": "pressure", "label": "pressure"},
          {"dataset": "thermal", "variable": "temperature", "label": "temperature"},
      ],
  )

  manager.append_representation_item(
      representation=repid,
      dataset="derived/density/scalar.000100.raw",
      logical_time=2.0,
      source_selections={
          "pressure": {"step": 100, "time": 2.0},
          "temperature": {"step": 50, "time": 1.98},
      },
  )

When ``source_selections`` is used, it must contain exactly the known source
labels. It cannot be mixed with the single-source convenience arguments
``source_step`` and ``source_time``.

Accuracy and quality metrics
----------------------------

Metrics are named numeric values. An item metric characterizes one stored
timestep, while an aggregate metric characterizes the complete representation.
Each metric can record units, a norm, whether it is relative, and arbitrary JSON
metadata describing its evaluation procedure.

.. code-block:: python

  manager.append_representation_item(
      representation=repid,
      dataset="splats/pressure.000010.raw",
      source_step=10,
      source_time=0.5,
      metrics=[
          {
              "name": "rmse",
              "value": 0.02,
              "units": "Pa",
              "norm": "L2",
              "metadata": {"reference": "continuous-field raster"},
          }
      ],
  )

  manager.add_representation_metric(
      representation=repid,
      name="mean_rmse",
      value=0.015,
      units="Pa",
      norm="L2",
  )

Metric names are not yet a controlled vocabulary. Producers should therefore
record enough metadata to identify the reference data, sampling domain,
weighting, masking, and aggregation procedure.

Gaussian-splat item format
---------------------------

The first Gaussian format is a two-dimensional anisotropic scalar basis. For
``N`` Gaussians, one item contains:

* ``centers`` with shape ``[N, 2]``;
* ``log_scales`` with shape ``[N, 2]``;
* ``angles`` with shape ``[N]``;
* ``amplitudes`` with shape ``[N]``; and
* one scalar ``bias``.

All components are finite little-endian float32 values. The embedded raw
payload uses structure-of-arrays order:

``centers, log_scales, angles, amplitudes, bias``

The payload length is ``(6*N + 1) * 4`` bytes. The item metadata records each
component's shape, byte offset, and byte length, so a reader does not have to
infer offsets from this prose.

Let ``d = p - center[i]``, ``c = cos(angle[i])``, and
``s = sin(angle[i])``. The stored model is evaluated as:

.. code-block:: text

  u = d.x*c + d.y*s
  v = -d.x*s + d.y*c
  sigma_x = exp(log_scale_x)
  sigma_y = exp(log_scale_y)

  field(p) = bias
           + sum_i amplitude[i] *
             exp(-0.5 * ((u/sigma_x)^2 + (v/sigma_y)^2))

The kernel is unnormalized: ``amplitude`` is the value contributed at the
Gaussian center, not an integral-normalized density weight. Angles are radians
and coordinates are ordered ``[x, y]``. These conventions are also stored in
item metadata as ``kernel``, ``scale_encoding``, ``angle_units``,
``coordinate_order``, ``rotation_convention``, and ``reconstruction``.

The v1 temporal contract requires the same ``N`` and the same stable Gaussian
ordering for all items in one representation. Structural validation rejects
different counts, layouts, data types, spaces, or transforms. The writer
cannot prove that a producer maintained semantic track identity; that is part
of the ``stable-index`` producer contract.

Coordinate and value spaces
---------------------------

Gaussian coordinates and reconstructed values each explicitly declare either
``physical`` or ``normalized`` space. A normalized space requires a non-empty,
typed JSON transform descriptor. A physical space may omit its transform.

Transform objects are deliberately extensible, but must include ``type``. The
portable affine convention is:

.. code-block:: json

  {
    "type": "affine",
    "physical_from_stored": {
      "scale": [4.4, -5.0],
      "offset": [0.2, 2.5]
    }
  }

For coordinates this means
``physical[j] = scale[j] * stored[j] + offset[j]``. A scalar ``scale`` may be
used for uniform scaling. The equivalent value transform uses scalar
``scale`` and ``offset`` and means
``physical_value = scale * stored_value + offset``.

Producer-specific transform types are allowed so that existing fitting
pipelines can retain their native transform metadata. Such a descriptor must
contain everything a reader for that transform type needs. Consumers should
reject an unknown transform type rather than silently treating normalized
parameters as physical values.

Writing Gaussian data with Python
---------------------------------

``gaussian_splat_data`` writes one timestep as a ``GAUSSIAN_SPLAT`` campaign
dataset. It does not by itself establish what ground-truth variable the item
represents; ``create_representation`` and ``append_representation_item`` record
that relationship.

.. code-block:: python

  manager.gaussian_splat_data(
      {
          "centers": centers,          # NumPy-compatible [N, 2]
          "log_scales": log_scales,    # [N, 2]
          "angles": angles,            # [N]
          "amplitudes": amplitudes,    # [N]
          "bias": bias,                # scalar or [1]
      },
      name="splats/pressure.000010.raw",
      coordinate_space="normalized",
      coordinate_transform={
          "type": "affine",
          "physical_from_stored": {
              "scale": [4.4, -5.0],
              "offset": [0.2, 2.5],
          },
      },
      value_space="normalized",
      value_transform={
          "type": "affine",
          "physical_from_stored": {"scale": 0.02388, "offset": 0.00184},
      },
  )

  repid = manager.create_representation(
      name="output/representations/pressure/gaussian",
      representation_format="GAUSSIAN_SPLAT",
      sources=[{"dataset": "output", "variable": "pressure"}],
      temporal_interpolation="linear",
  )

  manager.append_representation_item(
      representation=repid,
      dataset="splats/pressure.000010.raw",
      source_step=10,
      source_time=0.5,
  )

Calling ``append_representation_item`` again appends another independently
addressable timestep. ``item_order`` can be supplied explicitly when a producer
needs to reserve or reconstruct an order; otherwise it is assigned as
``max(item_order) + 1``.

Command-line manifests
----------------------

The ``gaussian-splat`` command accepts one ``.npz`` file containing the five
arrays. Spaces and transforms can be passed in a JSON metadata file:

.. code-block:: bash

  hpc_campaign manager run.aca gaussian-splat pressure.000010.npz \
    --name splats/pressure.000010.raw \
    --metadata-json pressure-space.json

A representation manifest can create the generic relationship and append its
items in the same manager invocation:

.. code-block:: json

  {
    "name": "output/representations/pressure/gaussian",
    "format": "GAUSSIAN_SPLAT",
    "sources": [
      {"dataset": "output", "variable": "pressure"}
    ],
    "temporal_interpolation": "linear",
    "items": [
      {
        "dataset": "splats/pressure.000010.raw",
        "source_step": 10,
        "source_time": 0.5,
        "metrics": [
          {"name": "rmse", "value": 0.02, "units": "Pa", "norm": "L2"}
        ]
      }
    ],
    "metrics": [
      {"name": "mean_rmse", "value": 0.015, "units": "Pa", "norm": "L2"}
    ]
  }

.. code-block:: bash

  hpc_campaign manager run.aca representation pressure-representation.json

An append-only manifest can omit ``format`` and ``sources`` and contain only
the existing representation ``name`` and new ``items``. ``--replace`` requires
a complete creation manifest and replaces the relationship, source mappings,
item associations, and metrics. It does not delete the item datasets.

Archive schema
--------------

The generic relationship is stored in five additive SQLite tables:

``representation``
  Name, output field, item format, temporal interpolation, parameter
  correspondence, and representation-level metadata.

``representation_source``
  Ground-truth dataset/variable pairs and their unique labels.

``representation_item``
  Ordered item datasets, logical times, and item-level metadata.

``representation_item_source``
  The explicit source selection for every item/source pair.

``representation_metric``
  Per-item or aggregate metrics.

Format-specific metadata remains attached to its dataset in ``scalar_field`` or
``gaussian_splat``. The generic tables describe relationships and time; they do
not duplicate the binary format contract.

``hpc_campaign manager <archive> info`` reports a ``Data Representations``
section with sources, item ordering, logical times, source selections, and
metrics. The structured Python ``InfoResult`` exposes the same information in
its ``representations`` member.

Current limitations and future decisions
----------------------------------------

The following issues are intentionally documented rather than hidden behind a
prematurely general v1 format:

* **Image migration.** Visualization sequences remain a separate legacy
  relationship for fixed ``IMAGE`` products. A future schema may express an
  image sequence as another generic representation while retaining rendering
  roles, thumbnails, and visualization-specific metadata.
* **Additional codecs.** MGARD, ZFP, and similar formats need format-specific
  metadata, codec/version negotiation, and decode capability discovery. They
  should reuse the generic source, item, time, and metric tables.
* **Higher-dimensional and multi-component fields.** The first Gaussian model
  is 2D and scalar. Three-dimensional Gaussians, vector/tensor fields,
  multi-channel values, and uncertainty fields need explicit new model names
  and payload contracts rather than reinterpretation of v1.
* **Changing Gaussian topology.** V1 linear interpolation requires a fixed
  count and stable ordering. Variable counts, split/merge events, pruning, and
  unmatched keyframes need an explicit correspondence map or a different
  temporal model.
* **Angle interpolation.** Direct linear interpolation can cross a periodic
  angle boundary or swap equivalent ellipse axes. Producers currently need to
  unwrap angles and preserve axis conventions. A future interpolation contract
  should define canonical angles and shortest-path interpolation.
* **Temporal models.** Only declarative ``none`` and producer-asserted
  ``linear`` behavior are expected initially. Cubic tracks, motion models,
  acceleration-aware interpolation, and per-interval interpolation modes need
  a versioned contract.
* **Time semantics.** ``logical_time`` and source ``time`` are numeric values,
  but units, epochs, calendars, simulation-cycle identifiers, and tolerances
  are not standardized. Those must be explicit before unrelated producers and
  consumers can safely compare time coordinates.
* **Asynchronous sources.** Multiple sources can select different steps and
  times, but resampling rules are not standardized. A producer must provide an
  item ``logical_time`` when source times differ and should document how inputs
  were aligned.
* **Selections.** Source selections can carry spatial subset metadata, but no
  common vocabulary yet defines indices, bounding boxes, mesh regions,
  components, or ghost zones.
* **Transform registry.** Typed transform descriptors are stored, but only the
  affine convention is described here. Coordinate reference systems, units,
  curvilinear meshes, non-affine maps, axis order, handedness, and display
  orientation need registered transform types and viewer capability checks.
* **Metric vocabulary.** RMSE, PSNR, relative norms, contour displacement,
  calibration, and uncertainty coverage need shared definitions. Metrics
  should eventually identify the ground-truth evaluator, sampling resolution,
  mask, quadrature weights, reduction over time, and units.
* **Uncertainty.** An uncertainty field can eventually be another linked
  representation or a named component, but correlation, calibration, and
  probabilistic contour semantics need an explicit model.
* **Storage efficiency.** V1 embeds one raw float32 payload per item.
  Compression, quantization, chunking, checksums, GPU-friendly alignment,
  byte-range access, and consolidated multi-item payloads require format and
  random-access design work.
* **Representation selection.** When multiple accuracy/size tradeoffs exist,
  clients need a policy for selecting by metric, storage size, decode cost,
  device support, requested operation, and acceptable error.
* **Exact versus approximate semantics.** The relationship does not currently
  label losslessness, bounded error, or fitness for a requested operation.
  Those concepts should be machine-readable rather than inferred from a format
  name or the presence of a metric.
* **Lifecycle and provenance.** Deleting a source or item dataset can currently
  leave a historical relationship that readers must detect. Strong foreign-key
  policy, immutable provenance, derivation DAGs, cycle detection, and cascading
  versus preserving relationships need deliberate archive-wide rules.
* **Concurrent append.** Item creation is committed transactionally by one
  manager connection, but concurrent producers, reservations, recovery from
  partial writes, and representation finalization are not a defined protocol.
* **Integrity and confidentiality.** Future external or chunked payloads need
  explicit checksums, authenticated encryption behavior, key handling, and
  clear reporting of missing, truncated, or corrupt representation items.
* **Format evolution.** These tables are introduced without changing the ACA
  version, as required for this pre-beta development phase. Before stable
  interoperability, the project needs explicit capability/schema discovery,
  migrations, and rules for readers that encounter unknown representation or
  transform formats.
* **Viewer behavior.** The campaign viewer does not yet decode or render
  ``GAUSSIAN_SPLAT``. A viewer must validate the format metadata, transform
  types, interpolation contract, and payload bounds before evaluating a field;
  unsupported capabilities should fail visibly rather than silently falling
  back to image semantics.
