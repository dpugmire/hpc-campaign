# Activity-Based Data-Product Provenance

## Summary

This change replaces the experimental direct `derived_from` edge model with a
W3C PROV-inspired entity/activity graph. Logical variables are data-product
entities. Controlled activities use role-qualified input entities and generate
role-qualified output entities. Following those input and output IDs reconstructs
the workflow without storing a separate workflow record or transitive closure.

The initial model deliberately omits agents, execution timing, status, data-kind
metadata, and a generic derivation-kind layer. Those can be added when concrete
requirements justify their storage cost.

## Public API

Source products are registered with `Manager.add_variable`:

```python
pressure = manager.add_variable(
    run="run-001",
    dataset="output.bp",
    variable="pressure",
    definition="pressure",
    primary=True,
)
```

Derived products are created atomically with `Manager.add_activity`:

```python
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
    action_spec={"method": "mgard", "error_bound": 1e-4},
).outputs["result"]
```

`Manager.add_image_sequence` is a convenience wrapper that ingests image
payloads and records a `visualization` activity through the same path.

This is a breaking replacement of an unreleased API. The removed fields and
methods are `representation_kind`, `representation_metadata`, `derived_from`
on variable writes, and `set_variable_relationships`. No compatibility aliases
or migration for databases created by the earlier PR implementation are
provided.

## Runs and Scientific Definitions

The existing campaign `dataset` remains a storage object or logical namespace;
it is not owned by a run. A logical variable is qualified by
`(run, dataset, variable)`, so several runs may reference the same physical
input, aggregate file, or namespace. The public `run` argument is optional and
defaults to `default`; `campaign_run` supplies compact local IDs and stable
UUIDs internally.

`definition` is an observed scientific identity, not a declaration that every
run must contain that variable. It defaults to the stored variable name. Names
are non-empty and matched exactly; lowercase `snake_case` values are the
recommended convention rather than an enforced rule. Producers explicitly map
code-specific storage names to a canonical definition:

```python
manager.add_variable(run="code-a", dataset="output", variable="P", definition="pressure")
manager.add_variable(run="code-b", dataset="output", variable="press", definition="pressure")
```

Source variables and activity outputs may both introduce new definitions. For
example, a QoI activity may generate a `flux` definition and a later QoI may
consume that field to generate a scalar `total_flux` definition. The activity
generates the logical-variable entity; `variable_definition` is the normalized
catalog entry describing what that entity means.

## Controlled Vocabulary

The accepted actions are fixed centrally:

| Action | Category |
| --- | --- |
| `reduction` | `transformation` |
| `projection` | `transformation` |
| `quantity_of_interest` | `analysis` |
| `visualization` | `presentation` |

Unknown actions, including `feature_detection`, are rejected. A new action
requires an intentional vocabulary change. Roles are non-empty and unique
inside an activity. Multiple inputs create multiple `activity_input` rows; no
`ordinal` is stored initially.

The action boundaries are:

- `reduction`: lower-fidelity or compressed form of an existing quantity;
- `projection`: spatial or mathematical projection;
- `quantity_of_interest`: scientifically meaningful computed quantity or
  diagnostic, including scalar, time-series, array, and field outputs; and
- `visualization`: presentation-oriented data product.

One logical operation with several outputs is one activity. Sequential
operations are separate activities connected through the intermediate output.
For example, computing a flux field and then integrating total flux produces
two `quantity_of_interest` activities, preserving the field as an explicit
intermediate entity in the workflow.

## SQLite Schema

Local integer keys keep joins and indexes compact. Runs, variables, and
activities also receive UUIDs so a future parent campaign can link stable
identities without replacing local keys.

The provenance tables are:

- `campaign_run`: stable run identities;
- `variable_definition`: shared scientific names such as `pressure`;
- `logical_variable`: data-product entities;
- `primary_variable`: one explicit primary entity per `(run, definition)`;
- `activity_category` and `activity_kind`: the controlled vocabulary;
- `action_spec`: immutable, canonical JSON specifications deduplicated by hash;
- `activity`: one recorded action;
- `activity_input`: role-qualified entities used by the activity;
- `activity_output`: role-qualified entities generated by the activity;
- `variable_chunk`: ordered payload datasets; and
- `activity_input_step_mapping`: compact temporal mappings.

### Complete storage data dictionary

`Required?` means that every persisted row in that table contains a value for
the property. This includes primary keys and UUIDs that the implementation
generates rather than requiring from the caller. A nullable or
encoding-dependent property is marked `No`. `New?` compares this activity-based
schema with the previous representation-based schema at commit `87e7ab2`. A
replacement table is new even when it serves a related purpose.

| Object | Property | Type or reference | Required? | New? | Purpose or constraint |
| --- | --- | --- | --- | --- | --- |
| `campaign_run` | `runid` | Integer primary key | Yes | Yes | Compact local identity for a run. |
| `campaign_run` | `uuid` | Text, unique | Yes | Yes | Stable identity that a parent campaign can reference. |
| `campaign_run` | `name` | Text, unique | Yes | Yes | User-facing run name, such as `run-001`. |
| `variable_definition` | `definitionid` | Integer primary key | Yes | Yes | Compact local identity for a scientific definition. |
| `variable_definition` | `name` | Text, unique | Yes | Yes | Shared scientific name, such as `pressure`, independent of storage name. |
| `logical_variable` | `variableid` | Integer primary key | Yes | No | Existing compact local identity for a logical variable. |
| `logical_variable` | `uuid` | Text, unique | Yes | Yes | Stable data-product identity for cross-campaign references. |
| `logical_variable` | `runid` | `campaign_run.runid` | Yes | Yes | Qualifies the entity by run. |
| `logical_variable` | `definitionid` | `variable_definition.definitionid` | Yes | Yes | Connects the stored entity to its scientific definition. |
| `logical_variable` | `datasetid` | Existing `dataset` row | Yes | No | Identifies the dataset or logical namespace containing the variable. |
| `logical_variable` | `name` | Text | Yes | No | Names the variable within its run and dataset. |
| `logical_variable` | `preferred_preview_id` | `logical_variable.variableid` | No | No | Selects another data product as the preferred preview. |
| `primary_variable` | `runid` | `campaign_run.runid` | Yes | Yes | First part of the one-primary-per-run-and-definition key. |
| `primary_variable` | `definitionid` | `variable_definition.definitionid` | Yes | Yes | Second part of the one-primary-per-run-and-definition key. |
| `primary_variable` | `variableid` | `logical_variable.variableid`, unique | Yes | Yes | Selects the default entity for the run and definition. The referenced entity must have the same run and definition. |
| `activity_category` | `categoryid` | Integer primary key | Yes | Yes | Compact identity for a controlled activity category. |
| `activity_category` | `name` | Text, unique | Yes | Yes | Controlled category: `transformation`, `analysis`, or `presentation`. |
| `activity_kind` | `kindid` | Integer primary key | Yes | Yes | Compact identity for a controlled action. |
| `activity_kind` | `categoryid` | `activity_category.categoryid` | Yes | Yes | Assigns each action to exactly one category. |
| `activity_kind` | `name` | Text, unique | Yes | Yes | Controlled action: `reduction`, `projection`, `quantity_of_interest`, or `visualization`. |
| `action_spec` | `specid` | Integer primary key | Yes | Yes | Compact identity for an immutable action specification. |
| `action_spec` | `kindid` | `activity_kind.kindid` | Yes | Yes | Limits a specification to one action kind. |
| `action_spec` | `content_hash` | SHA-256 text | Yes | Yes | Deduplicates canonical specifications within an action kind. |
| `action_spec` | `metadata` | Canonical JSON text | Yes | Yes | Stores optional producer details when an `action_spec` row exists. |
| `activity` | `activityid` | Integer primary key | Yes | Yes | Compact local identity for a recorded action. |
| `activity` | `uuid` | Text, unique | Yes | Yes | Stable provenance-activity identity. |
| `activity` | `runid` | `campaign_run.runid` | No | Yes | Identifies the run when every input and output is in one run; cross-run activities leave it null. |
| `activity` | `kindid` | `activity_kind.kindid` | Yes | Yes | Selects the controlled action performed. |
| `activity` | `specid` | Matching `action_spec.specid` | No | Yes | References optional, immutable producer details for the selected action kind. |
| `activity_input` | `inputid` | Integer primary key | Yes | Yes | Compact identity for one activity input edge. |
| `activity_input` | `activityid` | `activity.activityid` | Yes | Yes | Identifies the activity that used the entity. |
| `activity_input` | `variableid` | `logical_variable.variableid` | Yes | Yes | Identifies an input entity. |
| `activity_input` | `role` | Non-empty text | Yes | Yes | Describes how the input participates, such as `source`, `color`, or `contours`; unique within the activity. |
| `activity_output` | `outputid` | Integer primary key | Yes | Yes | Compact identity for one activity output edge. |
| `activity_output` | `activityid` | `activity.activityid` | Yes | Yes | Identifies the activity that generated the entity. |
| `activity_output` | `variableid` | `logical_variable.variableid`, unique | Yes | Yes | Identifies an output entity and ensures it has at most one generating activity. |
| `activity_output` | `role` | Non-empty text | Yes | Yes | Describes the output, such as `result`, `mean`, or `maximum`; unique within the activity. |
| `variable_chunk` | `chunkid` | Integer primary key | Yes | No | Existing compact identity for one ordered payload reference. |
| `variable_chunk` | `variableid` | `logical_variable.variableid` | Yes | No | Identifies the logical variable that owns the chunk. |
| `variable_chunk` | `chunk_index` | Non-negative integer | Yes | No | Defines deterministic chunk order within the variable. |
| `variable_chunk` | `payload_datasetid` | Existing `dataset` row | Yes | No | References the stored or externally replicated payload. |
| `activity_input_step_mapping` | `mappingid` | Integer primary key | Yes | Yes | Compact identity for one input/output mapping batch. |
| `activity_input_step_mapping` | `inputid` | `activity_input.inputid` | Yes | Yes | Selects the role-qualified activity input whose steps are mapped. |
| `activity_input_step_mapping` | `output_variableid` | `logical_variable.variableid` | Yes | Yes | Selects the output sequence described by the mapping. |
| `activity_input_step_mapping` | `output_start` | Non-negative integer | Yes | Yes | First output chunk index in the mapping batch. |
| `activity_input_step_mapping` | `count` | Positive integer | Yes | Yes | Number of consecutive output chunks covered by the row. |
| `activity_input_step_mapping` | `encoding` | Controlled text | Yes | Yes | Selects `identity`, `stride`, or `explicit` encoding. |
| `activity_input_step_mapping` | `source_start` | Integer | No | Yes | First source step for `identity` and `stride`; absent for `explicit`. |
| `activity_input_step_mapping` | `stride` | Positive integer | No | Yes | Source-step increment for `identity` and `stride`; absent for `explicit`. |
| `activity_input_step_mapping` | `explicit_steps` | JSON integer array | No | Yes | Irregular source steps for `explicit`; absent for `identity` and `stride`. |

### Public creation API requirements

This companion table uses `Required?` to mean that the caller must supply the
property. Defaults and internally generated values are therefore `No`, even
when the corresponding stored column is required above.

| API | Property | Required? | New? | Default or behavior |
| --- | --- | --- | --- | --- |
| `add_variable` | `dataset` | Yes | No | Existing dataset or logical namespace. |
| `add_variable` | `variable` | Yes | No | Variable name within the dataset. |
| `add_variable` | `run` | No | Yes | Defaults to `default`. |
| `add_variable` | `definition` | No | Yes | Defaults to the variable name. |
| `add_variable` | `chunks` | No | No | Omitted for variables stored directly in self-describing datasets. |
| `add_variable` | `primary` | No | Yes | Defaults to `False`. |
| `add_variable` | `preferred_preview` | No | No | Defaults to no preferred preview. |
| `add_variable` | `append` | No | No | Defaults to `False`. |
| `add_activity` | `action` | Yes | Yes | Must be one of the controlled action names. |
| `add_activity` | `inputs` | Yes | Yes | Non-empty mapping from input role to `VariableRef`. |
| `add_activity` | each input role | Yes | Yes | Each role is non-empty and unique within the activity. |
| `add_activity` | `outputs` | Yes | Yes | Non-empty mapping from output role to `VariableSpec`. |
| `add_activity` | each output role | Yes | Yes | Each role is non-empty and unique within the activity. |
| `add_activity` | `action_spec` | No | Yes | Defaults to no producer-specific specification. |
| `add_activity` | `source_steps` | No | No | Omitted when temporal correspondence is unknown or unnecessary. |
| activity output | `dataset` | Yes | No | Output dataset or logical namespace. |
| activity output | `variable` | Yes | No | Output variable name. |
| activity output | `run` | No | Yes | Defaults to `default`. |
| activity output | `definition` | No | Yes | Defaults to the output variable name. |
| activity output | `chunks` | No | No | Required only when the output has separately referenced payload chunks. |
| activity output | `primary` | No | Yes | Defaults to `False`. |
| activity output | `preferred_preview` | No | No | Defaults to no preferred preview. |
| activity output | `append` | No | No | Defaults to `False`. |
| `add_image_sequence` | `dataset` | Yes | No | Output dataset or logical namespace. |
| `add_image_sequence` | `variable` | Yes | No | Output image-sequence name. |
| `add_image_sequence` | `images` | Yes | No | Must resolve to at least one image. |
| `add_image_sequence` | `inputs` | Yes | Yes | Non-empty mapping from visualization role to source entity. |
| `add_image_sequence` | `run` | No | Yes | Defaults to `default`. |
| `add_image_sequence` | `definition` | No | Yes | Defaults to the image-sequence variable name. |
| `add_image_sequence` | `source_steps` | No | No | Optional compact or explicit mapping from frames to input steps. |
| `add_image_sequence` | `action_spec` | No | Yes | Optional visualization details such as colormap. |
| `add_image_sequence` | `store` | No | No | Defaults to `False`; in-memory images require `True`. |
| `add_image_sequence` | `thumbnail` | No | No | Defaults to no generated thumbnail. |
| `add_image_sequence` | `preferred_preview` | No | No | Defaults to no preferred preview. |
| `add_image_sequence` | `append` | No | No | Defaults to `False`. |
| `set_primary_variable` | `variable` | Yes | Yes | Existing entity to bind as the primary value for its run and definition. |

An output entity can have at most one generating activity. One activity can
have multiple inputs and outputs. Activities that span runs have a null local
`runid` rather than claiming incorrect ownership.

## Compact Source-Step Mappings

Step mappings are stored per activity input and output variable. Regular cases
use one row:

```python
# Output chunks 0..199 use source steps 0, 5, 10, ..., 995.
source_steps={"start": 0, "count": 200, "stride": 5}
```

The storage encoding is `identity`, `stride`, or `explicit`. An irregular list
is retained losslessly as compact JSON. Appending creates one mapping row per
input for the new batch rather than one row per output step.

## Schema Validation

Schema version 2 supports optional `activity_profiles`. A profile constrains
the keys of an action's optional `action_spec`:

```yaml
activity_profiles:
  visualization:
    action_spec:
      required: [required_key1, required_key2]
      optional: [optional_key1]
      allow_additional: false
```

Profiles cannot introduce actions. An omitted profile leaves that supported
action's specification unconstrained. The validator checks single and multiple
required keys, optional keys, `allow_additional` true and false, malformed
profile structures, and existing activity rows before replacing a campaign
schema. `Manager.validate_schema()` reports checked and specified activity
counts.

## Query and Deletion Behavior

`Manager.info()` exposes runs, variables, activities, inputs, outputs, chunks,
and compact mappings. Query helpers find primary values, root sources,
downstream variables, workflow paths, and activities by UUID. An optional
action filter can select downstream products generated by actions such as
`visualization`.

Deletion traverses activity inputs and outputs. A referenced variable is
protected by default; `cascade=True` explicitly removes downstream products
and activities after `variable_delete_impact()` reports the affected entities.

## Tests and Examples

The tests cover:

- all action names and categories, plus unknown-action rejection;
- per-run primary bindings and shared physical datasets across runs;
- observed definitions, code-specific stored names, and definitions absent from
  some runs;
- sequential field and scalar QoI activities that introduce new definitions;
- single-input, multi-input, and multi-output activities;
- reconstruction of `pressure -> reduction -> visualization`;
- action-spec JSON validation and content deduplication;
- identity, every-fifth-step, and irregular compact mappings;
- append invariants and transactional rollback;
- cross-run activities, graph deletion, and image ingestion;
- CLI variable, activity, and image manifest shapes; and
- activity-profile required, optional, and additional-key policies.

The Python, shell, API, CLI, schema, and provenance examples use the new names
and include comments explaining roles, primary bindings, actions, and compact
step mappings.

## Scale Properties

The design avoids several sources of metadata growth:

- file-native shape and temporal metadata are not copied into provenance rows;
- action specifications are content-deduplicated;
- roles and relationships are normalized instead of embedded repeatedly;
- regular temporal mappings use one strided row per batch; and
- workflows and transitive ancestry are computed from immediate edges.

These choices support many runs and derived products without requiring the
campaign database to contain copies of every file's self-describing metadata.

## Future Work

1. Add query pagination, result limits, and per-definition/action summaries so
   broad queries do not materialize every matching entity by default.
2. Add optional operation-specific role profiles if concrete producers need
   stronger constraints than the current non-empty, per-activity unique roles.
   Broad action-level role restrictions are deferred because QoI operations do
   not share a single meaningful set of input and output roles.
3. Add run-completeness profiles if campaigns need to declare definitions that
   are required or optional in each run. This requires completion semantics,
   run-type handling, and on-demand validation so incomplete campaigns can
   still be assembled incrementally.
4. Define parent-campaign manifests that link child campaign URLs and stable
   run/entity/activity UUIDs. Nested campaigns also require lazy retrieval,
   cross-campaign references, federated and paginated queries, unavailable-child
   behavior, and containment-cycle rules; they are a separate architecture
   effort.
5. Evaluate physical partitioning only after measurement identifies the scale
   at which a single SQLite campaign no longer meets size or query targets.
6. Add richer action-spec value schemas when stable domain requirements exist.
7. Expand the controlled action vocabulary through an explicit governance and
   schema-version process.
8. Add W3C PROV or RO-Crate export, including agents and software association,
   without requiring those interchange formats to be the local storage model.
9. Add migration support before changing this schema after it becomes part of
   a released and actively used archive format.
