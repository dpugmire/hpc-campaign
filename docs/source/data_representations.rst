Unified variable representations
================================

HPC Campaign uses one logical-variable model for primary scientific variables
and alternate representations such as MGARD, ZFP, images, scalar fields, and
Gaussian data. Format-specific payloads do not have separate provenance
schemas.

Variable identity
-----------------

A logical variable is identified by its campaign dataset and variable name.
``Manager.add_variable`` returns a stable ``VariableRef`` that can be used as a
parent or preferred preview in later calls.

.. code-block:: python

  pressure = manager.add_variable(
      dataset="output.bp",
      variable="pressure",
  )

  pressure_mgard = manager.add_variable(
      dataset="output.bp",
      variable="pressure-mgard-1e-4",
      representation_of=pressure,
      representation_kind="mgard",
  )

``representation_kind`` is a producer-defined non-empty string.
``representation_metadata`` may contain any JSON-compatible value and is
stored without interpretation.

Representation graph
--------------------

``representation_of`` records immediate inputs. A single parent may be passed
directly. Multiple parents use unique labels:

.. code-block:: python

  overlay = manager.add_variable(
      dataset="visualizations",
      variable="pressure-temperature-overlay",
      chunks=overlay_payloads,
      representation_of={
          "color": pressure_mgard,
          "contours": temperature,
      },
      representation_kind="image",
      source_steps={
          "color": pressure_steps,
          "contours": temperature_steps,
      },
  )

Edges form an acyclic graph. A viewer can follow ``overlay`` to the MGARD
variable and then to the primary pressure variable. Established edges cannot
be changed by an append operation; use ``set_variable_relationships`` for an
explicit relationship update.

Chunks and source steps
-----------------------

Chunked variables reference ordered campaign payload datasets. A chunk may be
given as a dataset name or a ``ChunkSpec`` with an explicit index. When indices
are omitted, append assigns the next dense indices transactionally.

``source_steps`` maps each new chunk to steps of its immediate parents. It is a
simple sequence for one parent and a label mapping for multiple parents.
Physical time remains defined by the parent variable.

.. code-block:: python

  scalar = manager.add_variable(
      dataset="representations",
      variable="pressure-scalar-field",
      chunks=scalar_payload_names,
      representation_of=pressure,
      representation_kind="scalar_field",
      source_steps=range(0, 1000, 5),
  )

  manager.add_variable(
      dataset=scalar.dataset,
      variable=scalar.variable,
      chunks=new_payload_names,
      source_steps=new_source_steps,
      append=True,
  )

Append rejects duplicate payloads, duplicate indices, invalid step mappings,
and changes to parent edges. Validation and writes occur in one SQLite
transaction.

Image sequences
---------------

``Manager.add_image_sequence`` expands image paths or globs, natural-sorts glob
results, ingests the payloads, and creates an ``image`` logical variable using
the generic chunk path.

.. code-block:: python

  images = manager.add_image_sequence(
      dataset="visualizations",
      variable="pressure-volume",
      images="/vis/pressure/frame*.png",
      representation_of=pressure,
      source_steps=range(0, 1000, 5),
      representation_metadata={
          "visualization": "volume_rendering",
          "colormap": "viridis",
      },
      thumbnail=(256, 256),
  )

Frames in one sequence must have the same resolution, aspect ratio, encoding,
and pixel mode. Bytes, PIL images, and matplotlib figures require
``store=True`` because they do not have an external path. Per-image thumbnails
remain storage replicas; ``preferred_preview`` refers to another logical
variable used as the sequence-level preview.

Queries and deletion
--------------------

``Manager.info()`` returns an ``InfoResult`` with logical variables and graph
helpers:

.. code-block:: python

  info = manager.info()
  image_info = info.find_variable("visualizations", "pressure-volume")
  roots = info.primary_ancestors(image_info.reference)
  downstream = info.representations_of(pressure, representation_kind="image")
  paths = info.paths_to_roots(image_info.reference)

Deleting a referenced variable or payload is rejected. Use
``variable_delete_impact`` to inspect affected variables before an explicit
``delete_variable(..., cascade=True)`` operation.
