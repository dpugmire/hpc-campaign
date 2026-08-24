"""Logical data products and the activities that generated them.

The provenance model follows the W3C PROV entity/activity split. A logical
variable is a data-product entity. An activity records a scientifically
meaningful action, its role-qualified input entities, and its output entities.
The activity graph is the workflow; no duplicate workflow record or transitive
derivation closure is stored.
"""

# The storage operations are kept together so their transaction and integrity
# invariants remain visible in one module.
# pylint: disable=too-many-lines

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from time import time_ns
from typing import Any, Iterator, Mapping, Sequence

from .schema import SUPPORTED_ACTIVITY_KINDS
from .utils import sql_execute

LOGICAL_DATASET_FORMAT = "VARIABLES"
DEFAULT_RUN = "default"


@dataclass(frozen=True)
class VariableRef:
    """Public identity of one logical variable inside a campaign."""

    run: str
    dataset: str
    variable: str


@dataclass(frozen=True)
class ChunkSpec:
    """Reference to one existing campaign payload dataset."""

    payload: str
    chunk_index: int | None = None


@dataclass(frozen=True)
class VariableSpec:  # pylint: disable=too-many-instance-attributes
    """Description of a logical variable created as an activity output."""

    dataset: str
    variable: str
    run: str = DEFAULT_RUN
    definition: str | None = None
    chunks: Any = None
    primary: bool = False
    preferred_preview: VariableRef | None = None
    append: bool = False


@dataclass(frozen=True)
class ActivityRef:
    """Stable UUID identity of a recorded provenance activity."""

    uuid: str


@dataclass(frozen=True)
class ActivityResult:
    """Activity identity and its role-qualified output variables."""

    activity: ActivityRef
    outputs: Mapping[str, VariableRef]


@dataclass(frozen=True)
class VariableDeleteImpact:
    """Variables affected by deletion of one logical variable."""

    target: VariableRef
    dependent_variables: tuple[VariableRef, ...]
    preview_users: tuple[VariableRef, ...]


def ensure_variable_tables(cur: sqlite3.Cursor) -> None:  # pylint: disable=too-many-statements
    """Create the activity-based variable provenance schema and indexes.

    Integer row IDs keep local joins compact. UUID columns provide stable
    identities that can later be referenced by a parent campaign without
    changing the local representation.
    """
    sql_execute(
        cur,
        "create table if not exists campaign_run"
        "(runid INTEGER PRIMARY KEY, uuid TEXT NOT NULL UNIQUE, name TEXT NOT NULL UNIQUE)",
    )
    sql_execute(
        cur,
        "create table if not exists variable_definition(definitionid INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE)",
    )
    sql_execute(
        cur,
        "create table if not exists logical_variable"
        "(variableid INTEGER PRIMARY KEY, uuid TEXT NOT NULL UNIQUE, runid INT NOT NULL, "
        "definitionid INT NOT NULL, datasetid INT NOT NULL, name TEXT NOT NULL, preferred_preview_id INT, "
        "UNIQUE(runid, datasetid, name), "
        "UNIQUE(runid, definitionid, variableid), "
        "FOREIGN KEY(runid) REFERENCES campaign_run(runid) ON DELETE RESTRICT ON UPDATE CASCADE, "
        "FOREIGN KEY(definitionid) REFERENCES variable_definition(definitionid) "
        "ON DELETE RESTRICT ON UPDATE CASCADE, "
        "FOREIGN KEY(preferred_preview_id) REFERENCES logical_variable(variableid) "
        "ON DELETE RESTRICT ON UPDATE CASCADE)",
    )
    # A binding is preferable to a Boolean on every data product: it enforces
    # exactly one default entity for a scientific variable in each run.
    sql_execute(
        cur,
        "create table if not exists primary_variable"
        "(runid INT NOT NULL, definitionid INT NOT NULL, variableid INT NOT NULL UNIQUE, "
        "PRIMARY KEY(runid, definitionid), "
        "FOREIGN KEY(runid, definitionid, variableid) "
        "REFERENCES logical_variable(runid, definitionid, variableid) "
        "ON DELETE CASCADE ON UPDATE CASCADE)",
    )

    sql_execute(
        cur,
        "create table if not exists activity_category(categoryid INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE)",
    )
    sql_execute(
        cur,
        "create table if not exists activity_kind"
        "(kindid INTEGER PRIMARY KEY, categoryid INT NOT NULL, name TEXT NOT NULL UNIQUE, "
        "FOREIGN KEY(categoryid) REFERENCES activity_category(categoryid) "
        "ON DELETE RESTRICT ON UPDATE CASCADE)",
    )
    _seed_activity_vocabulary(cur)
    sql_execute(
        cur,
        "create table if not exists action_spec"
        "(specid INTEGER PRIMARY KEY, kindid INT NOT NULL, content_hash TEXT NOT NULL, metadata TEXT NOT NULL, "
        "UNIQUE(kindid, content_hash), "
        "UNIQUE(specid, kindid), "
        "FOREIGN KEY(kindid) REFERENCES activity_kind(kindid) ON DELETE RESTRICT ON UPDATE CASCADE)",
    )
    sql_execute(
        cur,
        "create table if not exists activity"
        "(activityid INTEGER PRIMARY KEY, uuid TEXT NOT NULL UNIQUE, runid INT, kindid INT NOT NULL, specid INT, "
        "FOREIGN KEY(runid) REFERENCES campaign_run(runid) ON DELETE RESTRICT ON UPDATE CASCADE, "
        "FOREIGN KEY(kindid) REFERENCES activity_kind(kindid) ON DELETE RESTRICT ON UPDATE CASCADE, "
        "FOREIGN KEY(specid, kindid) REFERENCES action_spec(specid, kindid) "
        "ON DELETE RESTRICT ON UPDATE CASCADE)",
    )
    # Roles are unique inside one activity. If a future action truly needs an
    # ordered repeated role, a position column can be added without changing the
    # entity/activity graph.
    sql_execute(
        cur,
        "create table if not exists activity_input"
        "(inputid INTEGER PRIMARY KEY, activityid INT NOT NULL, variableid INT NOT NULL, role TEXT NOT NULL, "
        "CHECK(length(trim(role)) > 0), "
        "UNIQUE(activityid, role), "
        "FOREIGN KEY(activityid) REFERENCES activity(activityid) ON DELETE CASCADE ON UPDATE CASCADE, "
        "FOREIGN KEY(variableid) REFERENCES logical_variable(variableid) "
        "ON DELETE RESTRICT ON UPDATE CASCADE)",
    )
    sql_execute(
        cur,
        "create table if not exists activity_output"
        "(outputid INTEGER PRIMARY KEY, activityid INT NOT NULL, variableid INT NOT NULL UNIQUE, role TEXT NOT NULL, "
        "CHECK(length(trim(role)) > 0), "
        "UNIQUE(activityid, role), "
        "FOREIGN KEY(activityid) REFERENCES activity(activityid) ON DELETE CASCADE ON UPDATE CASCADE, "
        "FOREIGN KEY(variableid) REFERENCES logical_variable(variableid) "
        "ON DELETE RESTRICT ON UPDATE CASCADE)",
    )
    sql_execute(
        cur,
        "create table if not exists variable_chunk"
        "(chunkid INTEGER PRIMARY KEY, variableid INT NOT NULL, chunk_index INT NOT NULL, "
        "payload_datasetid INT NOT NULL, UNIQUE(variableid, chunk_index), "
        "UNIQUE(variableid, payload_datasetid), "
        "FOREIGN KEY(variableid) REFERENCES logical_variable(variableid) "
        "ON DELETE CASCADE ON UPDATE CASCADE)",
    )
    # One compact row represents identity, strided, or explicit source-step
    # mappings for one append batch. This avoids one metadata row per image or
    # timestep for common mappings such as "every fifth step".
    sql_execute(
        cur,
        "create table if not exists activity_input_step_mapping"
        "(mappingid INTEGER PRIMARY KEY, inputid INT NOT NULL, output_variableid INT NOT NULL, "
        "output_start INT NOT NULL, count INT NOT NULL, encoding TEXT NOT NULL, "
        "source_start INT, stride INT, explicit_steps TEXT, "
        "CHECK(count > 0), "
        "CHECK(encoding in ('identity', 'stride', 'explicit')), "
        "CHECK((encoding = 'explicit' and source_start is null and stride is null and explicit_steps is not null) "
        "or (encoding in ('identity', 'stride') and source_start is not null "
        "and stride > 0 and explicit_steps is null)), "
        "CHECK(encoding != 'identity' or (source_start = output_start and stride = 1)), "
        "UNIQUE(inputid, output_variableid, output_start), "
        "FOREIGN KEY(inputid) REFERENCES activity_input(inputid) ON DELETE CASCADE ON UPDATE CASCADE, "
        "FOREIGN KEY(output_variableid) REFERENCES logical_variable(variableid) "
        "ON DELETE CASCADE ON UPDATE CASCADE)",
    )

    sql_execute(cur, "create index if not exists logical_variable_run_idx on logical_variable(runid)")
    sql_execute(
        cur,
        "create index if not exists logical_variable_definition_idx on logical_variable(definitionid)",
    )
    sql_execute(cur, "create index if not exists activity_kind_idx on activity(kindid)")
    sql_execute(cur, "create index if not exists activity_input_variable_idx on activity_input(variableid)")
    sql_execute(cur, "create index if not exists activity_output_activity_idx on activity_output(activityid)")
    sql_execute(
        cur,
        "create index if not exists variable_chunk_order_idx on variable_chunk(variableid, chunk_index)",
    )
    sql_execute(
        cur,
        "create index if not exists variable_chunk_payload_idx on variable_chunk(payload_datasetid)",
    )


def _seed_activity_vocabulary(cur: sqlite3.Cursor) -> None:
    """Install the fixed first-version category and activity-kind vocabulary."""
    categories = sorted(set(SUPPORTED_ACTIVITY_KINDS.values()))
    for category in categories:
        sql_execute(cur, "insert or ignore into activity_category (name) values (?)", (category,))
    for kind, category in SUPPORTED_ACTIVITY_KINDS.items():
        category_row = sql_execute(
            cur,
            "select categoryid from activity_category where name = ?",
            (category,),
        ).fetchone()
        sql_execute(
            cur,
            "insert or ignore into activity_kind (categoryid, name) values (?, ?)",
            (int(category_row[0]), kind),
        )


@contextmanager
def variable_transaction(con: sqlite3.Connection, name: str = "variable_write") -> Iterator[None]:
    """Run a provenance mutation atomically, nesting through a savepoint."""
    if con.in_transaction:
        con.execute(f"SAVEPOINT {name}")
        try:
            yield
        except Exception:
            con.execute(f"ROLLBACK TO {name}")
            con.execute(f"RELEASE {name}")
            raise
        con.execute(f"RELEASE {name}")
        return

    con.execute("BEGIN IMMEDIATE")
    try:
        yield
    except Exception:
        con.rollback()
        raise
    con.commit()


def _nonempty(value: Any, label: str) -> str:
    result = str(value or "").strip()
    if not result:
        raise ValueError(f"{label} must not be empty")
    return result


def _normalize_ref(reference: Any, label: str) -> VariableRef:
    if not isinstance(reference, VariableRef):
        raise TypeError(f"{label} must be a VariableRef")
    return VariableRef(
        run=_nonempty(reference.run, f"{label} run"),
        dataset=_nonempty(reference.dataset, f"{label} dataset"),
        variable=_nonempty(reference.variable, f"{label} variable"),
    )


def _dataset_row(cur: sqlite3.Cursor, dataset: str, *, live: bool = True) -> sqlite3.Row | None:
    condition = " and deltime = 0" if live else ""
    return sql_execute(
        cur,
        "select rowid, name, uuid, fileformat, deltime from dataset where name = ?" + condition,
        (dataset,),
    ).fetchone()


def _ensure_run(cur: sqlite3.Cursor, run: str) -> int:
    run_name = _nonempty(run, "run")
    row = sql_execute(cur, "select runid from campaign_run where name = ?", (run_name,)).fetchone()
    if row is not None:
        return int(row[0])
    result = sql_execute(
        cur,
        "insert into campaign_run (uuid, name) values (?, ?) returning runid",
        (uuid.uuid4().hex, run_name),
    )
    return int(result.fetchone()[0])


def _ensure_definition(cur: sqlite3.Cursor, definition: str) -> int:
    name = _nonempty(definition, "variable definition")
    row = sql_execute(
        cur,
        "select definitionid from variable_definition where name = ?",
        (name,),
    ).fetchone()
    if row is not None:
        return int(row[0])
    result = sql_execute(
        cur,
        "insert into variable_definition (name) values (?) returning definitionid",
        (name,),
    )
    return int(result.fetchone()[0])


def _resolve_owner_dataset(cur: sqlite3.Cursor, dataset: str, *, allow_namespace: bool) -> int:
    row = _dataset_row(cur, dataset)
    if row is not None:
        return int(row["rowid"])
    deleted = _dataset_row(cur, dataset, live=False)
    if deleted is not None:
        raise LookupError(f"Dataset is deleted: {dataset}")
    if not allow_namespace:
        raise LookupError(f"Dataset not found: {dataset}")

    namespace_uuid = uuid.uuid5(uuid.NAMESPACE_URL, f"hpc-campaign-variable-namespace:{dataset}").hex
    result = sql_execute(
        cur,
        "insert into dataset (name, uuid, modtime, deltime, fileformat, tsid, tsorder) "
        "values (?, ?, ?, 0, ?, 0, 0) returning rowid",
        (dataset, namespace_uuid, time_ns(), LOGICAL_DATASET_FORMAT),
    )
    return int(result.fetchone()[0])


def _resolve_variable(cur: sqlite3.Cursor, reference: VariableRef) -> tuple[int, int, int]:
    run_row = sql_execute(cur, "select runid from campaign_run where name = ?", (reference.run,)).fetchone()
    if run_row is None:
        raise LookupError(f"Run not found: {reference.run}")
    dataset_row = _dataset_row(cur, reference.dataset)
    if dataset_row is None:
        raise LookupError(f"Variable dataset not found or deleted: {reference.dataset}")
    run_id = int(run_row[0])
    dataset_id = int(dataset_row["rowid"])
    row = sql_execute(
        cur,
        "select variableid from logical_variable where runid = ? and datasetid = ? and name = ?",
        (run_id, dataset_id, reference.variable),
    ).fetchone()
    if row is None:
        raise LookupError(f"Logical variable not found: {reference.run}/{reference.dataset}/{reference.variable}")
    return int(row[0]), run_id, dataset_id


def _normalize_chunk_specs(chunks: Any) -> list[ChunkSpec]:
    if chunks is None:
        return []
    raw_chunks = [chunks] if isinstance(chunks, (str, ChunkSpec, Mapping)) else list(chunks)
    if not raw_chunks:
        raise ValueError("chunks must contain at least one payload")

    normalized: list[ChunkSpec] = []
    for raw_chunk in raw_chunks:
        if isinstance(raw_chunk, ChunkSpec):
            payload = raw_chunk.payload
            chunk_index = raw_chunk.chunk_index
        elif isinstance(raw_chunk, str):
            payload = raw_chunk
            chunk_index = None
        elif isinstance(raw_chunk, Mapping):
            if "payload" not in raw_chunk:
                raise ValueError(f"Chunk specification requires payload: {raw_chunk!r}")
            unknown = sorted(str(key) for key in raw_chunk if key not in {"payload", "chunk_index"})
            if unknown:
                raise ValueError(f"Chunk specification contains unsupported field(s): {', '.join(unknown)}")
            payload = raw_chunk["payload"]
            chunk_index = raw_chunk.get("chunk_index")
        else:
            raise TypeError(f"Unsupported chunk specification: {raw_chunk!r}")

        resolved_index = None
        if chunk_index is not None:
            if isinstance(chunk_index, bool):
                raise ValueError("chunk_index must be a non-negative integer")
            resolved_index = int(chunk_index)
            if resolved_index < 0 or (isinstance(chunk_index, float) and not chunk_index.is_integer()):
                raise ValueError("chunk_index must be a non-negative integer")
        normalized.append(ChunkSpec(_nonempty(payload, "chunk payload"), resolved_index))

    explicit = [chunk.chunk_index is not None for chunk in normalized]
    if any(explicit) and not all(explicit):
        raise ValueError("A chunk batch must either supply every chunk_index or omit all chunk indices")
    return normalized


def _resolve_chunks(cur: sqlite3.Cursor, chunks: Sequence[ChunkSpec]) -> list[tuple[ChunkSpec, int]]:
    resolved: list[tuple[ChunkSpec, int]] = []
    payload_ids: set[int] = set()
    for chunk in chunks:
        row = _dataset_row(cur, chunk.payload)
        if row is None:
            row = sql_execute(
                cur,
                "select rowid, name, uuid, fileformat, deltime from dataset where uuid = ? and deltime = 0",
                (chunk.payload,),
            ).fetchone()
        if row is None:
            raise LookupError(f"Chunk payload dataset not found or deleted: {chunk.payload}")
        payload_id = int(row["rowid"])
        if payload_id in payload_ids:
            raise ValueError(f"Chunk payload is duplicated in the append batch: {chunk.payload}")
        payload_ids.add(payload_id)
        resolved.append((chunk, payload_id))
    return resolved


def _insert_chunks(  # pylint: disable=too-many-locals
    cur: sqlite3.Cursor,
    variable_id: int,
    chunk_specs: Sequence[ChunkSpec],
) -> list[int]:
    """Insert one chunk batch and return its logical indices in order."""
    resolved = _resolve_chunks(cur, chunk_specs)
    if not resolved:
        return []
    existing_payload_ids = {
        int(row[0])
        for row in sql_execute(
            cur,
            "select payload_datasetid from variable_chunk where variableid = ?",
            (variable_id,),
        ).fetchall()
    }
    duplicates = [chunk.payload for chunk, payload_id in resolved if payload_id in existing_payload_ids]
    if duplicates:
        raise ValueError("Chunk payload is already present: " + ", ".join(duplicates))

    indexed: list[tuple[int, int]]
    if resolved[0][0].chunk_index is None:
        row = sql_execute(
            cur,
            "select coalesce(max(chunk_index), -1) + 1 from variable_chunk where variableid = ?",
            (variable_id,),
        ).fetchone()
        start = int(row[0])
        indexed = [(start + offset, payload_id) for offset, (_chunk, payload_id) in enumerate(resolved)]
    else:
        indexed = []
        for chunk, payload_id in resolved:
            if chunk.chunk_index is None:
                raise AssertionError("Normalized chunk indices unexpectedly changed")
            indexed.append((chunk.chunk_index, payload_id))
        indices = [index for index, _payload in indexed]
        if len(indices) != len(set(indices)):
            raise ValueError("Chunk indices must be unique within an append batch")

    existing_indices = {
        int(row[0])
        for row in sql_execute(
            cur,
            "select chunk_index from variable_chunk where variableid = ?",
            (variable_id,),
        ).fetchall()
    }
    duplicate_indices = sorted(index for index, _payload in indexed if index in existing_indices)
    if duplicate_indices:
        raise ValueError("Chunk indices already exist: " + ", ".join(str(index) for index in duplicate_indices))
    for chunk_index, payload_id in indexed:
        sql_execute(
            cur,
            "insert into variable_chunk (variableid, chunk_index, payload_datasetid) values (?, ?, ?)",
            (variable_id, chunk_index, payload_id),
        )
    return [index for index, _payload in indexed]


def _set_primary(cur: sqlite3.Cursor, run_id: int, definition_id: int, variable_id: int) -> None:
    row = sql_execute(
        cur,
        "select variableid from primary_variable where runid = ? and definitionid = ?",
        (run_id, definition_id),
    ).fetchone()
    if row is not None and int(row[0]) != variable_id:
        raise ValueError("A different primary variable is already registered for this definition and run")
    sql_execute(
        cur,
        "insert or ignore into primary_variable (runid, definitionid, variableid) values (?, ?, ?)",
        (run_id, definition_id, variable_id),
    )


def _create_or_append_variable(  # pylint: disable=too-many-locals
    cur: sqlite3.Cursor,
    spec: VariableSpec,
    *,
    allow_namespace: bool = False,
) -> tuple[VariableRef, int, int, list[int]]:
    run_name = _nonempty(spec.run, "run")
    dataset_name = _nonempty(spec.dataset, "dataset")
    variable_name = _nonempty(spec.variable, "variable")
    chunks = _normalize_chunk_specs(spec.chunks)
    run_id = _ensure_run(cur, run_name)
    dataset_id = _resolve_owner_dataset(
        cur,
        dataset_name,
        allow_namespace=allow_namespace or spec.chunks is not None,
    )
    reference = VariableRef(run_name, dataset_name, variable_name)
    existing = sql_execute(
        cur,
        "select variableid, definitionid from logical_variable where runid = ? and datasetid = ? and name = ?",
        (run_id, dataset_id, variable_name),
    ).fetchone()

    if spec.append:
        if existing is None:
            raise LookupError(f"Cannot append to missing logical variable: {run_name}/{dataset_name}/{variable_name}")
        if not chunks:
            raise ValueError("append=True requires one or more chunks")
        variable_id = int(existing["variableid"])
        definition_id = int(existing["definitionid"])
        if spec.definition is not None and _ensure_definition(cur, spec.definition) != definition_id:
            raise ValueError("append cannot change a variable definition")
        if (
            sql_execute(
                cur,
                "select 1 from variable_chunk where variableid = ? limit 1",
                (variable_id,),
            ).fetchone()
            is None
        ):
            raise ValueError("Cannot append chunks to a direct self-describing variable")
    else:
        if existing is not None:
            raise ValueError(f"Logical variable already exists: {run_name}/{dataset_name}/{variable_name}")
        definition_id = _ensure_definition(cur, spec.definition or variable_name)
        preview_id = None
        if spec.preferred_preview is not None:
            preview_id, _preview_run, _preview_dataset = _resolve_variable(
                cur, _normalize_ref(spec.preferred_preview, "preferred_preview")
            )
        result = sql_execute(
            cur,
            "insert into logical_variable "
            "(uuid, runid, definitionid, datasetid, name, preferred_preview_id) "
            "values (?, ?, ?, ?, ?, ?) returning variableid",
            (uuid.uuid4().hex, run_id, definition_id, dataset_id, variable_name, preview_id),
        )
        variable_id = int(result.fetchone()[0])

    indices = _insert_chunks(cur, variable_id, chunks)
    if spec.primary:
        _set_primary(cur, run_id, definition_id, variable_id)
    return reference, variable_id, run_id, indices


def add_variable(  # pylint: disable=too-many-arguments
    cur: sqlite3.Cursor,
    con: sqlite3.Connection,
    *,
    dataset: str,
    variable: str,
    run: str = DEFAULT_RUN,
    definition: str | None = None,
    chunks: Any = None,
    primary: bool = False,
    preferred_preview: VariableRef | None = None,
    append: bool = False,
) -> VariableRef:
    """Register a source data product or append chunks to one.

    Derived products are created through :func:`add_activity`, which guarantees
    that the generating action and its inputs are recorded atomically with the
    output entities.
    """
    spec = VariableSpec(
        run=run,
        dataset=dataset,
        variable=variable,
        definition=definition,
        chunks=chunks,
        primary=primary,
        preferred_preview=preferred_preview,
        append=append,
    )
    with variable_transaction(con):
        ensure_variable_tables(cur)
        reference, _variable_id, _run_id, _indices = _create_or_append_variable(cur, spec)
    return reference


def set_primary_variable(cur: sqlite3.Cursor, con: sqlite3.Connection, variable: VariableRef) -> None:
    """Select one existing entity as the primary definition value for its run."""
    reference = _normalize_ref(variable, "variable")
    with variable_transaction(con, "primary_variable_write"):
        variable_id, run_id, _dataset_id = _resolve_variable(cur, reference)
        row = sql_execute(
            cur,
            "select definitionid from logical_variable where variableid = ?",
            (variable_id,),
        ).fetchone()
        _set_primary(cur, run_id, int(row[0]), variable_id)


def _normalize_inputs(inputs: Any) -> list[tuple[str, VariableRef]]:
    if not isinstance(inputs, Mapping) or not inputs:
        raise ValueError("activity inputs must be a non-empty role mapping")
    normalized = [
        (
            _nonempty(raw_role, "activity input role"),
            _normalize_ref(raw_reference, f"activity input {raw_role}"),
        )
        for raw_role, raw_reference in inputs.items()
    ]
    _require_unique_roles(normalized, "activity input")
    return normalized


def _require_unique_roles(items: Sequence[tuple[str, Any]], label: str) -> None:
    """Reject distinct mapping keys that normalize to the same role."""
    roles = [role for role, _value in items]
    if len(roles) != len(set(roles)):
        raise ValueError(f"{label} roles must be unique after whitespace normalization")


def _variable_spec_from_mapping(value: Mapping[str, Any], label: str) -> VariableSpec:
    supported = {
        "run",
        "dataset",
        "variable",
        "definition",
        "chunks",
        "primary",
        "preferred_preview",
        "append",
    }
    unknown = sorted(str(key) for key in value if key not in supported)
    if unknown:
        raise ValueError(f"{label} contains unsupported field(s): {', '.join(unknown)}")
    if "dataset" not in value or "variable" not in value:
        raise ValueError(f"{label} requires dataset and variable")
    preview = value.get("preferred_preview")
    if preview is not None and not isinstance(preview, VariableRef):
        raise TypeError(f"{label} preferred_preview must be a VariableRef")
    return VariableSpec(
        run=str(value.get("run", DEFAULT_RUN)),
        dataset=str(value["dataset"]),
        variable=str(value["variable"]),
        definition=(str(value["definition"]) if value.get("definition") is not None else None),
        chunks=value.get("chunks"),
        primary=bool(value.get("primary", False)),
        preferred_preview=preview,
        append=bool(value.get("append", False)),
    )


def _normalize_outputs(outputs: Any) -> list[tuple[str, VariableSpec]]:
    if not isinstance(outputs, Mapping) or not outputs:
        raise ValueError("activity outputs must be a non-empty role mapping")
    normalized: list[tuple[str, VariableSpec]] = []
    for raw_role, raw_spec in outputs.items():
        role = _nonempty(raw_role, "activity output role")
        if isinstance(raw_spec, VariableSpec):
            spec = raw_spec
        elif isinstance(raw_spec, Mapping):
            spec = _variable_spec_from_mapping(raw_spec, f"activity output {role}")
        else:
            raise TypeError(f"activity output {role} must be a VariableSpec or mapping")
        normalized.append((role, spec))
    _require_unique_roles(normalized, "activity output")
    return normalized


def _activity_kind_id(cur: sqlite3.Cursor, action: str) -> int:
    action_name = _nonempty(action, "action")
    row = sql_execute(cur, "select kindid from activity_kind where name = ?", (action_name,)).fetchone()
    if row is None:
        allowed = ", ".join(SUPPORTED_ACTIVITY_KINDS)
        raise ValueError(f"Unsupported activity action {action_name!r}; allowed actions: {allowed}")
    return int(row[0])


def _canonical_action_spec(action_spec: Any) -> tuple[str, str] | None:
    if action_spec is None:
        return None
    if not isinstance(action_spec, Mapping):
        raise TypeError("action_spec must be an object")
    try:
        metadata = json.dumps(action_spec, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError("action_spec must be JSON-compatible") from exc
    return metadata, hashlib.sha256(metadata.encode("utf-8")).hexdigest()


def _ensure_action_spec(cur: sqlite3.Cursor, kind_id: int, action_spec: Any) -> int | None:
    canonical = _canonical_action_spec(action_spec)
    if canonical is None:
        return None
    metadata, content_hash = canonical
    row = sql_execute(
        cur,
        "select specid, metadata from action_spec where kindid = ? and content_hash = ?",
        (kind_id, content_hash),
    ).fetchone()
    if row is not None:
        if str(row["metadata"]) != metadata:
            raise ValueError("Action specification hash collision detected")
        return int(row[0])
    result = sql_execute(
        cur,
        "insert into action_spec (kindid, content_hash, metadata) values (?, ?, ?) returning specid",
        (kind_id, content_hash, metadata),
    )
    return int(result.fetchone()[0])


def _normalize_step(value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError("source_steps must contain non-negative integers")
    try:
        step = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("source_steps must contain non-negative integers") from exc
    if step < 0 or (isinstance(value, float) and not value.is_integer()):
        raise ValueError("source_steps must contain non-negative integers")
    return step


def _expand_steps(value: Any, expected_count: int, label: str) -> Sequence[int]:
    if isinstance(value, Mapping) and set(value).issubset({"start", "count", "stride"}):
        start = _normalize_step(value.get("start", 0))
        raw_count = value.get("count", expected_count)
        if isinstance(raw_count, bool):
            raise ValueError(f"{label} count must be a non-negative integer")
        try:
            count = int(raw_count)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{label} count must be a non-negative integer") from exc
        if count < 0 or (isinstance(raw_count, float) and not raw_count.is_integer()):
            raise ValueError(f"{label} count must be a non-negative integer")
        stride = _normalize_step(value.get("stride", 1))
        if count != expected_count:
            raise ValueError(f"{label} count is {count}; expected {expected_count}")
        if stride <= 0:
            raise ValueError(f"{label} stride must be positive")
        return range(start, start + count * stride, stride)
    values = [_normalize_step(item) for item in list(value)]
    if len(values) != expected_count:
        raise ValueError(f"{label} contains {len(values)} entries; expected {expected_count}")
    return values


def _steps_by_output_and_input(
    source_steps: Any,
    output_roles: Sequence[str],
    input_roles: Sequence[str],
    counts: Mapping[str, int],
) -> dict[tuple[str, str], Sequence[int]]:
    if source_steps is None:
        return {}
    if len(output_roles) == 1:
        raw_outputs = {output_roles[0]: source_steps}
    elif isinstance(source_steps, Mapping):
        raw_outputs = dict(source_steps)
    else:
        raise TypeError("Multi-output source_steps must map output roles to input mappings")
    if set(raw_outputs) != set(output_roles):
        raise ValueError("source_steps output roles do not match activity outputs")

    normalized: dict[tuple[str, str], Sequence[int]] = {}
    for output_role, raw_inputs in raw_outputs.items():
        count = counts[output_role]
        if count <= 0:
            raise ValueError(f"source_steps requires chunked output {output_role!r}")
        compact_descriptor = isinstance(raw_inputs, Mapping) and set(raw_inputs).issubset({"start", "count", "stride"})
        if len(input_roles) == 1 and (
            compact_descriptor or not isinstance(raw_inputs, Mapping) or not set(raw_inputs).intersection(input_roles)
        ):
            by_input = {input_roles[0]: raw_inputs}
        elif isinstance(raw_inputs, Mapping):
            by_input = dict(raw_inputs)
        else:
            raise TypeError(f"source_steps for output {output_role!r} must map input roles to step sequences")
        if set(by_input) != set(input_roles):
            raise ValueError(f"source_steps input roles for output {output_role!r} do not match activity inputs")
        for input_role, raw_values in by_input.items():
            normalized[(output_role, input_role)] = _expand_steps(
                raw_values,
                count,
                f"source_steps[{output_role!r}][{input_role!r}]",
            )
    return normalized


def _store_step_mapping(
    cur: sqlite3.Cursor,
    input_id: int,
    output_variable_id: int,
    output_indices: Sequence[int],
    source_steps: Sequence[int],
) -> None:
    if not output_indices:
        return
    if any(value != output_indices[0] + offset for offset, value in enumerate(output_indices)):
        raise ValueError("source_steps requires a dense output chunk-index range")
    output_start = output_indices[0]
    count = len(source_steps)
    detected_stride = source_steps[1] - source_steps[0] if count > 1 else 1
    regular = all(source_steps[index] == source_steps[0] + index * detected_stride for index in range(count))
    if regular and detected_stride > 0:
        encoding = "identity" if detected_stride == 1 and source_steps[0] == output_start else "stride"
        source_start = source_steps[0]
        stored_stride: int | None = detected_stride
        explicit_steps = None
    else:
        encoding = "explicit"
        source_start = None
        stored_stride = None
        explicit_steps = json.dumps(list(source_steps), separators=(",", ":"))
    sql_execute(
        cur,
        "insert into activity_input_step_mapping "
        "(inputid, output_variableid, output_start, count, encoding, source_start, stride, explicit_steps) "
        "values (?, ?, ?, ?, ?, ?, ?, ?)",
        (input_id, output_variable_id, output_start, count, encoding, source_start, stored_stride, explicit_steps),
    )


def _existing_activity_for_append(
    cur: sqlite3.Cursor,
    output_reference: VariableRef,
    action: str,
    input_pairs: Sequence[tuple[str, int]],
    spec_id: int | None,
) -> tuple[int, str, str, int]:
    variable_id, run_id, _dataset_id = _resolve_variable(cur, output_reference)
    row = sql_execute(
        cur,
        "select a.activityid, a.uuid, kind.name as action, a.specid, output.role "
        "from activity_output as output join activity as a on a.activityid = output.activityid "
        "join activity_kind as kind on kind.kindid = a.kindid where output.variableid = ?",
        (variable_id,),
    ).fetchone()
    if row is None:
        raise ValueError("Cannot append an activity output that has no generating activity")
    if str(row["action"]) != action:
        raise ValueError("append cannot change the generating activity action")
    existing_spec = int(row["specid"]) if row["specid"] is not None else None
    if spec_id is not None and spec_id != existing_spec:
        raise ValueError("append cannot change an action specification")
    existing_inputs = {
        (str(item["role"]), int(item["variableid"]))
        for item in sql_execute(
            cur,
            "select role, variableid from activity_input where activityid = ?",
            (int(row["activityid"]),),
        ).fetchall()
    }
    if set(input_pairs) != existing_inputs:
        raise ValueError("append cannot change activity inputs")
    return int(row["activityid"]), str(row["uuid"]), str(row["role"]), run_id


def add_activity(  # pylint: disable=too-many-arguments,too-many-locals,too-many-statements
    cur: sqlite3.Cursor,
    con: sqlite3.Connection,
    *,
    action: str,
    inputs: Mapping[str, VariableRef],
    outputs: Mapping[str, VariableSpec | Mapping[str, Any]],
    action_spec: Mapping[str, Any] | None = None,
    source_steps: Any = None,
) -> ActivityResult:
    """Atomically record an activity and the data products it generated.

    Output specs normally create new variables. A single output with
    ``append=True`` appends chunks and compact step mappings to the variable's
    existing generating activity.
    """
    action_name = _nonempty(action, "action")
    normalized_inputs = _normalize_inputs(inputs)
    normalized_outputs = _normalize_outputs(outputs)
    append_outputs = [(role, spec) for role, spec in normalized_outputs if spec.append]
    if append_outputs and (len(append_outputs) != 1 or len(normalized_outputs) != 1):
        raise ValueError("Activity append supports exactly one output")

    with variable_transaction(con, "activity_write"):
        ensure_variable_tables(cur)
        kind_id = _activity_kind_id(cur, action_name)
        spec_id = _ensure_action_spec(cur, kind_id, action_spec)
        resolved_inputs: list[tuple[str, int, VariableRef, int]] = []
        for role, reference in normalized_inputs:
            variable_id, run_id, _dataset_id = _resolve_variable(cur, reference)
            resolved_inputs.append((role, variable_id, reference, run_id))

        if append_outputs:
            output_role, output_spec = append_outputs[0]
            output_reference = VariableRef(output_spec.run, output_spec.dataset, output_spec.variable)
            activity_id, activity_uuid, existing_role, _run_id = _existing_activity_for_append(
                cur,
                output_reference,
                action_name,
                [(role, variable_id) for role, variable_id, _reference, _run in resolved_inputs],
                spec_id,
            )
            if output_role != existing_role:
                raise ValueError("append cannot change an activity output role")
            reference, output_id, _output_run, output_indices = _create_or_append_variable(
                cur,
                output_spec,
                allow_namespace=True,
            )
            input_rows = {
                str(row["role"]): int(row["inputid"])
                for row in sql_execute(
                    cur,
                    "select inputid, role from activity_input where activityid = ?",
                    (activity_id,),
                ).fetchall()
            }
            steps = _steps_by_output_and_input(
                source_steps,
                [output_role],
                [role for role, _id, _ref, _run in resolved_inputs],
                {output_role: len(output_indices)},
            )
            for input_role, input_id in input_rows.items():
                values = steps.get((output_role, input_role))
                if values is not None:
                    _store_step_mapping(cur, input_id, output_id, output_indices, values)
            return ActivityResult(ActivityRef(activity_uuid), {output_role: reference})

        output_records: list[tuple[str, VariableRef, int, int, list[int]]] = []
        for role, output_spec in normalized_outputs:
            reference, variable_id, run_id, indices = _create_or_append_variable(
                cur,
                output_spec,
                allow_namespace=True,
            )
            if any(variable_id == input_id for _role, input_id, _reference, _run in resolved_inputs):
                raise ValueError("An activity output cannot also be one of its inputs")
            output_records.append((role, reference, variable_id, run_id, indices))

        all_run_ids = {run_id for _role, _id, _ref, run_id in resolved_inputs}
        all_run_ids.update(run_id for _role, _ref, _id, run_id, _indices in output_records)
        activity_run_id = next(iter(all_run_ids)) if len(all_run_ids) == 1 else None
        activity_uuid = uuid.uuid4().hex
        result = sql_execute(
            cur,
            "insert into activity (uuid, runid, kindid, specid) values (?, ?, ?, ?) returning activityid",
            (activity_uuid, activity_run_id, kind_id, spec_id),
        )
        activity_id = int(result.fetchone()[0])
        input_ids: dict[str, int] = {}
        for role, variable_id, _reference, _run_id in resolved_inputs:
            result = sql_execute(
                cur,
                "insert into activity_input (activityid, variableid, role) values (?, ?, ?) returning inputid",
                (activity_id, variable_id, role),
            )
            input_ids[role] = int(result.fetchone()[0])
        for role, _reference, variable_id, _run_id, _indices in output_records:
            sql_execute(
                cur,
                "insert into activity_output (activityid, variableid, role) values (?, ?, ?)",
                (activity_id, variable_id, role),
            )

        output_counts = {role: len(indices) for role, _ref, _id, _run, indices in output_records}
        steps = _steps_by_output_and_input(
            source_steps,
            [role for role, _spec in normalized_outputs],
            [role for role, _id, _reference, _run in resolved_inputs],
            output_counts,
        )
        for output_role, _reference, output_id, _run_id, output_indices in output_records:
            for input_role, input_id in input_ids.items():
                values = steps.get((output_role, input_role))
                if values is not None:
                    _store_step_mapping(cur, input_id, output_id, output_indices, values)

    return ActivityResult(
        ActivityRef(activity_uuid),
        {role: reference for role, reference, _id, _run, _indices in output_records},
    )


def _variable_ref_by_id(cur: sqlite3.Cursor, variable_id: int) -> VariableRef:
    row = sql_execute(
        cur,
        "select run.name as run_name, d.name as dataset_name, lv.name as variable_name "
        "from logical_variable as lv join campaign_run as run on run.runid = lv.runid "
        "join dataset as d on d.rowid = lv.datasetid where lv.variableid = ?",
        (variable_id,),
    ).fetchone()
    if row is None:
        raise LookupError(f"Logical variable ID not found: {variable_id}")
    return VariableRef(str(row["run_name"]), str(row["dataset_name"]), str(row["variable_name"]))


def variable_delete_impact(cur: sqlite3.Cursor, variable: VariableRef) -> VariableDeleteImpact:
    """Return all downstream products and preferred-preview users."""
    reference = _normalize_ref(variable, "variable")
    variable_id, _run_id, _dataset_id = _resolve_variable(cur, reference)
    rows = sql_execute(
        cur,
        "with recursive descendants(variableid) as ("
        "select output.variableid from activity_input as input "
        "join activity_output as output on output.activityid = input.activityid "
        "where input.variableid = ? "
        "union "
        "select output.variableid from activity_input as input "
        "join activity_output as output on output.activityid = input.activityid "
        "join descendants on descendants.variableid = input.variableid) "
        "select variableid from descendants where variableid != ? order by variableid",
        (variable_id, variable_id),
    ).fetchall()
    descendant_ids = [int(row[0]) for row in rows]
    affected = [variable_id, *descendant_ids]
    placeholders = ", ".join("?" for _item in affected)
    preview_rows = sql_execute(
        cur,
        f"select variableid from logical_variable where preferred_preview_id in ({placeholders}) order by variableid",
        tuple(affected),
    ).fetchall()
    return VariableDeleteImpact(
        target=reference,
        dependent_variables=tuple(_variable_ref_by_id(cur, item) for item in descendant_ids),
        preview_users=tuple(_variable_ref_by_id(cur, int(row[0])) for row in preview_rows),
    )


def delete_variable(
    cur: sqlite3.Cursor,
    con: sqlite3.Connection,
    variable: VariableRef,
    *,
    cascade: bool = False,
) -> VariableDeleteImpact:
    """Delete a data product and optionally every product derived from it."""
    reference = _normalize_ref(variable, "variable")
    with variable_transaction(con, "variable_delete"):
        _variable_id, _run_id, _dataset_id = _resolve_variable(cur, reference)
        impact = variable_delete_impact(cur, reference)
        if not cascade and (impact.dependent_variables or impact.preview_users):
            raise ValueError(
                "Logical variable is still referenced; inspect variable_delete_impact() and use cascade=True"
            )
        delete_ids = {_resolve_variable(cur, item)[0] for item in (impact.target, *impact.dependent_variables)}
        placeholders = ", ".join("?" for _item in delete_ids)
        parameters = tuple(sorted(delete_ids))
        sql_execute(
            cur,
            f"update logical_variable set preferred_preview_id = null where preferred_preview_id in ({placeholders})",
            parameters,
        )
        # Activities consuming a deleted entity generated only descendants in
        # this set. Delete those activities first to release input FKs.
        sql_execute(
            cur,
            f"delete from activity where activityid in "
            f"(select activityid from activity_input where variableid in ({placeholders}))",
            parameters,
        )
        # A selected entity may be only one output of a still-useful activity.
        # Remove its generation edge without discarding sibling outputs.
        sql_execute(
            cur,
            f"delete from activity_output where variableid in ({placeholders})",
            parameters,
        )
        sql_execute(
            cur,
            f"delete from logical_variable where variableid in ({placeholders})",
            parameters,
        )
        sql_execute(
            cur,
            "delete from activity where activityid not in (select activityid from activity_output)",
        )
    return impact
