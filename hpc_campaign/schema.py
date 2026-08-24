import fnmatch
import re
from typing import Any, Mapping, Sequence

# This is intentionally a small, controlled vocabulary. The category is
# structural; producer-specific details belong in an optional action_spec.
SUPPORTED_ACTIVITY_KINDS = {
    "reduction": "transformation",
    "projection": "transformation",
    "quantity_of_interest": "analysis",
    "visualization": "presentation",
}


class SchemaInterpretationError(ValueError):
    """Raised when a schema cannot be interpreted against available datasets."""


def interpret_schema_layout(
    schema: Mapping[str, Any],
    datasets: Sequence[str],
    timeseries: Mapping[str, Sequence[str]] | None = None,
) -> dict[str, Any]:
    """
    Interpret a minimal ingestion schema against known campaign dataset names.

    This function resolves file groups, append vs file-per-timestep mode,
    timestep extraction, group-level associations, and time references
    normalized onto time-series groups. It intentionally does not open
    ADIOS/HDF5 data.
    """
    schema_version = _supported_schema_version(schema)
    # Action profiles are campaign-global and independent of file layout.
    interpret_activity_profiles(schema)

    files = _mapping(schema.get("files"), "files")
    dataset_names = [str(name) for name in datasets]
    timeseries_map = {str(name): [str(dataset) for dataset in values] for name, values in (timeseries or {}).items()}

    file_groups: dict[str, dict[str, Any]] = {}
    for group_name, raw_group in files.items():
        group_key = str(group_name)
        group = _mapping(raw_group, f"files.{group_key}")
        file_groups[group_key] = _interpret_file_group(group_key, group, files, dataset_names, timeseries_map)

    _apply_root_time(schema.get("time"), file_groups)

    return {
        "schema_version": schema_version,
        "schema_name": str(schema.get("name", "") or ""),
        "file_groups": file_groups,
    }


def interpret_campaign_schema_layout(
    schema: Mapping[str, Any],
    datasets: Sequence[str],
    timeseries: Mapping[str, Sequence[str]] | None = None,
) -> dict[str, Any]:
    """
    Interpret a schema against campaign dataset names.

    First try the schema at campaign root. If it does not match root datasets,
    apply the same schema independently to each immediate child directory by
    resolving schema paths relative to that directory.
    """
    # Validate global profiles before trying root or per-run layout so a
    # profile error is not misleadingly reported with a run prefix.
    interpret_activity_profiles(schema)
    dataset_names = [str(name) for name in datasets]
    timeseries_map = {str(name): [str(dataset) for dataset in values] for name, values in (timeseries or {}).items()}

    try:
        layout = interpret_schema_layout(schema, dataset_names, timeseries_map)
        layout["scope"] = ""
        return layout
    except SchemaInterpretationError as root_error:
        prefixes = _immediate_child_prefixes(dataset_names)
        if not prefixes:
            raise root_error

    instances = {}
    for prefix in prefixes:
        scoped_datasets = _strip_scope_prefix(prefix, dataset_names)
        scoped_timeseries = _strip_timeseries_scope_prefix(prefix, timeseries_map)
        try:
            scoped_layout = interpret_schema_layout(schema, scoped_datasets, scoped_timeseries)
        except SchemaInterpretationError as exc:
            raise SchemaInterpretationError(f"{prefix}: {exc}") from exc
        instances[prefix] = _prefix_layout_datasets(prefix, scoped_layout)

    return {
        "schema_version": _schema_version(schema),
        "schema_name": str(schema.get("name", "") or ""),
        "instances": instances,
    }


def _immediate_child_prefixes(dataset_names: Sequence[str]) -> list[str]:
    prefixes = {name.split("/", 1)[0] for name in dataset_names if "/" in name and name.split("/", 1)[0]}
    return sorted(prefixes)


def _strip_scope_prefix(prefix: str, dataset_names: Sequence[str]) -> list[str]:
    prefix_slash = f"{prefix}/"
    return [name[len(prefix_slash) :] for name in dataset_names if name.startswith(prefix_slash)]


def _strip_timeseries_scope_prefix(
    prefix: str,
    timeseries: Mapping[str, Sequence[str]],
) -> dict[str, list[str]]:
    prefix_slash = f"{prefix}/"
    scoped = {}
    for name, datasets in timeseries.items():
        scoped_datasets = [dataset[len(prefix_slash) :] for dataset in datasets if dataset.startswith(prefix_slash)]
        if not scoped_datasets:
            continue
        scoped_name = name[len(prefix_slash) :] if name.startswith(prefix_slash) else name
        scoped[scoped_name] = scoped_datasets
    return scoped


def _prefix_layout_datasets(prefix: str, layout: Mapping[str, Any]) -> dict[str, Any]:
    prefixed_layout = dict(layout)
    prefixed_layout["scope"] = prefix
    file_groups = {}
    for group_name, group in layout.get("file_groups", {}).items():
        prefixed_group = dict(group)
        prefixed_group["datasets"] = [f"{prefix}/{dataset}" for dataset in group.get("datasets", [])]
        file_groups[group_name] = prefixed_group
    prefixed_layout["file_groups"] = file_groups
    return prefixed_layout


def _schema_version(schema: Mapping[str, Any]) -> int:
    try:
        return int(schema.get("schema_version", 0))
    except Exception as exc:
        raise SchemaInterpretationError("schema_version must be an integer") from exc


def _supported_schema_version(schema: Mapping[str, Any]) -> int:
    version = _schema_version(schema)
    if version not in {1, 2}:
        raise SchemaInterpretationError(f"Unsupported schema_version={version}; expected 1 or 2")
    return version


def _mapping(value: Any, field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise SchemaInterpretationError(f"{field_name} must be a mapping")
    return value


def _nonempty_string(value: Any, field_name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise SchemaInterpretationError(f"{field_name} is required")
    return text


def _string_list(value: Any, field_name: str) -> list[str]:
    """Return a validated list of unique, non-empty schema identifiers."""
    if value is None:
        return []
    if not isinstance(value, list):
        raise SchemaInterpretationError(f"{field_name} must be a list")

    result: list[str] = []
    seen: set[str] = set()
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            raise SchemaInterpretationError(f"{field_name}[{index}] must be a non-empty string")
        key = item.strip()
        if key in seen:
            raise SchemaInterpretationError(f"{field_name} contains duplicate key: {key}")
        seen.add(key)
        result.append(key)
    return result


def interpret_activity_profiles(schema: Mapping[str, Any]) -> dict[str, dict[str, Any]] | None:
    """Normalize optional schema rules for activity action specifications.

    The activity vocabulary is fixed by :data:`SUPPORTED_ACTIVITY_KINDS`.
    Profiles do not add new actions; they only constrain keys in an action's
    optional ``action_spec`` object.
    """
    version = _supported_schema_version(schema)
    if "representation_profiles" in schema:
        raise SchemaInterpretationError(
            "representation_profiles is not supported by the activity-based provenance schema; use activity_profiles"
        )
    if version == 1:
        if "activity_profiles" in schema:
            raise SchemaInterpretationError("activity_profiles requires schema_version=2")
        return None
    if "activity_profiles" not in schema:
        return None

    raw_profiles = _mapping(schema.get("activity_profiles"), "activity_profiles")
    profiles: dict[str, dict[str, Any]] = {}
    for raw_action, raw_profile in raw_profiles.items():
        if not isinstance(raw_action, str) or not raw_action.strip():
            raise SchemaInterpretationError("activity profile names must be non-empty strings")
        action = raw_action.strip()
        if action not in SUPPORTED_ACTIVITY_KINDS:
            allowed = ", ".join(SUPPORTED_ACTIVITY_KINDS)
            raise SchemaInterpretationError(f"Unsupported activity profile {action!r}; allowed actions: {allowed}")
        if action in profiles:
            raise SchemaInterpretationError(f"Duplicate activity profile: {action}")
        profiles[action] = _interpret_activity_profile(action, raw_profile)
    return profiles


def _interpret_activity_profile(action: str, raw_profile: Any) -> dict[str, Any]:
    """Normalize one profile after its activity action has been validated."""
    field_name = f"activity_profiles.{action}"
    profile = _mapping(raw_profile, field_name)
    unknown_profile_fields = sorted(str(key) for key in profile if key != "action_spec")
    if unknown_profile_fields:
        raise SchemaInterpretationError(
            f"{field_name} contains unsupported field(s): {', '.join(unknown_profile_fields)}"
        )

    spec_field = f"{field_name}.action_spec"
    spec_rules = _mapping(profile.get("action_spec", {}), spec_field)
    supported_spec_fields = {"required", "optional", "allow_additional"}
    unknown_spec_fields = sorted(str(key) for key in spec_rules if key not in supported_spec_fields)
    if unknown_spec_fields:
        raise SchemaInterpretationError(f"{spec_field} contains unsupported field(s): {', '.join(unknown_spec_fields)}")

    required = _string_list(spec_rules.get("required"), f"{spec_field}.required")
    optional = _string_list(spec_rules.get("optional"), f"{spec_field}.optional")
    overlap = sorted(set(required).intersection(optional))
    if overlap:
        raise SchemaInterpretationError(f"{spec_field} keys cannot be both required and optional: {', '.join(overlap)}")

    allow_additional = spec_rules.get("allow_additional", True)
    if not isinstance(allow_additional, bool):
        raise SchemaInterpretationError(f"{spec_field}.allow_additional must be a boolean")
    return {
        "action_spec": {
            "required": required,
            "optional": optional,
            "allow_additional": allow_additional,
        }
    }


def validate_action_spec(
    action: str,
    action_spec: Any,
    profiles: Mapping[str, Mapping[str, Any]] | None,
    *,
    identity: str = "activity",
) -> None:
    """Validate one action and its optional specification against the schema."""
    action_name = str(action or "").strip()
    if action_name not in SUPPORTED_ACTIVITY_KINDS:
        allowed = ", ".join(SUPPORTED_ACTIVITY_KINDS)
        raise SchemaInterpretationError(f"{identity}: unsupported action={action_name!r}; allowed actions: {allowed}")

    if action_spec is None:
        specification: Mapping[str, Any] = {}
    elif isinstance(action_spec, Mapping):
        specification = action_spec
    else:
        raise SchemaInterpretationError(f"{identity}: action_spec must be an object")

    invalid_keys = sorted(repr(key) for key in specification if not isinstance(key, str) or not key.strip())
    if invalid_keys:
        raise SchemaInterpretationError(
            f"{identity}: action_spec keys must be non-empty strings: {', '.join(invalid_keys)}"
        )

    # An omitted profile leaves the specification producer-defined. This keeps
    # profiles optional while the activity vocabulary remains controlled.
    profile = profiles.get(action_name) if profiles is not None else None
    if profile is None:
        return

    spec_rules = profile["action_spec"]
    spec_keys = set(specification)
    required_keys = set(spec_rules["required"])
    missing = sorted(required_keys - spec_keys)
    if missing:
        raise SchemaInterpretationError(
            f"{identity}: action_spec is missing required key(s) for {action_name!r}: {', '.join(missing)}"
        )

    if not spec_rules["allow_additional"]:
        allowed_keys = required_keys.union(spec_rules["optional"])
        additional = sorted(spec_keys - allowed_keys)
        if additional:
            raise SchemaInterpretationError(
                f"{identity}: action_spec contains unsupported key(s) for {action_name!r}: " + ", ".join(additional)
            )


def validate_campaign_activities(
    profiles: Mapping[str, Mapping[str, Any]] | None,
    activities: Sequence[tuple[str, str, Any]],
) -> dict[str, Any]:
    """Validate stored activities and return a compact verification report."""
    ordered = sorted(activities, key=lambda item: item[0])
    errors: list[str] = []
    specified = 0
    for activity_uuid, action, action_spec in ordered:
        if action_spec is not None:
            specified += 1
        try:
            validate_action_spec(action, action_spec, profiles, identity=f"activity {activity_uuid}")
        except SchemaInterpretationError as exc:
            errors.append(str(exc))

    if errors:
        raise SchemaInterpretationError("Activity profile validation failed:\n- " + "\n- ".join(errors))
    return {
        "enforced": profiles is not None,
        "activities_checked": len(ordered),
        "specified_activities": specified,
    }


def _interpret_file_group(
    group_name: str,
    group: Mapping[str, Any],
    all_groups: Mapping[str, Any],
    dataset_names: Sequence[str],
    timeseries: Mapping[str, Sequence[str]],
) -> dict[str, Any]:
    role = _nonempty_string(group.get("role"), f"files.{group_name}.role")
    if role not in {"static", "time_series"}:
        raise SchemaInterpretationError(f"Unsupported files.{group_name}.role={role!r}")

    associations = _interpret_associations(group_name, group.get("associations", {}), all_groups)

    result: dict[str, Any]
    if role == "static":
        if "time" in group:
            raise SchemaInterpretationError(f"files.{group_name}.time is only valid for time_series groups")
        result = {
            "role": role,
            "mode": "none",
            "datasets": _resolve_static_datasets(group_name, group, dataset_names),
        }
    else:
        mode = _nonempty_string(group.get("mode"), f"files.{group_name}.mode")
        if mode == "append":
            result = {
                "role": role,
                "mode": mode,
                "datasets": [_resolve_path_dataset(group_name, group, dataset_names)],
            }
        elif mode == "file_per_timestep":
            datasets = _resolve_file_per_timestep_datasets(group_name, group, dataset_names, timeseries)
            result = {
                "role": role,
                "mode": mode,
                "datasets": datasets,
                "step_indices": _extract_step_indices(group_name, group, datasets),
            }
        else:
            raise SchemaInterpretationError(f"Unsupported files.{group_name}.mode={mode!r}")

    if associations:
        result["associations"] = associations
    if "time" in group:
        result["time"] = _interpret_group_time(group.get("time"), f"files.{group_name}.time")
    return result


def _resolve_static_datasets(group_name: str, group: Mapping[str, Any], dataset_names: Sequence[str]) -> list[str]:
    if group.get("path"):
        return [_resolve_path_dataset(group_name, group, dataset_names)]
    pattern = _nonempty_string(group.get("pattern"), f"files.{group_name}.path or files.{group_name}.pattern")
    matches = sorted(name for name in dataset_names if fnmatch.fnmatch(name, pattern))
    if not matches:
        raise SchemaInterpretationError(f"files.{group_name}.pattern matched no datasets: {pattern}")
    return matches


def _resolve_path_dataset(group_name: str, group: Mapping[str, Any], dataset_names: Sequence[str]) -> str:
    path = _nonempty_string(group.get("path"), f"files.{group_name}.path")
    if path not in dataset_names:
        raise SchemaInterpretationError(f"files.{group_name}.path does not match a dataset: {path}")
    return path


def _resolve_file_per_timestep_datasets(
    group_name: str,
    group: Mapping[str, Any],
    dataset_names: Sequence[str],
    timeseries: Mapping[str, Sequence[str]],
) -> list[str]:
    pattern = _nonempty_string(group.get("pattern"), f"files.{group_name}.pattern")
    matches = {name for name in dataset_names if fnmatch.fnmatch(name, pattern)}
    if not matches:
        raise SchemaInterpretationError(f"files.{group_name}.pattern matched no datasets: {pattern}")

    if group_name not in timeseries:
        return sorted(matches)

    ordered = []
    for dataset in timeseries[group_name]:
        if dataset not in dataset_names:
            raise SchemaInterpretationError(f"timeseries.{group_name} references missing dataset: {dataset}")
        if dataset not in matches:
            raise SchemaInterpretationError(
                f"timeseries.{group_name} dataset does not match files.{group_name}.pattern: {dataset}"
            )
        ordered.append(dataset)
    if not ordered:
        raise SchemaInterpretationError(f"timeseries.{group_name} is empty")
    return ordered


def _extract_step_indices(group_name: str, group: Mapping[str, Any], datasets: Sequence[str]) -> list[int]:
    pattern = _nonempty_string(group.get("step_from_filename"), f"files.{group_name}.step_from_filename")
    try:
        regex = re.compile(pattern)
    except re.error as exc:
        raise SchemaInterpretationError(f"Invalid files.{group_name}.step_from_filename regex: {exc}") from exc

    steps = []
    for dataset in datasets:
        match = regex.search(dataset)
        if match is None:
            raise SchemaInterpretationError(f"files.{group_name}.step_from_filename did not match dataset: {dataset}")
        if not match.groups():
            raise SchemaInterpretationError(f"files.{group_name}.step_from_filename must capture a step number")
        try:
            steps.append(int(match.group(1)))
        except Exception as exc:
            raise SchemaInterpretationError(
                f"files.{group_name}.step_from_filename captured a non-integer step for {dataset}: {match.group(1)}"
            ) from exc
    return steps


def _interpret_associations(
    group_name: str,
    associations: Any,
    all_groups: Mapping[str, Any],
) -> dict[str, str]:
    if associations in (None, {}):
        return {}
    assoc_map = _mapping(associations, f"files.{group_name}.associations")
    result = {}
    for role, target in assoc_map.items():
        target_group = _nonempty_string(target, f"files.{group_name}.associations.{role}")
        if target_group not in all_groups:
            raise SchemaInterpretationError(
                f"files.{group_name}.associations.{role} references unknown group: {target_group}"
            )
        result[str(role)] = target_group
    return result


def _interpret_time_fields(time_spec: Any, field_name: str) -> dict[str, str]:
    time_map = _mapping(time_spec, field_name)
    variable = str(time_map.get("variable", "") or "").strip()
    index = str(time_map.get("index", "") or "").strip()
    has_variable = bool(variable)
    has_index = bool(index)
    if has_variable == has_index:
        raise SchemaInterpretationError(f"{field_name} requires exactly one of variable or index")

    if has_variable:
        return {"variable": variable}
    return {"index": index}


def _interpret_group_time(time_spec: Any, field_name: str) -> dict[str, str]:
    time_map = _mapping(time_spec, field_name)
    if "file" in time_map:
        raise SchemaInterpretationError(f"{field_name}.file is not supported; file group is implicit")
    return _interpret_time_fields(time_map, field_name)


def _interpret_root_time(time_spec: Any, file_groups: Mapping[str, dict[str, Any]]) -> dict[str, str]:
    time_map = _mapping(time_spec, "time")
    result = _interpret_time_fields(time_map, "time")
    if "file" in time_map:
        file_group = _nonempty_string(time_map.get("file"), "time.file")
        group = file_groups.get(file_group)
        if group is None:
            raise SchemaInterpretationError(f"time.file references unknown group: {file_group}")
        if group.get("role") != "time_series":
            raise SchemaInterpretationError(f"time.file references non-time_series group: {file_group}")
        result["file"] = file_group
    return result


def _apply_root_time(time_spec: Any, file_groups: dict[str, dict[str, Any]]) -> None:
    if time_spec in (None, {}):
        return

    root_time = _interpret_root_time(time_spec, file_groups)
    root_group = root_time.get("file", "")
    group_time = {key: value for key, value in root_time.items() if key != "file"}

    if root_group:
        file_groups[root_group].setdefault("time", dict(group_time))
        return

    for group in file_groups.values():
        if group.get("role") == "time_series":
            group.setdefault("time", dict(group_time))
