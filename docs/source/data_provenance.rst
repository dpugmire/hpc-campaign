Data product provenance
=======================

HPC Campaign records provenance as a graph of data-product entities and the
activities that generated them. This mirrors the W3C PROV entity/activity
model: a logical variable is an entity, an activity uses input entities, and
the activity generates output entities.

The database does not store ``representation_kind``. Properties such as array
shape and temporal dimensionality already belong to self-describing ADIOS or
HDF5 data. A reduced pressure array is identified by its shared scientific
definition, ``pressure``, and by the ``reduction`` activity that generated it.
MGARD or ZFP are optional details of that activity's ``action_spec``.

Runs, definitions, and variables
--------------------------------

A ``VariableRef`` contains a run, campaign dataset, and variable name. Database
rows also have compact local integer IDs and stable UUIDs. UUIDs provide a
future federation point for parent campaigns without making local joins large.

Register source variables separately from derived products:

.. code-block:: python

  pressure = manager.add_variable(
      run="run-001",
      dataset="output.bp",
      variable="pressure",
      definition="pressure",
      primary=True,
  )

``definition`` is the scientific identity shared by related data products.
``primary=True`` creates an explicit per-run binding for the default pressure
entity. It is not inferred from names or the absence of a generating activity.
At most one entity can be primary for one definition in one run.

Definitions describe products observed in the campaign; they do not declare
which variables every run is expected to contain. A source or activity output
may introduce a definition that is absent from every other run. Definition
names are non-empty and matched exactly. Lowercase ``snake_case`` names such as
``pressure``, ``flux``, and ``total_flux`` are recommended, but not enforced.
The stored variable name remains unchanged, so different simulation codes can
map ``P``, ``press``, or ``pres`` to the shared definition ``pressure``.

The existing campaign dataset remains a storage object or logical namespace;
it is not owned by a run. Run qualification belongs to the logical variable,
whose public identity is ``(run, dataset, variable)``. Consequently, multiple
runs may reference the same physical input, aggregate file, or namespace. The
``run`` argument defaults to ``default`` when a campaign does not distinguish
runs.

Activities and actions
----------------------

Derived products are created with ``Manager.add_activity``. The initial action
vocabulary and categories are deliberately small:

======================  ================
Action                  Category
======================  ================
``reduction``           transformation
``projection``          transformation
``quantity_of_interest`` analysis
``visualization``       presentation
======================  ================

Unknown actions are rejected. New action names should be added through a
governed schema change rather than introduced independently by producers.
The action boundaries are:

* ``reduction`` produces a lower-fidelity or compressed form of an existing
  quantity;
* ``projection`` produces a spatial or mathematical projection;
* ``quantity_of_interest`` computes a scientifically meaningful quantity or
  diagnostic whose output may be a scalar, time series, array, or field; and
* ``visualization`` produces a presentation-oriented data product.

.. code-block:: python

  from hpc_campaign import VariableSpec

  reduced = manager.add_activity(
      action="reduction",
      inputs={"source": pressure},
      outputs={
          "result": VariableSpec(
              run="run-001",
              dataset="output.bp",
              variable="pressure-reduced",
              definition="pressure",
          )
      },
      action_spec={
          "method": "mgard",
          "error_bound": 1e-4,
      },
  ).outputs["result"]

A QoI can introduce a new scientific definition. Sequential calculations use
separate activities so that the intermediate product remains visible in the
provenance graph. For example, computing a flux field and then integrating it
to a scalar total flux creates two QoI activities:

.. code-block:: python

  flux = manager.add_activity(
      action="quantity_of_interest",
      inputs={"pressure": pressure, "temperature": temperature},
      outputs={
          "field": VariableSpec(
              run="run-001",
              dataset="products",
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
              run="run-001",
              dataset="diagnostics",
              variable="total-flux",
              definition="total_flux",
          )
      },
      action_spec={"operation": "integral", "domain": "boundary"},
  ).outputs["total"]

When one logical operation produces several values, one activity may instead
have several role-qualified outputs. The activity boundary follows the logical
operation, not the number or dimensionality of its outputs.

An activity may have multiple role-qualified inputs and outputs. An overlay
derived from pressure and temperature creates two ``activity_input`` rows:

.. code-block:: python

  overlay = manager.add_activity(
      action="visualization",
      inputs={
          "color": reduced,
          "contours": temperature,
      },
      outputs={
          "image": VariableSpec(
              run="run-001",
              dataset="visualizations",
              variable="pressure-temperature-overlay",
          )
      },
  ).outputs["image"]

Roles identify how an entity participates. They are unique within an activity;
there is no ``ordinal`` field in the initial model.

Action specifications and schema profiles
-----------------------------------------

``action_spec`` is optional JSON metadata describing how an action was
performed. Equal canonical specifications are content-hash deduplicated and
treated as immutable. No keys are universally required by the code.

A version-2 campaign schema may constrain keys for selected actions:

.. code-block:: yaml

  schema_version: 2

  activity_profiles:
    reduction:
      action_spec:
        required: []
        optional: [method, error_bound, error_bound_type]
        allow_additional: true

    visualization:
      action_spec:
        # Placeholder names demonstrate required-key validation.
        required: [required_key1, required_key2]
        optional: [optional_key1]
        allow_additional: false

Profiles do not add actions; the controlled vocabulary remains authoritative.
An omitted profile leaves that supported action's specification open. Within a
profile, required keys must exist, and ``allow_additional: false`` rejects keys
that are neither required nor optional. ``Manager.set_schema`` validates all
existing activities before storing a candidate schema, while
``Manager.validate_schema`` audits stored activities. See
``data/schema_examples/code_activities.yaml`` for a complete example.

Chunks and compact source-step mappings
---------------------------------------

Chunked outputs reference ordered campaign payload datasets. ``source_steps``
maps output chunks to steps of each immediate input. Regular mappings are
stored as one compact identity or strided row rather than one row per step.

Every fifth input step is represented as:

.. code-block:: python

  source_steps={"start": 0, "count": 200, "stride": 5}

For two inputs, provide one sequence or compact descriptor per role:

.. code-block:: python

  source_steps={
      "color": {"start": 0, "count": 200, "stride": 5},
      "contours": {"start": 0, "count": 200, "stride": 5},
  }

Irregular sequences such as ``[1, 4, 10]`` use an explicit compact JSON list.
For multiple outputs, add an outer mapping keyed by output role. Creating the
activity, its inputs, its outputs, chunks, and source mappings is one SQLite
transaction.

Appending a chunked output reuses its existing generating activity. The action,
roles, inputs, output role, definition, and immutable action specification
cannot change during append.

Image sequences
---------------

``Manager.add_image_sequence`` ingests image payloads and records a
``visualization`` activity through the same provenance path:

.. code-block:: python

  images = manager.add_image_sequence(
      run="run-001",
      dataset="visualizations",
      variable="pressure-volume",
      definition="pressure",
      images="rendered/pressure/frame*.png",
      inputs={"source": reduced},
      source_steps={"start": 0, "count": 200, "stride": 5},
      action_spec={"colormap": "viridis"},
      thumbnail=(256, 256),
  )

Frames in one sequence must have the same resolution, aspect ratio, encoding,
and pixel mode. Bytes, PIL images, and matplotlib figures require
``store=True`` because they do not have an external replica path.

Workflow queries and deletion
-----------------------------

The workflow is not saved as a second object. It is reconstructed by following
activity input and output IDs:

.. code-block:: text

  pressure -> reduction -> reduced pressure -> visualization -> image

.. code-block:: python

  info = manager.info()
  image_info = info.find_variable(
      "visualizations", "pressure-volume", run="run-001"
  )
  generating_activity = info.find_activity(image_info.generated_by)
  roots = info.root_sources(image_info.reference)
  downstream = info.derived_variables_from(pressure, action="visualization")
  paths = info.paths_to_root_sources(image_info.reference)

No transitive closure is stored, avoiding duplicated metadata as campaigns
grow. Deleting a referenced entity is rejected unless the caller explicitly
uses ``cascade=True`` after inspecting ``variable_delete_impact``.

Scale and future federation
---------------------------

The normalized model avoids copying variable metadata into every relationship,
deduplicates action specifications, and compresses regular step mappings. These
choices target campaigns with many runs and derived products while keeping the
high-level database compact.

Future work includes query pagination and summaries, parent-campaign manifests
that link child campaign URLs and UUIDs, optional partitions for very large
campaigns, and value-type rules for action specifications. A parent campaign
should provide discovery and routing; child campaign databases should retain
the detailed local provenance graph so only requested details need downloading.

Deferred design work
--------------------

Three related features are intentionally deferred from this schema:

* **Role profiles.** Input and output roles remain producer-defined, non-empty,
  and unique within an activity. Useful restrictions are likely specific to an
  operation rather than broad actions such as ``quantity_of_interest``.
* **Expected definitions per run.** Definitions catalog observed products only.
  A future run profile may declare required and optional definitions, define
  when a run is complete, and perform completeness validation on demand rather
  than rejecting a campaign while it is being assembled.
* **Nested campaigns.** Run, variable, and activity UUIDs provide stable future
  link targets, but parent/child manifests, URLs, lazy retrieval, cross-campaign
  references, query federation, and containment-cycle rules require a separate
  architecture effort.
