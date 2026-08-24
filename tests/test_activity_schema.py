import json
from pathlib import Path

import pytest
import yaml

from hpc_campaign import Manager, VariableSpec
from hpc_campaign.schema import (
    SUPPORTED_ACTIVITY_KINDS,
    SchemaInterpretationError,
    interpret_activity_profiles,
    validate_action_spec,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
SAMPLE_DATASET = REPO_ROOT / "data" / "onearray.h5"
_UNSET = object()


def write_schema(tmp_path: Path, profiles=_UNSET, *, version: int = 2, name: str = "activities") -> Path:
    """Write a minimal layout schema with optional action-spec profiles."""
    schema = {
        "schema_version": version,
        "name": name,
        "files": {"output": {"role": "static", "path": "output"}},
    }
    if profiles is not _UNSET:
        schema["activity_profiles"] = profiles
    path = tmp_path / f"{name}.yaml"
    path.write_text(yaml.safe_dump(schema, sort_keys=False), encoding="utf-8")
    return path


def open_campaign(tmp_path: Path, name: str = "profiles.aca") -> tuple[Manager, object]:
    """Create a campaign and register one root data-product entity."""
    manager = Manager(name, campaign_store=str(tmp_path))
    manager.open(create=True, truncate=True)
    manager.data(SAMPLE_DATASET, name="output")
    pressure = manager.add_variable(run="run-1", dataset="output", variable="pressure", primary=True)
    return manager, pressure


def add_reduction(manager: Manager, pressure, *, variable: str, action_spec=None):
    """Record one reduction with a unique output name."""
    return manager.add_activity(
        action="reduction",
        inputs={"source": pressure},
        outputs={"result": VariableSpec(run="run-1", dataset="output", variable=variable)},
        action_spec=action_spec,
    )


def test_version_one_rejects_activity_profiles_and_version_two_keeps_them_optional():
    """Profiles are a version-2 extension, but a version-2 schema need not use them."""
    assert interpret_activity_profiles({"schema_version": 1}) is None
    assert interpret_activity_profiles({"schema_version": 2}) is None
    with pytest.raises(SchemaInterpretationError, match="requires schema_version=2"):
        interpret_activity_profiles({"schema_version": 1, "activity_profiles": {}})


def test_supported_actions_are_the_fixed_initial_vocabulary():
    """The schema can constrain specs but cannot add producer-defined actions."""
    assert SUPPORTED_ACTIVITY_KINDS == {
        "reduction": "transformation",
        "projection": "transformation",
        "quantity_of_interest": "analysis",
        "visualization": "presentation",
    }
    with pytest.raises(SchemaInterpretationError, match="Unsupported activity profile"):
        interpret_activity_profiles({"schema_version": 2, "activity_profiles": {"feature_detection": {}}})


def test_profiles_normalize_action_spec_rules_with_extensible_default():
    """Omitted allow_additional defaults to true for gradual schema adoption."""
    profiles = interpret_activity_profiles(
        {
            "schema_version": 2,
            "activity_profiles": {
                "reduction": {
                    "action_spec": {
                        "required": ["required_key1"],
                        "optional": ["optional_key1"],
                    }
                }
            },
        }
    )
    assert profiles == {
        "reduction": {
            "action_spec": {
                "required": ["required_key1"],
                "optional": ["optional_key1"],
                "allow_additional": True,
            }
        }
    }


@pytest.mark.parametrize(
    ("profiles", "message"),
    [
        (None, "activity_profiles must be a mapping"),
        ([], "activity_profiles must be a mapping"),
        ({"": {}}, "profile names must be non-empty strings"),
        ({1: {}}, "profile names must be non-empty strings"),
        ({"reduction": []}, "activity_profiles.reduction must be a mapping"),
        ({"reduction": {"unknown": {}}}, "unsupported field"),
        (
            {"reduction": {"action_spec": None}},
            "activity_profiles.reduction.action_spec must be a mapping",
        ),
        ({"reduction": {"action_spec": {"unknown": []}}}, "contains unsupported field"),
        ({"reduction": {"action_spec": {"required": "required_key1"}}}, "required must be a list"),
        ({"reduction": {"action_spec": {"optional": [""]}}}, "must be a non-empty string"),
        ({"reduction": {"action_spec": {"required": [1]}}}, "must be a non-empty string"),
        (
            {"reduction": {"action_spec": {"required": ["required_key1", "required_key1"]}}},
            "required contains duplicate key",
        ),
        (
            {
                "reduction": {
                    "action_spec": {
                        "required": ["required_key1"],
                        "optional": ["required_key1"],
                    }
                }
            },
            "both required and optional",
        ),
        (
            {"reduction": {"action_spec": {"allow_additional": "yes"}}},
            "must be a boolean",
        ),
    ],
)
def test_malformed_profiles_have_field_specific_errors(profiles, message):
    """Schema authoring errors fail at the precise activity-profile field."""
    with pytest.raises(SchemaInterpretationError, match=message):
        interpret_activity_profiles({"schema_version": 2, "activity_profiles": profiles})


def test_profile_with_one_required_key_accepts_and_rejects_expected_specs():
    """A single required key is enforced even though action_spec itself is optional generally."""
    profiles = interpret_activity_profiles(
        {
            "schema_version": 2,
            "activity_profiles": {
                "reduction": {"action_spec": {"required": ["required_key1"]}},
            },
        }
    )
    validate_action_spec("reduction", {"required_key1": "value1"}, profiles)
    with pytest.raises(SchemaInterpretationError, match="missing required key.*required_key1"):
        validate_action_spec("reduction", None, profiles)


def test_profile_with_multiple_required_keys_reports_each_missing_key():
    """Every placeholder key in a multiple-required profile is independently required."""
    required = ["required_key1", "required_key2"]
    profiles = interpret_activity_profiles(
        {
            "schema_version": 2,
            "activity_profiles": {
                "visualization": {"action_spec": {"required": required}},
            },
        }
    )
    validate_action_spec(
        "visualization",
        {"required_key1": "value1", "required_key2": "value2"},
        profiles,
    )
    for missing_key in required:
        incomplete = {key: "value" for key in required if key != missing_key}
        with pytest.raises(SchemaInterpretationError, match=rf"missing required key.*{missing_key}"):
            validate_action_spec("visualization", incomplete, profiles)


def test_allow_additional_false_accepts_declared_keys_and_rejects_unknown_keys():
    """A strict profile permits only its required and optional action-spec keys."""
    profiles = interpret_activity_profiles(
        {
            "schema_version": 2,
            "activity_profiles": {
                "reduction": {
                    "action_spec": {
                        "required": ["required_key1"],
                        "optional": ["optional_key1"],
                        "allow_additional": False,
                    }
                }
            },
        }
    )
    declared = {"required_key1": "value1", "optional_key1": "value2"}
    validate_action_spec("reduction", declared, profiles)
    with pytest.raises(SchemaInterpretationError, match="unsupported key.*unexpected_key"):
        validate_action_spec("reduction", {**declared, "unexpected_key": "value3"}, profiles)


def test_profile_omission_leaves_other_supported_action_specs_open():
    """Profiles constrain listed actions without forcing profiles for all supported actions."""
    profiles = interpret_activity_profiles(
        {
            "schema_version": 2,
            "activity_profiles": {"reduction": {"action_spec": {"allow_additional": False}}},
        }
    )
    validate_action_spec("visualization", {"producer_key": "accepted"}, profiles)


def test_action_validation_always_rejects_unknown_actions_and_non_objects():
    """Core action and JSON-object checks apply even when no schema is stored."""
    with pytest.raises(SchemaInterpretationError, match="unsupported action"):
        validate_action_spec("feature_detection", {}, None)
    with pytest.raises(SchemaInterpretationError, match="action_spec must be an object"):
        validate_action_spec("reduction", ["not", "an", "object"], None)
    with pytest.raises(SchemaInterpretationError, match="keys must be non-empty strings"):
        validate_action_spec("reduction", {"": "bad"}, None)


def test_manager_enforces_required_and_closed_profiles_before_mutation(tmp_path: Path):
    """Profile failures cannot leave an output entity or activity behind."""
    manager, pressure = open_campaign(tmp_path)
    manager.set_schema(
        write_schema(
            tmp_path,
            {
                "reduction": {
                    "action_spec": {
                        "required": ["required_key1"],
                        "allow_additional": False,
                    }
                }
            },
        )
    )

    with pytest.raises(SchemaInterpretationError, match="missing required key"):
        add_reduction(manager, pressure, variable="missing", action_spec=None)
    with pytest.raises(SchemaInterpretationError, match="unsupported key"):
        add_reduction(
            manager,
            pressure,
            variable="additional",
            action_spec={"required_key1": "value", "unexpected": "bad"},
        )
    accepted = add_reduction(
        manager,
        pressure,
        variable="accepted",
        action_spec={"required_key1": "value"},
    )

    info = manager.info()
    assert list(info.activities) == [info.find_activity(accepted.activity).id]
    with pytest.raises(LookupError):
        info.find_variable("output", "missing", run="run-1")


def test_setting_candidate_schema_validates_existing_activity_specs(tmp_path: Path):
    """A stricter schema is rejected when an existing action would violate it."""
    manager, pressure = open_campaign(tmp_path)
    add_reduction(manager, pressure, variable="reduced", action_spec={"method": "mgard"})
    strict_schema = write_schema(
        tmp_path,
        {
            "reduction": {
                "action_spec": {
                    "required": ["required_key1"],
                    "allow_additional": False,
                }
            }
        },
        name="strict",
    )

    with pytest.raises(SchemaInterpretationError, match="Activity profile validation failed"):
        manager.set_schema(strict_schema)


def test_validate_schema_reports_activity_profile_verification(tmp_path: Path):
    """Campaign validation reports how many stored activities and specs were checked."""
    manager, pressure = open_campaign(tmp_path)
    manager.set_schema(
        write_schema(
            tmp_path,
            {"reduction": {"action_spec": {"optional": ["method"], "allow_additional": False}}},
        )
    )
    add_reduction(manager, pressure, variable="reduced", action_spec={"method": "mgard"})

    layout = manager.validate_schema()
    assert sorted(layout["activity_profiles"]) == ["reduction"]
    assert layout["activity_validation"] == {
        "enforced": True,
        "activities_checked": 1,
        "specified_activities": 1,
    }


def test_logical_output_namespaces_do_not_confuse_file_layout_validation(tmp_path: Path):
    """Synthetic VARIABLES datasets are provenance namespaces, not schema input files."""
    manager, pressure = open_campaign(tmp_path)
    manager.set_schema(write_schema(tmp_path))
    manager.add_activity(
        action="projection",
        inputs={"source": pressure},
        outputs={"result": VariableSpec(run="run-1", dataset="products", variable="slice")},
    )

    layout = manager.validate_schema()
    assert layout["file_groups"]["output"]["datasets"] == ["output"]


def test_append_without_repeating_spec_validates_the_existing_effective_spec(tmp_path: Path):
    """An append may omit an immutable spec already attached to the generating activity."""
    manager, pressure = open_campaign(tmp_path)
    for name in ("frame-0", "frame-1"):
        path = tmp_path / f"{name}.txt"
        path.write_text(name, encoding="utf-8")
        manager.text(path, name=name, store=True)
    manager.set_schema(
        write_schema(
            tmp_path,
            {"visualization": {"action_spec": {"required": ["required_key1"]}}},
        )
    )
    initial = manager.add_activity(
        action="visualization",
        inputs={"source": pressure},
        outputs={
            "image": VariableSpec(
                run="run-1",
                dataset="images",
                variable="sequence",
                chunks=["frame-0"],
            )
        },
        action_spec={"required_key1": "value"},
        source_steps=[0],
    )
    appended = manager.add_activity(
        action="visualization",
        inputs={"source": pressure},
        outputs={
            "image": VariableSpec(
                run="run-1",
                dataset="images",
                variable="sequence",
                chunks=["frame-1"],
                append=True,
            )
        },
        source_steps=[5],
    )

    assert appended.activity == initial.activity


def test_validation_rejects_corrupted_stored_action_spec_json(tmp_path: Path):
    """Campaign-wide verification does not silently treat malformed stored JSON as absent."""
    manager, pressure = open_campaign(tmp_path)
    result = add_reduction(manager, pressure, variable="reduced", action_spec={"method": "mgard"})
    manager.cur.execute(
        "update action_spec set metadata = ? where specid = (select specid from activity where uuid = ?)",
        ("{not-json", result.activity.uuid),
    )
    manager.con.commit()

    with pytest.raises(SchemaInterpretationError, match="stored action_spec is not valid JSON"):
        manager.set_schema(write_schema(tmp_path))


def test_profile_shape_is_plain_yaml_and_json_compatible(tmp_path: Path):
    """Placeholder profile names can be serialized without custom schema objects."""
    schema_path = write_schema(
        tmp_path,
        {
            "quantity_of_interest": {
                "action_spec": {
                    "required": ["required_key1", "required_key2"],
                    "allow_additional": True,
                }
            }
        },
    )
    parsed = yaml.safe_load(schema_path.read_text(encoding="utf-8"))
    json.dumps(parsed)
    assert parsed["activity_profiles"]["quantity_of_interest"]["action_spec"]["required"] == [
        "required_key1",
        "required_key2",
    ]
