"""Generic alternate representations of campaign data.

A representation is a derived encoding of one or more ground-truth campaign
variables.  It is intentionally independent of rendering: SCALAR_FIELD and
GAUSSIAN_SPLAT items remain scalar data that a consumer may render, contour,
sample, or otherwise process.
"""

from __future__ import annotations

import json
import math
import sqlite3
from typing import Any

from .utils import sql_commit, sql_execute

SCALAR_FIELD_FORMAT = "SCALAR_FIELD"
GAUSSIAN_SPLAT_FORMAT = "GAUSSIAN_SPLAT"
SUPPORTED_DATA_REPRESENTATION_FORMATS = {
    SCALAR_FIELD_FORMAT,
    GAUSSIAN_SPLAT_FORMAT,
}


def ensure_representation_tables(cur: sqlite3.Cursor, con: sqlite3.Connection) -> None:
    """Create the additive representation schema when it is not present."""
    sql_execute(
        cur,
        "create table if not exists representation"
        + "(repid INTEGER PRIMARY KEY, name TEXT UNIQUE, field_name TEXT, format TEXT, "
        + "temporal_interpolation TEXT, parameter_correspondence TEXT, metadata TEXT)",
    )
    sql_execute(
        cur,
        "create table if not exists representation_source"
        + "(sourceid INTEGER PRIMARY KEY, repid INT, datasetid INT, variable_name TEXT, "
        + "label TEXT, metadata TEXT, UNIQUE(repid, label))",
    )
    sql_execute(
        cur,
        "create table if not exists representation_item"
        + "(itemid INTEGER PRIMARY KEY, repid INT, item_order INT, datasetid INT, "
        + "logical_time REAL, metadata TEXT, UNIQUE(repid, item_order), UNIQUE(repid, datasetid))",
    )
    sql_execute(
        cur,
        "create table if not exists representation_item_source"
        + "(itemid INT, sourceid INT, selection TEXT, PRIMARY KEY(itemid, sourceid))",
    )
    sql_execute(
        cur,
        "create table if not exists representation_metric"
        + "(metricid INTEGER PRIMARY KEY, repid INT, itemid INT, name TEXT, value REAL, "
        + "units TEXT, norm TEXT, relative INT, metadata TEXT)",
    )
    sql_commit(con)


def _serialize_json_object(value: Any, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise TypeError(f"{label} must be a dictionary")
    try:
        return json.dumps(value, sort_keys=True, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be JSON serializable and contain only finite numbers") from exc


def _deserialize_json_object(value: str | None, label: str) -> dict:
    if not value:
        return {}
    try:
        result = json.loads(value)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ValueError(f"{label} is not valid JSON") from exc
    if not isinstance(result, dict):
        raise ValueError(f"{label} must contain a JSON object")
    return result


def _resolve_live_dataset(cur: sqlite3.Cursor, dataset_name: str) -> tuple[int, str, str]:
    res = sql_execute(
        cur,
        "select rowid, uuid, fileformat from dataset where name = ? and deltime = 0",
        (dataset_name,),
    )
    row = res.fetchone()
    if row is None:
        raise LookupError(f"Dataset not found or deleted: {dataset_name}")
    return int(row[0]), str(row[1]), str(row[2])


def _resolve_representation(cur: sqlite3.Cursor, representation: int | str) -> tuple[int, str, str]:
    if isinstance(representation, int):
        res = sql_execute(
            cur,
            "select repid, name, format from representation where repid = ?",
            (representation,),
        )
    else:
        name = str(representation or "").strip()
        if not name:
            raise ValueError("representation name must not be empty")
        res = sql_execute(
            cur,
            "select repid, name, format from representation where name = ?",
            (name,),
        )
    row = res.fetchone()
    if row is None:
        raise LookupError(f"Representation not found: {representation}")
    return int(row[0]), str(row[1]), str(row[2])


def _normalize_source_specs(sources) -> list[dict]:
    if not sources:
        raise ValueError("A representation requires at least one source variable")

    source_list = [sources] if isinstance(sources, dict) else list(sources)
    normalized: list[dict] = []
    labels: set[str] = set()
    for source in source_list:
        if not isinstance(source, dict):
            raise TypeError(f"Representation source must be a dictionary: {source!r}")
        dataset_name = str(source.get("dataset", source.get("source_dataset", "")) or "").strip()
        variable_name = str(source.get("variable", source.get("variable_name", "")) or "").strip()
        label = str(source.get("label", variable_name) or variable_name).strip()
        if not dataset_name or not variable_name:
            raise ValueError(f"Representation source requires dataset and variable: {source!r}")
        if not label:
            raise ValueError(f"Representation source requires a non-empty label: {source!r}")
        if label in labels:
            raise ValueError(f"Representation source labels must be unique: {label}")
        labels.add(label)
        normalized.append(
            {
                "dataset": dataset_name,
                "variable": variable_name,
                "label": label,
                "metadata": source.get("metadata"),
            }
        )
    return normalized


def create_representation(  # pylint: disable=too-many-arguments,too-many-locals
    cur: sqlite3.Cursor,
    con: sqlite3.Connection,
    *,
    name: str,
    representation_format: str,
    sources,
    field_name: str | None = None,
    temporal_interpolation: str = "none",
    parameter_correspondence: str | None = None,
    metadata: dict | None = None,
    replace: bool = False,
) -> int:
    """Create a representation and its ground-truth source associations."""
    ensure_representation_tables(cur, con)
    representation_name = str(name or "").strip()
    if not representation_name:
        raise ValueError("representation name must not be empty")

    resolved_format = str(representation_format or "").strip().upper()
    if resolved_format not in SUPPORTED_DATA_REPRESENTATION_FORMATS:
        supported = ", ".join(sorted(SUPPORTED_DATA_REPRESENTATION_FORMATS))
        raise ValueError(f"Unsupported representation format {resolved_format!r}; supported formats: {supported}")

    source_specs = _normalize_source_specs(sources)
    resolved_field_name = str(field_name or "").strip()
    if not resolved_field_name:
        if len(source_specs) == 1:
            resolved_field_name = source_specs[0]["variable"]
        else:
            raise ValueError("field_name is required when a representation has multiple source variables")

    interpolation = str(temporal_interpolation or "").strip().lower()
    if not interpolation:
        raise ValueError("temporal_interpolation must be explicit")
    if interpolation not in {"none", "linear"}:
        raise ValueError("temporal_interpolation must be 'none' or 'linear'")
    correspondence = str(parameter_correspondence or "").strip().lower()
    if not correspondence:
        correspondence = "stable-index" if resolved_format == GAUSSIAN_SPLAT_FORMAT else "grid-index"
    expected_correspondence = "stable-index" if resolved_format == GAUSSIAN_SPLAT_FORMAT else "grid-index"
    if correspondence != expected_correspondence:
        raise ValueError(
            f"{resolved_format} representation parameter_correspondence must be {expected_correspondence!r}"
        )

    source_rows: list[dict] = []
    for source in source_specs:
        dataset_id, _dataset_uuid, _file_format = _resolve_live_dataset(cur, source["dataset"])
        source_rows.append(
            {
                **source,
                "dataset_id": dataset_id,
                "metadata_json": _serialize_json_object(
                    source["metadata"],
                    "representation source metadata",
                ),
            }
        )
    representation_metadata_json = _serialize_json_object(metadata, "representation metadata")

    res = sql_execute(cur, "select repid from representation where name = ?", (representation_name,))
    row = res.fetchone()
    if row is not None:
        repid = int(row[0])
        if not replace:
            raise ValueError(f"Representation already exists: {representation_name}")
        item_rows = sql_execute(cur, "select itemid from representation_item where repid = ?", (repid,)).fetchall()
        for item_row in item_rows:
            sql_execute(cur, "delete from representation_item_source where itemid = ?", (int(item_row[0]),))
        sql_execute(cur, "delete from representation_metric where repid = ?", (repid,))
        sql_execute(cur, "delete from representation_item where repid = ?", (repid,))
        sql_execute(cur, "delete from representation_source where repid = ?", (repid,))
        sql_execute(
            cur,
            "update representation set field_name = ?, format = ?, temporal_interpolation = ?, "
            "parameter_correspondence = ?, metadata = ? where repid = ?",
            (
                resolved_field_name,
                resolved_format,
                interpolation,
                correspondence,
                representation_metadata_json,
                repid,
            ),
        )
    else:
        cur_representation = sql_execute(
            cur,
            "insert into representation "
            "(name, field_name, format, temporal_interpolation, parameter_correspondence, metadata) "
            "values (?, ?, ?, ?, ?, ?) returning repid",
            (
                representation_name,
                resolved_field_name,
                resolved_format,
                interpolation,
                correspondence,
                representation_metadata_json,
            ),
        )
        repid = int(cur_representation.fetchone()[0])

    for source in source_rows:
        sql_execute(
            cur,
            "insert into representation_source "
            "(repid, datasetid, variable_name, label, metadata) values (?, ?, ?, ?, ?)",
            (
                repid,
                source["dataset_id"],
                source["variable"],
                source["label"],
                source["metadata_json"],
            ),
        )
    sql_commit(con)
    return repid


def _normalize_source_selection(selection: Any) -> dict:
    if selection is None:
        return {}
    if isinstance(selection, int):
        return {"step": selection}
    if not isinstance(selection, dict):
        raise TypeError(f"Source selection must be an integer step or dictionary: {selection!r}")
    normalized = dict(selection)
    if "step" not in normalized:
        raise ValueError(f"Source selection must contain an explicit step: {selection!r}")
    raw_step = normalized["step"]
    if isinstance(raw_step, bool):
        raise ValueError(f"Source selection step must be an integer: {selection!r}")
    try:
        normalized["step"] = int(raw_step)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Source selection step must be an integer: {selection!r}") from exc
    if isinstance(raw_step, float) and not raw_step.is_integer():
        raise ValueError(f"Source selection step must be an integer: {selection!r}")
    if normalized["step"] < 0:
        raise ValueError(f"Source selection step must be non-negative: {selection!r}")
    if "time" in normalized:
        try:
            normalized["time"] = float(normalized["time"])
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Source selection time must be numeric: {selection!r}") from exc
        if not math.isfinite(normalized["time"]):
            raise ValueError(f"Source selection time must be finite: {selection!r}")
    return normalized


def _source_rows_for_representation(cur: sqlite3.Cursor, repid: int) -> list[sqlite3.Row]:
    return sql_execute(
        cur,
        "select sourceid, label from representation_source where repid = ? order by sourceid",
        (repid,),
    ).fetchall()


def _normalize_item_source_selections(
    source_rows: list[sqlite3.Row],
    source_selections,
    source_step: int | None,
    source_time: float | None,
) -> dict[int, dict]:
    common_selection: dict[str, Any] = {}
    if source_step is not None:
        common_selection["step"] = source_step
    if source_time is not None:
        common_selection["time"] = source_time

    by_label: dict[str, Any] = {}
    if source_selections is not None:
        if not isinstance(source_selections, dict):
            raise TypeError("source_selections must map source labels to selection dictionaries")
        by_label = dict(source_selections)
        if source_step is not None or source_time is not None:
            raise ValueError("source_selections cannot be combined with source_step or source_time")

    known_labels = {str(row["label"]) for row in source_rows}
    unknown_labels = sorted(set(by_label) - known_labels)
    if unknown_labels:
        raise ValueError(f"Unknown representation source labels: {', '.join(unknown_labels)}")
    if by_label:
        missing_labels = sorted(known_labels - set(by_label))
        if missing_labels:
            raise ValueError(f"Missing selections for representation sources: {', '.join(missing_labels)}")

    normalized: dict[int, dict] = {}
    for row in source_rows:
        label = str(row["label"])
        selection = _normalize_source_selection(by_label[label]) if by_label else dict(common_selection)
        selection = _normalize_source_selection(selection)
        normalized[int(row["sourceid"])] = selection
    return normalized


def _load_specialized_metadata(
    cur: sqlite3.Cursor,
    datasetid: int,
    representation_format: str,
    dataset_name: str,
) -> dict:
    table_name = {
        SCALAR_FIELD_FORMAT: "scalar_field",
        GAUSSIAN_SPLAT_FORMAT: "gaussian_splat",
    }[representation_format]
    row = sql_execute(
        cur,
        f"select metadata from {table_name} where datasetid = ?",
        (datasetid,),
    ).fetchone()
    if row is None or not row[0]:
        raise ValueError(f"{representation_format} dataset is missing representation metadata: {dataset_name}")
    return _deserialize_json_object(row[0], f"{representation_format} metadata for {dataset_name}")


def _canonical_json_value(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _representation_item_signature(
    representation_format: str,
    metadata: dict,
    dataset_name: str,
) -> tuple[str, ...]:
    keys: tuple[str, ...]
    required: tuple[str, ...]
    if representation_format == SCALAR_FIELD_FORMAT:
        keys = (
            "rank",
            "shape",
            "dtype",
            "byte_order",
            "layout",
            "encoding",
            "compression",
            "value_encoding",
            "coordinate_space",
            "coordinate_transform",
            "value_space",
            "value_transform",
        )
        try:
            shape = tuple(int(value) for value in metadata.get("shape", []))
            rank = int(metadata.get("rank", len(shape)))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"SCALAR_FIELD dataset has invalid rank or shape: {dataset_name}") from exc
        if rank != 2 or len(shape) != 2 or any(value <= 0 for value in shape):
            raise ValueError(f"SCALAR_FIELD representation items must have a positive rank-2 shape: {dataset_name}")
        normalized = dict(metadata)
        normalized["rank"] = rank
        normalized["shape"] = shape
        required = keys[:8]
    elif representation_format == GAUSSIAN_SPLAT_FORMAT:
        keys = (
            "format_version",
            "model",
            "spatial_dimensions",
            "count",
            "dtype",
            "byte_order",
            "layout",
            "encoding",
            "compression",
            "scale_encoding",
            "angle_units",
            "coordinate_order",
            "kernel",
            "rotation_convention",
            "reconstruction",
            "coordinate_space",
            "coordinate_transform",
            "value_space",
            "value_transform",
        )
        normalized = dict(metadata)
        try:
            normalized["format_version"] = int(metadata.get("format_version", 0))
            normalized["spatial_dimensions"] = int(metadata.get("spatial_dimensions", 0))
            normalized["count"] = int(metadata.get("count", 0))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"GAUSSIAN_SPLAT dataset has invalid dimensions or count: {dataset_name}") from exc
        if normalized["spatial_dimensions"] != 2 or normalized["count"] <= 0:
            raise ValueError(f"GAUSSIAN_SPLAT representation items must be non-empty 2D fields: {dataset_name}")
        required = keys
    else:
        raise ValueError(f"Unsupported representation format: {representation_format}")

    missing = [key for key in required if key not in normalized or normalized[key] in ("", None, (), [])]
    if missing:
        raise ValueError(
            f"{representation_format} dataset metadata is missing {', '.join(missing)}: {dataset_name}"
        )
    return tuple(_canonical_json_value(normalized.get(key)) for key in keys)


def _first_item_signature(cur: sqlite3.Cursor, repid: int, representation_format: str) -> tuple | None:
    row = sql_execute(
        cur,
        "select ri.datasetid, d.name from representation_item as ri "
        "join dataset as d on d.rowid = ri.datasetid "
        "where ri.repid = ? order by ri.item_order limit 1",
        (repid,),
    ).fetchone()
    if row is None:
        return None
    metadata = _load_specialized_metadata(cur, int(row["datasetid"]), representation_format, str(row["name"]))
    return _representation_item_signature(representation_format, metadata, str(row["name"]))


def _normalize_metric_specs(metrics) -> list[dict]:
    if metrics is None:
        return []
    metric_list = [metrics] if isinstance(metrics, dict) else list(metrics)
    normalized = []
    for metric in metric_list:
        if not isinstance(metric, dict):
            raise TypeError(f"Representation metric must be a dictionary: {metric!r}")
        name = str(metric.get("name", "") or "").strip()
        if not name:
            raise ValueError(f"Representation metric requires a name: {metric!r}")
        if "value" not in metric:
            raise ValueError(f"Representation metric requires a value: {metric!r}")
        metric_value = float(metric["value"])
        if not math.isfinite(metric_value):
            raise ValueError(f"Representation metric value must be finite: {metric!r}")
        normalized.append(
            {
                "name": name,
                "value": metric_value,
                "units": str(metric.get("units", "") or "").strip() or None,
                "norm": str(metric.get("norm", "") or "").strip() or None,
                "relative": bool(metric.get("relative", False)),
                "metadata": metric.get("metadata"),
            }
        )
    return normalized


def _insert_metric(
    cur: sqlite3.Cursor,
    repid: int,
    itemid: int | None,
    metric: dict,
) -> int:
    result = sql_execute(
        cur,
        "insert into representation_metric "
        "(repid, itemid, name, value, units, norm, relative, metadata) values (?, ?, ?, ?, ?, ?, ?, ?) "
        "returning metricid",
        (
            repid,
            itemid,
            metric["name"],
            metric["value"],
            metric["units"],
            metric["norm"],
            int(metric["relative"]),
            _serialize_json_object(metric["metadata"], "representation metric metadata"),
        ),
    )
    return int(result.fetchone()[0])


def append_representation_item(  # pylint: disable=too-many-arguments,too-many-locals
    cur: sqlite3.Cursor,
    con: sqlite3.Connection,
    *,
    representation: int | str,
    dataset: str,
    logical_time: float | None = None,
    source_selections=None,
    source_step: int | None = None,
    source_time: float | None = None,
    metrics=None,
    metadata: dict | None = None,
    item_order: int | None = None,
) -> int:
    """Append one independently addressable timestep to a representation."""
    ensure_representation_tables(cur, con)
    repid, representation_name, representation_format = _resolve_representation(cur, representation)
    dataset_id, _dataset_uuid, dataset_format = _resolve_live_dataset(cur, dataset)
    if dataset_format != representation_format:
        raise ValueError(
            f"Representation {representation_name} requires {representation_format} items, "
            f"but dataset {dataset} has format {dataset_format}"
        )

    item_metadata = _load_specialized_metadata(cur, dataset_id, representation_format, dataset)
    item_signature = _representation_item_signature(representation_format, item_metadata, dataset)
    first_signature = _first_item_signature(cur, repid, representation_format)
    if first_signature is not None and item_signature != first_signature:
        raise ValueError(
            f"{representation_format} dataset {dataset} is not compatible with the first item "
            f"in representation {representation_name}"
        )

    if item_order is None:
        row = sql_execute(
            cur,
            "select coalesce(max(item_order), -1) + 1 from representation_item where repid = ?",
            (repid,),
        ).fetchone()
        resolved_item_order = int(row[0])
    else:
        resolved_item_order = int(item_order)
        if resolved_item_order < 0:
            raise ValueError("representation item_order must be non-negative")

    source_rows = _source_rows_for_representation(cur, repid)
    selections = _normalize_item_source_selections(source_rows, source_selections, source_step, source_time)
    selection_json = {
        sourceid: json.dumps(selection, sort_keys=True, allow_nan=False)
        for sourceid, selection in selections.items()
    }
    normalized_metrics = _normalize_metric_specs(metrics)
    for metric in normalized_metrics:
        _serialize_json_object(metric["metadata"], "representation metric metadata")
    item_metadata_json = _serialize_json_object(metadata, "representation item metadata")
    resolved_logical_time = float(logical_time) if logical_time is not None else None
    if resolved_logical_time is None:
        selection_times = {float(selection["time"]) for selection in selections.values() if "time" in selection}
        if len(selection_times) == 1:
            resolved_logical_time = float(next(iter(selection_times)))
        elif len(selection_times) > 1:
            raise ValueError("logical_time is required when source selections have different physical times")
    if resolved_logical_time is not None and not math.isfinite(resolved_logical_time):
        raise ValueError("representation logical_time must be finite")

    cur_item = sql_execute(
        cur,
        "insert into representation_item "
        "(repid, item_order, datasetid, logical_time, metadata) values (?, ?, ?, ?, ?) returning itemid",
        (
            repid,
            resolved_item_order,
            dataset_id,
            resolved_logical_time,
            item_metadata_json,
        ),
    )
    itemid = int(cur_item.fetchone()[0])
    for sourceid, serialized_selection in selection_json.items():
        sql_execute(
            cur,
            "insert into representation_item_source (itemid, sourceid, selection) values (?, ?, ?)",
            (itemid, sourceid, serialized_selection),
        )
    for metric in normalized_metrics:
        _insert_metric(cur, repid, itemid, metric)
    sql_commit(con)
    return itemid


def add_representation_metric(  # pylint: disable=too-many-arguments
    cur: sqlite3.Cursor,
    con: sqlite3.Connection,
    *,
    representation: int | str,
    name: str,
    value: float,
    units: str | None = None,
    norm: str | None = None,
    relative: bool = False,
    metadata: dict | None = None,
) -> int:
    """Add an aggregate accuracy or quality metric to a representation."""
    ensure_representation_tables(cur, con)
    repid, _representation_name, _representation_format = _resolve_representation(cur, representation)
    metric = _normalize_metric_specs(
        {
            "name": name,
            "value": value,
            "units": units,
            "norm": norm,
            "relative": relative,
            "metadata": metadata,
        }
    )[0]
    metricid = _insert_metric(cur, repid, None, metric)
    sql_commit(con)
    return metricid
