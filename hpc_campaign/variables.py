"""Logical campaign variables, representation edges, and ordered chunks."""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from time import time_ns
from typing import Any, Iterator, Mapping, Sequence

import adios2  # type: ignore[import-untyped]

from .utils import sql_execute

LOGICAL_DATASET_FORMAT = "VARIABLES"


@dataclass(frozen=True)
class VariableRef:
    """Stable public identity for a logical variable."""

    dataset: str
    variable: str


@dataclass(frozen=True)
class ChunkSpec:
    """Reference to one existing campaign payload dataset."""

    payload: str
    chunk_index: int | None = None


@dataclass(frozen=True)
class VariableDeleteImpact:
    """Variables affected by deletion of one logical variable."""

    target: VariableRef
    dependent_representations: tuple[VariableRef, ...]
    preview_users: tuple[VariableRef, ...]


def ensure_variable_tables(cur: sqlite3.Cursor) -> None:
    """Create the unified logical-variable schema and its lookup indexes.

    The schema separates logical identity, graph relationships, ordered payload
    references, and per-parent temporal mappings. Foreign-key cascades remove
    edge and step-mapping rows when their owning variable or chunk is removed.
    """
    # A logical variable belongs either to a self-describing dataset or to a
    # synthetic VARIABLES dataset that acts only as a namespace.
    sql_execute(
        cur,
        "create table if not exists logical_variable"
        "(variableid INTEGER PRIMARY KEY, datasetid INT NOT NULL, name TEXT NOT NULL, "
        "representation_kind TEXT, representation_metadata TEXT, preferred_preview_id INT, "
        "UNIQUE(datasetid, name), "
        "FOREIGN KEY(preferred_preview_id) REFERENCES logical_variable(variableid) "
        "ON DELETE RESTRICT ON UPDATE CASCADE)",
    )
    # Edges point from a representation (child) to the variables it represents
    # (parents). Labels distinguish the roles in a multi-parent representation.
    sql_execute(
        cur,
        "create table if not exists variable_representation_edge"
        "(edgeid INTEGER PRIMARY KEY, child_variable_id INT NOT NULL, parent_variable_id INT NOT NULL, "
        "label TEXT NOT NULL, identity_steps INT NOT NULL DEFAULT 0, "
        "UNIQUE(child_variable_id, label), UNIQUE(child_variable_id, parent_variable_id), "
        "FOREIGN KEY(child_variable_id) REFERENCES logical_variable(variableid) "
        "ON DELETE CASCADE ON UPDATE CASCADE, "
        "FOREIGN KEY(parent_variable_id) REFERENCES logical_variable(variableid) "
        "ON DELETE RESTRICT ON UPDATE CASCADE)",
    )
    # Schema initialization also verifies that the cached identity-step flag is
    # present before relationship rows are written.
    edge_columns = {
        str(row["name"] if isinstance(row, sqlite3.Row) else row[1])
        for row in sql_execute(cur, "pragma table_info(variable_representation_edge)").fetchall()
    }
    if "identity_steps" not in edge_columns:
        sql_execute(
            cur,
            "alter table variable_representation_edge add column identity_steps INT NOT NULL DEFAULT 0",
        )
    # Chunk indices define logical order independently of payload dataset IDs.
    sql_execute(
        cur,
        "create table if not exists variable_chunk"
        "(chunkid INTEGER PRIMARY KEY, variableid INT NOT NULL, chunk_index INT NOT NULL, "
        "payload_datasetid INT NOT NULL, UNIQUE(variableid, chunk_index), "
        "UNIQUE(variableid, payload_datasetid), "
        "FOREIGN KEY(variableid) REFERENCES logical_variable(variableid) "
        "ON DELETE CASCADE ON UPDATE CASCADE)",
    )
    # A mapping row records which step of one parent edge produced one chunk.
    sql_execute(
        cur,
        "create table if not exists variable_chunk_source_step"
        "(chunkid INT NOT NULL, edgeid INT NOT NULL, source_step INT NOT NULL, "
        "PRIMARY KEY(chunkid, edgeid), "
        "FOREIGN KEY(chunkid) REFERENCES variable_chunk(chunkid) "
        "ON DELETE CASCADE ON UPDATE CASCADE, "
        "FOREIGN KEY(edgeid) REFERENCES variable_representation_edge(edgeid) "
        "ON DELETE CASCADE ON UPDATE CASCADE)",
    )
    sql_execute(
        cur,
        "create index if not exists variable_edge_parent_idx on variable_representation_edge(parent_variable_id)",
    )
    sql_execute(
        cur,
        "create index if not exists logical_variable_kind_idx on logical_variable(representation_kind)",
    )
    sql_execute(
        cur,
        "create index if not exists variable_chunk_order_idx on variable_chunk(variableid, chunk_index)",
    )
    sql_execute(
        cur,
        "create index if not exists variable_chunk_payload_idx on variable_chunk(payload_datasetid)",
    )


@contextmanager
def variable_transaction(con: sqlite3.Connection, name: str = "variable_write") -> Iterator[None]:
    """Run a variable mutation atomically, nesting through a savepoint when needed.

    Savepoints allow higher-level operations, such as image ingestion, to include
    variable updates in their own transaction without an inner commit. An outer
    ``BEGIN IMMEDIATE`` also serializes writers while dense chunk indices are
    calculated.
    """
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


def _serialize_metadata(metadata: Any) -> str | None:
    if metadata is None:
        return None
    try:
        return json.dumps(metadata, sort_keys=True, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError("representation_metadata must be JSON-compatible") from exc


def _normalize_ref(reference: Any, label: str) -> VariableRef:
    if isinstance(reference, VariableRef):
        return VariableRef(
            dataset=_nonempty(reference.dataset, f"{label} dataset"),
            variable=_nonempty(reference.variable, f"{label} variable"),
        )
    raise TypeError(f"{label} must be a VariableRef")


def _dataset_row(cur: sqlite3.Cursor, dataset: str, *, live: bool = True) -> sqlite3.Row | None:
    condition = " and deltime = 0" if live else ""
    return sql_execute(
        cur,
        "select rowid, name, uuid, fileformat, deltime from dataset where name = ?" + condition,
        (dataset,),
    ).fetchone()


def _resolve_owner_dataset(cur: sqlite3.Cursor, dataset: str, *, allow_namespace: bool) -> int:
    row = _dataset_row(cur, dataset)
    if row is not None:
        return int(row["rowid"])
    deleted = _dataset_row(cur, dataset, live=False)
    if deleted is not None:
        raise LookupError(f"Dataset is deleted: {dataset}")
    if not allow_namespace:
        raise LookupError(f"Dataset not found: {dataset}")

    # Chunk-backed variables need a logical owner but not a file of that name.
    # A deterministic UUID gives that namespace the same identity on every run.
    namespace_uuid = uuid.uuid5(uuid.NAMESPACE_URL, f"hpc-campaign-variable-namespace:{dataset}").hex
    result = sql_execute(
        cur,
        "insert into dataset (name, uuid, modtime, deltime, fileformat, tsid, tsorder) "
        "values (?, ?, ?, 0, ?, 0, 0) returning rowid",
        (dataset, namespace_uuid, time_ns(), LOGICAL_DATASET_FORMAT),
    )
    return int(result.fetchone()[0])


def _resolve_variable(cur: sqlite3.Cursor, reference: VariableRef) -> tuple[int, int]:
    dataset_row = _dataset_row(cur, reference.dataset)
    if dataset_row is None:
        raise LookupError(f"Variable dataset not found or deleted: {reference.dataset}")
    dataset_id = int(dataset_row["rowid"])
    row = sql_execute(
        cur,
        "select variableid from logical_variable where datasetid = ? and name = ?",
        (dataset_id, reference.variable),
    ).fetchone()
    if row is None:
        raise LookupError(f"Logical variable not found: {reference.dataset}/{reference.variable}")
    return int(row[0]), dataset_id


def _normalize_parents(representation_of: Any) -> list[tuple[str, VariableRef]]:
    if representation_of is None:
        return []
    # A mapping supplies explicit role labels for a multi-parent representation.
    if isinstance(representation_of, Mapping):
        normalized: list[tuple[str, VariableRef]] = []
        labels: set[str] = set()
        for raw_label, raw_ref in representation_of.items():
            label = _nonempty(raw_label, "representation parent label")
            if label in labels:
                raise ValueError(f"Representation parent labels must be unique: {label}")
            labels.add(label)
            normalized.append((label, _normalize_ref(raw_ref, f"representation parent {label}")))
        if not normalized:
            raise ValueError("representation_of mapping must not be empty")
        return normalized

    # The parent's variable name is a convenient stable label for the common
    # single-parent form.
    parent = _normalize_ref(representation_of, "representation parent")
    return [(parent.variable, parent)]


def _resolve_parents(cur: sqlite3.Cursor, representation_of: Any) -> list[tuple[str, int, VariableRef]]:
    resolved: list[tuple[str, int, VariableRef]] = []
    parent_ids: set[int] = set()
    for label, reference in _normalize_parents(representation_of):
        parent_id, _dataset_id = _resolve_variable(cur, reference)
        if parent_id in parent_ids:
            raise ValueError(f"Representation parent is listed more than once: {reference}")
        parent_ids.add(parent_id)
        resolved.append((label, parent_id, reference))
    return resolved


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
            payload = raw_chunk["payload"]
            chunk_index = raw_chunk.get("chunk_index")
        else:
            raise TypeError(f"Unsupported chunk specification: {raw_chunk!r}")

        payload_name = _nonempty(payload, "chunk payload")
        resolved_index = None
        if chunk_index is not None:
            if isinstance(chunk_index, bool):
                raise ValueError("chunk_index must be a non-negative integer")
            resolved_index = int(chunk_index)
            if resolved_index < 0 or (isinstance(chunk_index, float) and not chunk_index.is_integer()):
                raise ValueError("chunk_index must be a non-negative integer")
        normalized.append(ChunkSpec(payload=payload_name, chunk_index=resolved_index))

    # Mixing explicit and automatic indices makes ordering ambiguous within one
    # batch, so callers must choose one indexing mode for the whole batch.
    explicit = [chunk.chunk_index is not None for chunk in normalized]
    if any(explicit) and not all(explicit):
        raise ValueError("A chunk batch must either supply every chunk_index or omit all chunk indices")
    return normalized


def _resolve_chunks(cur: sqlite3.Cursor, chunks: Sequence[ChunkSpec]) -> list[tuple[ChunkSpec, int]]:
    resolved: list[tuple[ChunkSpec, int]] = []
    payload_ids: set[int] = set()
    for chunk in chunks:
        # Payload references accept the human-readable dataset name first and
        # fall back to UUID for callers that retain stable dataset identities.
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


def _normalize_source_steps(
    source_steps: Any,
    edges: Sequence[tuple[int, str]],
    chunk_count: int,
) -> dict[int, list[int]]:
    if source_steps is None:
        return {}
    if not edges:
        raise ValueError("source_steps requires at least one representation parent")

    # A single parent permits a compact sequence. Multiple parents require a
    # sequence per edge label so every temporal mapping remains unambiguous.
    if len(edges) == 1 and not isinstance(source_steps, Mapping):
        raw_by_label = {edges[0][1]: list(source_steps)}
    else:
        if not isinstance(source_steps, Mapping):
            raise TypeError("Multi-parent source_steps must map parent labels to step sequences")
        raw_by_label = dict(source_steps)

    known_labels = {label for _edge_id, label in edges}
    supplied_labels = {str(label) for label in raw_by_label}
    if supplied_labels != known_labels:
        missing = sorted(known_labels - supplied_labels)
        unknown = sorted(supplied_labels - known_labels)
        details = []
        if missing:
            details.append(f"missing: {', '.join(missing)}")
        if unknown:
            details.append(f"unknown: {', '.join(unknown)}")
        raise ValueError("source_steps labels do not match representation parents (" + "; ".join(details) + ")")

    # Convert labels to edge IDs here; storage is keyed by the exact relationship,
    # not by a label that could be reused after relationships are replaced.
    normalized: dict[int, list[int]] = {}
    edge_by_label = {label: edge_id for edge_id, label in edges}
    for raw_label, raw_values in raw_by_label.items():
        label = str(raw_label)
        values = list(raw_values)
        if len(values) != chunk_count:
            raise ValueError(f"source_steps for {label!r} contains {len(values)} entries; expected {chunk_count}")
        normalized[edge_by_label[label]] = [_normalize_step(value) for value in values]
    return normalized


def _edge_rows(cur: sqlite3.Cursor, variable_id: int) -> list[tuple[int, str, int]]:
    rows = sql_execute(
        cur,
        "select edgeid, label, parent_variable_id from variable_representation_edge "
        "where child_variable_id = ? order by edgeid",
        (variable_id,),
    ).fetchall()
    return [(int(row["edgeid"]), str(row["label"]), int(row["parent_variable_id"])) for row in rows]


def _assert_no_cycle(cur: sqlite3.Cursor, child_id: int, parent_ids: Sequence[int]) -> None:
    if child_id in parent_ids:
        raise ValueError("A logical variable cannot represent itself")
    for parent_id in parent_ids:
        # Adding child -> parent is illegal when child is already reachable by
        # walking from that parent toward its ancestors.
        row = sql_execute(
            cur,
            "with recursive ancestors(variableid) as ("
            "select parent_variable_id from variable_representation_edge where child_variable_id = ? "
            "union "
            "select edge.parent_variable_id from variable_representation_edge as edge "
            "join ancestors on edge.child_variable_id = ancestors.variableid) "
            "select 1 from ancestors where variableid = ? limit 1",
            (parent_id, child_id),
        ).fetchone()
        if row is not None:
            raise ValueError("representation_of would create a cycle")


def _insert_edges(
    cur: sqlite3.Cursor,
    child_id: int,
    parents: Sequence[tuple[str, int, VariableRef]],
    *,
    identity_eligible: bool = True,
) -> None:
    _assert_no_cycle(cur, child_id, [parent_id for _label, parent_id, _ref in parents])
    for label, parent_id, _reference in parents:
        # Identity is inferred only for one direct parent. Chunk-backed variables
        # use explicit source-step mappings instead of this cached shortcut.
        identity_steps = int(
            identity_eligible and len(parents) == 1 and _verified_identity_steps(cur, child_id, parent_id)
        )
        sql_execute(
            cur,
            "insert into variable_representation_edge "
            "(child_variable_id, parent_variable_id, label, identity_steps) values (?, ?, ?, ?)",
            (child_id, parent_id, label, identity_steps),
        )


def _direct_variable_step_count(cur: sqlite3.Cursor, variable_id: int) -> int | None:
    # A variable with chunks is not a direct variable in its owner dataset.
    chunk = sql_execute(
        cur,
        "select 1 from variable_chunk where variableid = ? limit 1",
        (variable_id,),
    ).fetchone()
    if chunk is not None:
        return None
    row = sql_execute(
        cur,
        "select lv.name as variable_name, d.rowid as datasetid, d.fileformat "
        "from logical_variable as lv join dataset as d on d.rowid = lv.datasetid "
        "where lv.variableid = ? and d.deltime = 0",
        (variable_id,),
    ).fetchone()
    if row is None or str(row["fileformat"]) != "ADIOS":
        return None
    # Identity inference deliberately uses a live, local, non-archival replica;
    # inaccessible remote or archived data must not make a relationship invalid.
    replica = sql_execute(
        cur,
        "select r.name as replica_name, directory.name as directory_name "
        "from replica as r join directory on directory.rowid = r.dirid "
        "where r.datasetid = ? and r.deltime = 0 and r.archiveid = 0 order by r.rowid limit 1",
        (int(row["datasetid"]),),
    ).fetchone()
    if replica is None:
        return None

    replica_path = Path(str(replica["replica_name"]))
    if not replica_path.is_absolute():
        replica_path = Path(str(replica["directory_name"])) / replica_path
    if not replica_path.exists():
        return None
    # Step inference is an optimization recorded when it can be verified. Failure
    # to inspect optional payload data simply leaves the edge non-identity.
    try:
        with adios2.FileReader(str(replica_path)) as reader:
            adios_variable = reader.inquire_variable(str(row["variable_name"]))
            return int(adios_variable.steps()) if adios_variable is not None else None
    except (OSError, RuntimeError, ValueError):
        return None


def _verified_identity_steps(cur: sqlite3.Cursor, child_id: int, parent_id: int) -> bool:
    child_steps = _direct_variable_step_count(cur, child_id)
    parent_steps = _direct_variable_step_count(cur, parent_id)
    return child_steps is not None and child_steps == parent_steps


def add_variable(  # pylint: disable=too-many-arguments,too-many-locals,too-many-statements
    cur: sqlite3.Cursor,
    con: sqlite3.Connection,
    *,
    dataset: str,
    variable: str,
    chunks: Any = None,
    representation_of: Any = None,
    representation_kind: str | None = None,
    representation_metadata: Any = None,
    source_steps: Any = None,
    preferred_preview: VariableRef | None = None,
    append: bool = False,
) -> VariableRef:
    """Create a logical variable or append ordered chunks to one.

    Without ``chunks``, ``dataset`` must already exist and the variable directly
    names data in that self-describing dataset. With ``chunks``, the dataset may
    instead be a synthetic namespace and each chunk references an existing
    campaign payload dataset.

    ``append=True`` is valid only for a chunk-backed variable. It may update
    optional metadata and append payloads, but it cannot redefine established
    representation parents. Source-step sequences align positionally with the
    chunks in the current batch.
    """
    dataset_name = _nonempty(dataset, "dataset")
    variable_name = _nonempty(variable, "variable")
    kind = None if representation_kind is None else _nonempty(representation_kind, "representation_kind")
    metadata_json = _serialize_metadata(representation_metadata)
    chunk_specs = _normalize_chunk_specs(chunks)
    if chunks is None and source_steps is not None:
        raise ValueError("source_steps can only be supplied with chunks")

    # Variable identity, graph edges, chunks, and temporal mappings are one
    # logical write and must never become visible in a partially updated state.
    with variable_transaction(con):
        ensure_variable_tables(cur)
        dataset_id = _resolve_owner_dataset(cur, dataset_name, allow_namespace=chunks is not None)
        existing = sql_execute(
            cur,
            "select variableid, representation_kind, representation_metadata, preferred_preview_id "
            "from logical_variable where datasetid = ? and name = ?",
            (dataset_id, variable_name),
        ).fetchone()

        if append:
            # Appending extends an existing chunk-backed identity. Parent edges
            # are immutable here because existing chunks may already map to them.
            if existing is None:
                raise LookupError(f"Cannot append to missing logical variable: {dataset_name}/{variable_name}")
            if not chunk_specs:
                raise ValueError("append=True requires one or more chunks")
            variable_id = int(existing["variableid"])
            existing_chunk = sql_execute(
                cur,
                "select 1 from variable_chunk where variableid = ? limit 1",
                (variable_id,),
            ).fetchone()
            if existing_chunk is None:
                raise ValueError("Cannot append chunks to a direct self-describing variable")
            current_edges = _edge_rows(cur, variable_id)
            if representation_of is not None:
                requested = _resolve_parents(cur, representation_of)
                requested_pairs = {(label, parent_id) for label, parent_id, _ref in requested}
                current_pairs = {(label, parent_id) for _edge_id, label, parent_id in current_edges}
                if requested_pairs != current_pairs:
                    raise ValueError("append cannot change established representation parents")
            updates: list[str] = []
            parameters: list[Any] = []
            if kind is not None:
                updates.append("representation_kind = ?")
                parameters.append(kind)
            if representation_metadata is not None:
                updates.append("representation_metadata = ?")
                parameters.append(metadata_json)
            if preferred_preview is not None:
                preview_id, _preview_dataset = _resolve_variable(
                    cur, _normalize_ref(preferred_preview, "preferred_preview")
                )
                if preview_id == variable_id:
                    raise ValueError("A logical variable cannot be its own preferred preview")
                updates.append("preferred_preview_id = ?")
                parameters.append(preview_id)
            if updates:
                parameters.append(variable_id)
                sql_execute(
                    cur,
                    "update logical_variable set " + ", ".join(updates) + " where variableid = ?",
                    tuple(parameters),
                )
        else:
            # Creation establishes identity and parent edges before chunk rows so
            # source-step mappings can reference the newly assigned edge IDs.
            if existing is not None:
                raise ValueError(f"Logical variable already exists: {dataset_name}/{variable_name}")
            parents = _resolve_parents(cur, representation_of)
            preview_id = None
            if preferred_preview is not None:
                preview_id, _preview_dataset = _resolve_variable(
                    cur, _normalize_ref(preferred_preview, "preferred_preview")
                )
            result = sql_execute(
                cur,
                "insert into logical_variable "
                "(datasetid, name, representation_kind, representation_metadata, preferred_preview_id) "
                "values (?, ?, ?, ?, ?) returning variableid",
                (dataset_id, variable_name, kind, metadata_json, preview_id),
            )
            variable_id = int(result.fetchone()[0])
            _insert_edges(cur, variable_id, parents, identity_eligible=not chunk_specs)
            current_edges = _edge_rows(cur, variable_id)

        resolved_chunks = _resolve_chunks(cur, chunk_specs)
        if not resolved_chunks:
            return VariableRef(dataset=dataset_name, variable=variable_name)

        existing_payload_ids = {
            int(row[0])
            for row in sql_execute(
                cur,
                "select payload_datasetid from variable_chunk where variableid = ?",
                (variable_id,),
            ).fetchall()
        }
        duplicate_payloads = [
            chunk.payload for chunk, payload_id in resolved_chunks if payload_id in existing_payload_ids
        ]
        if duplicate_payloads:
            raise ValueError("Chunk payload is already present: " + ", ".join(duplicate_payloads))

        if resolved_chunks[0][0].chunk_index is None:
            # BEGIN IMMEDIATE prevents another writer from choosing the same next
            # index between this query and the inserts below.
            row = sql_execute(
                cur,
                "select coalesce(max(chunk_index), -1) + 1 from variable_chunk where variableid = ?",
                (variable_id,),
            ).fetchone()
            next_index = int(row[0])
            indexed_chunks = [
                (next_index + offset, payload_id) for offset, (_chunk, payload_id) in enumerate(resolved_chunks)
            ]
        else:
            indexed_chunks = []
            for chunk, payload_id in resolved_chunks:
                if chunk.chunk_index is None:
                    raise AssertionError("Explicit chunk-index validation was not preserved")
                indexed_chunks.append((chunk.chunk_index, payload_id))
            indices = [index for index, _payload_id in indexed_chunks]
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
        duplicate_indices = sorted(index for index, _payload_id in indexed_chunks if index in existing_indices)
        if duplicate_indices:
            raise ValueError("Chunk indices already exist: " + ", ".join(str(index) for index in duplicate_indices))

        # Normalize all mappings before inserting chunks so validation failures
        # cannot leave chunk rows behind, even within the surrounding transaction.
        steps_by_edge = _normalize_source_steps(
            source_steps,
            [(edge_id, label) for edge_id, label, _parent_id in current_edges],
            len(indexed_chunks),
        )
        chunk_ids: list[int] = []
        for chunk_index, payload_id in indexed_chunks:
            result = sql_execute(
                cur,
                "insert into variable_chunk (variableid, chunk_index, payload_datasetid) "
                "values (?, ?, ?) returning chunkid",
                (variable_id, chunk_index, payload_id),
            )
            chunk_ids.append(int(result.fetchone()[0]))
        for edge_id, step_values in steps_by_edge.items():
            for chunk_id, source_step in zip(chunk_ids, step_values, strict=True):
                sql_execute(
                    cur,
                    "insert into variable_chunk_source_step (chunkid, edgeid, source_step) values (?, ?, ?)",
                    (chunk_id, edge_id, source_step),
                )

    return VariableRef(dataset=dataset_name, variable=variable_name)


def set_variable_relationships(  # pylint: disable=too-many-locals
    cur: sqlite3.Cursor,
    con: sqlite3.Connection,
    variable: VariableRef,
    representation_of: Any,
    source_steps: Any = None,
) -> None:
    """Replace the direct parent edges of an existing logical variable.

    Existing source-step mappings belong to the old edge IDs. If the variable is
    temporally mapped, replacement mappings must therefore be supplied alongside
    new parents. Passing no parents clears the relationships and their mappings.
    """
    reference = _normalize_ref(variable, "variable")
    with variable_transaction(con, "variable_relationship_write"):
        variable_id, _dataset_id = _resolve_variable(cur, reference)
        parents = _resolve_parents(cur, representation_of)
        _assert_no_cycle(cur, variable_id, [parent_id for _label, parent_id, _ref in parents])
        current_edges = _edge_rows(cur, variable_id)
        current_pairs = {(label, parent_id) for _edge_id, label, parent_id in current_edges}
        requested_pairs = {(label, parent_id) for label, parent_id, _ref in parents}
        if current_pairs == requested_pairs and source_steps is None:
            return

        chunk_rows = sql_execute(
            cur,
            "select chunkid from variable_chunk where variableid = ? order by chunk_index",
            (variable_id,),
        ).fetchall()
        existing_mapping = sql_execute(
            cur,
            "select 1 from variable_chunk_source_step as mapping "
            "join variable_chunk as chunk on chunk.chunkid = mapping.chunkid "
            "where chunk.variableid = ? limit 1",
            (variable_id,),
        ).fetchone()
        if existing_mapping is not None and source_steps is None and parents:
            raise ValueError("Changing parents of a temporally mapped variable requires replacement source_steps")
        if source_steps is not None and not chunk_rows:
            raise ValueError("source_steps can only be supplied for a variable with chunks")

        # Deleting edges also deletes their source-step rows through the foreign
        # key cascade. New mappings below are attached to the replacement edges.
        sql_execute(
            cur,
            "delete from variable_representation_edge where child_variable_id = ?",
            (variable_id,),
        )
        _insert_edges(cur, variable_id, parents)
        new_edges = _edge_rows(cur, variable_id)
        steps_by_edge = _normalize_source_steps(
            source_steps,
            [(edge_id, label) for edge_id, label, _parent_id in new_edges],
            len(chunk_rows),
        )
        chunk_ids = [int(row["chunkid"]) for row in chunk_rows]
        for edge_id, step_values in steps_by_edge.items():
            for chunk_id, source_step in zip(chunk_ids, step_values, strict=True):
                sql_execute(
                    cur,
                    "insert into variable_chunk_source_step (chunkid, edgeid, source_step) values (?, ?, ?)",
                    (chunk_id, edge_id, source_step),
                )


def _variable_ref_by_id(cur: sqlite3.Cursor, variable_id: int) -> VariableRef:
    row = sql_execute(
        cur,
        "select d.name as dataset_name, lv.name as variable_name "
        "from logical_variable as lv join dataset as d on d.rowid = lv.datasetid "
        "where lv.variableid = ?",
        (variable_id,),
    ).fetchone()
    if row is None:
        raise LookupError(f"Logical variable ID not found: {variable_id}")
    return VariableRef(str(row["dataset_name"]), str(row["variable_name"]))


def variable_delete_impact(cur: sqlite3.Cursor, variable: VariableRef) -> VariableDeleteImpact:
    """Return the transitive graph and preview impact of deleting a variable.

    ``dependent_representations`` contains every downstream representation that
    requires cascading deletion. ``preview_users`` contains variables whose
    preferred preview is the target or one of those descendants.
    """
    reference = _normalize_ref(variable, "variable")
    variable_id, _dataset_id = _resolve_variable(cur, reference)
    descendant_rows = sql_execute(
        cur,
        "with recursive descendants(variableid) as ("
        "select child_variable_id from variable_representation_edge where parent_variable_id = ? "
        "union "
        "select edge.child_variable_id from variable_representation_edge as edge "
        "join descendants on edge.parent_variable_id = descendants.variableid) "
        "select variableid from descendants order by variableid",
        (variable_id,),
    ).fetchall()
    descendant_ids = [int(row[0]) for row in descendant_rows]
    # Preview references are independent of representation edges, so inspect all
    # variables selected for deletion rather than only the requested target.
    affected_preview_ids = [variable_id, *descendant_ids]
    placeholders = ", ".join("?" for _item in affected_preview_ids)
    preview_rows = sql_execute(
        cur,
        f"select variableid from logical_variable where preferred_preview_id in ({placeholders}) order by variableid",
        tuple(affected_preview_ids),
    ).fetchall()
    preview_ids = [int(row[0]) for row in preview_rows]
    return VariableDeleteImpact(
        target=reference,
        dependent_representations=tuple(_variable_ref_by_id(cur, item) for item in descendant_ids),
        preview_users=tuple(_variable_ref_by_id(cur, item) for item in preview_ids),
    )


def delete_variable(
    cur: sqlite3.Cursor,
    con: sqlite3.Connection,
    variable: VariableRef,
    *,
    cascade: bool = False,
) -> VariableDeleteImpact:
    """Delete a logical variable and optionally its downstream representations.

    Without ``cascade``, any representation or preview reference blocks deletion.
    Cascading deletes logical descendants, clears surviving preview references,
    and leaves payload datasets and namespace datasets intact.
    """
    reference = _normalize_ref(variable, "variable")
    with variable_transaction(con, "variable_delete"):
        variable_id, _dataset_id = _resolve_variable(cur, reference)
        impact = variable_delete_impact(cur, reference)
        if not cascade and (impact.dependent_representations or impact.preview_users):
            raise ValueError(
                "Logical variable is still referenced; inspect variable_delete_impact() and use cascade=True"
            )

        delete_ids = {_resolve_variable(cur, item)[0] for item in (impact.target, *impact.dependent_representations)}
        placeholders = ", ".join("?" for _item in delete_ids)
        if delete_ids:
            # Preview users may survive the graph cascade. Clear their references
            # before deleting targets protected by the preview foreign key.
            sql_execute(
                cur,
                f"update logical_variable set preferred_preview_id = null "
                f"where preferred_preview_id in ({placeholders})",
                tuple(delete_ids),
            )

        def delete_descendants_first(item_id: int, visited: set[int]) -> None:
            # Parent deletion is restricted while child edges exist. Post-order
            # traversal satisfies that constraint; visited handles shared DAGs.
            if item_id in visited:
                return
            child_rows = sql_execute(
                cur,
                "select child_variable_id from variable_representation_edge where parent_variable_id = ?",
                (item_id,),
            ).fetchall()
            for row in child_rows:
                delete_descendants_first(int(row[0]), visited)
            sql_execute(cur, "delete from logical_variable where variableid = ?", (item_id,))
            visited.add(item_id)

        delete_descendants_first(variable_id, set())
    return impact
