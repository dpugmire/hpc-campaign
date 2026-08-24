#!/usr/bin/env python3

# pylint: disable=too-many-lines
# pylint: disable=import-error
# pylint: disable=too-many-arguments
# pylint: disable=too-many-locals
# pylint: disable=unused-argument
# pylint: disable=too-many-positional-arguments

import argparse
import glob
import json
import re
import sqlite3
import sys
import zlib
from hashlib import sha1
from io import BytesIO
from os import getcwd
from os.path import exists
from pathlib import Path
from time import time_ns
from typing import Any, Mapping

import nacl.secret
import yaml
from PIL import Image as PILImage

from .info import InfoResult, collect_info, print_info
from .key import read_key
from .manager_args import ArgParser
from .manager_funcs import (
    add_archival_storage,
    add_directory,
    add_host_name,
    add_key_id,
    add_time_series,
    archive_dataset,
    check_archival_storage_system_name,
    create_tables,
    delete_dataset,
    delete_replica,
    delete_time_series,
    get_host_name,
    process_image,
    process_image_data,
    set_default_args,
    update,
)
from .schema import (
    SchemaInterpretationError,
    interpret_activity_profiles,
    interpret_campaign_schema_layout,
    validate_action_spec,
    validate_campaign_activities,
)
from .upgrade import upgrade_aca
from .utils import (
    check_campaign_store,
    sql_commit,
    sql_error_list,
)
from .variables import (
    DEFAULT_RUN,
    ActivityResult,
    VariableDeleteImpact,
    VariableRef,
    VariableSpec,
    variable_delete_impact,
    variable_transaction,
)
from .variables import (
    add_activity as add_provenance_activity,
)
from .variables import (
    add_variable as add_logical_variable,
)
from .variables import (
    delete_variable as delete_logical_variable,
)
from .variables import (
    set_primary_variable as set_logical_primary_variable,
)

CURRENT_TIME = time_ns()
_CAMPAIGN_SCHEMA_NAME = "__campaign_schema.yaml"


class Manager:  # pylint: disable=too-many-public-methods
    """Manager API for campaign archives."""

    def __init__(
        self,
        archive: str,
        hostname: str = "",
        campaign_store: str = "",
        keyfile: str = "",
        verbose: int = 0,
    ):
        """
        Create Manager object for a campaign archive
        :param archive: The name of the campaign archive (relative path under campaign_store)
        :param hostname: Optional hostname, default is from ~/.config/hpc-campaign/config.yaml, or
           the return value of gethostname.
        :param campaign_store: Optional base path for all campaign archives, default is from
            ~/.config/hpc-campaign/config.yaml.
        :param keyfile: Optional encryption key to encrypt all metadata inside the campaign archive.
            Only applied to the operations in this session, existing information is not encrypted.
        :param verbose: Optional verbose for printing debug information if verbose > 0
        """

        if not archive:
            raise ValueError("Manager requires an archive path")

        self.args: argparse.Namespace = argparse.Namespace(archive=archive)
        self.args.verbose = verbose
        self.args.campaign_store = campaign_store
        self.args.hostname = hostname
        self.args.keyfile = keyfile
        self.args = set_default_args(self.args)
        self._apply_encryption_key()
        check_campaign_store(self.args.campaign_store, False)
        self.con: sqlite3.Connection
        self.cur: sqlite3.Cursor
        self.connected = False

    def _apply_encryption_key(self):
        if self.args.keyfile:
            key = read_key(self.args.keyfile)
            # ask for password at this point
            self.args.encryption_key = key.get_decrypted_key()
            self.args.encryption_key_id = key.id
        else:
            self.args.encryption_key = None
            self.args.encryption_key_id = None

    def _build_command_args(self, command: str, updates: dict | None = None) -> argparse.Namespace:
        cmd_args = argparse.Namespace(**vars(self.args))
        cmd_args.command = command
        if updates:
            for key, value in updates.items():
                setattr(cmd_args, key, value)
        return cmd_args

    def _wipe_aca(self):
        self.cur.execute("PRAGMA foreign_keys = OFF;")
        objects = self.cur.execute("""
            SELECT type, name
            FROM sqlite_master
            WHERE name NOT LIKE 'sqlite_%';
        """).fetchall()

        for obj_type, name in objects:
            self.cur.execute(f'DROP {obj_type.upper()} IF EXISTS "{name}";')

        self.con.commit()
        self.con.execute("VACUUM;")
        # PRAGMA foreign_keys is connection-local. Truncation temporarily turns
        # it off so tables can be dropped, then must restore it for the newly
        # created provenance constraints on this same Manager session.
        self.con.execute("PRAGMA foreign_keys = ON;")

    def open(self, create=False, truncate=False):
        """
        Open/create an ACA campaign archive
        :param create: if True create new archive if it does not exists. Default is to throw an error.
        :param truncate: if True and archive already exists, remove all content of the archive first.
        """
        fileexists = exists(self.args.campaign_file_name)
        if not create and not fileexists:
            raise FileNotFoundError(f"archive {self.args.campaign_file_name} does not exist")

        self.con = sqlite3.connect(self.args.campaign_file_name)
        self.con.row_factory = sqlite3.Row
        self.con.execute("PRAGMA foreign_keys = ON")
        self.cur = self.con.cursor()
        self.connected = True

        if truncate:
            self._wipe_aca()

        if not fileexists or truncate:
            create_tables(self.args.campaign_file_name, self.con)

    def close(self):
        """
        Close the ACA campaign archive.
        All operations have committed their changes, so close is only for freeing up database resources.
        """
        if self.connected:
            self.cur.close()
            self.con.close()
            self.connected = False

    def info(
        self,
        list_replicas: bool = False,
        list_files: bool = False,
        show_deleted: bool = False,
        show_checksum: bool = False,
    ) -> InfoResult:
        args = self._build_command_args(
            "info",
            {
                "list_replicas": list_replicas,
                "list_files": list_files,
                "show_deleted": show_deleted,
                "show_checksum": show_checksum,
            },
        )
        if not self.connected:
            self.open(create=True, truncate=False)
        info_data = collect_info(args, self.con)
        return info_data

    def data(self, files: list[str | Path] | str | Path, name: str | None = None):
        file_list = self.normalize_files(files)
        if name is not None and len(file_list) > 1:
            raise ValueError("Invalid arguments for data: when using --name <name>, only one data file is allowed")
        cmd_args = self._build_command_args("data", {"files": file_list, "name": name})
        if not self.connected:
            self.open(create=True, truncate=False)
        update(cmd_args, self.cur, self.con)

    def text(self, files: list[str | Path] | str | Path, name: str | None = None, store: bool = False):
        file_list = self.normalize_files(files)
        if name is not None and len(file_list) > 1:
            raise ValueError("Invalid arguments for text: when using --name <name>, only one text file is allowed")
        cmd_args = self._build_command_args(
            "text",
            {"files": file_list, "name": name, "store": store},
        )
        if not self.connected:
            self.open(create=True, truncate=False)
        update(cmd_args, self.cur, self.con)

    def set_schema(self, schema_file: str | Path):
        schema_path = Path(schema_file).expanduser()
        if not schema_path.is_file():
            raise FileNotFoundError(f"Schema file not found: {schema_path}")
        if schema_path.stat().st_size == 0:
            raise ValueError(f"Schema file is empty: {schema_path}")
        schema = self._parse_campaign_schema(schema_path.read_text(encoding="utf-8"), str(schema_path))
        profiles = interpret_activity_profiles(schema)
        if not self.connected:
            self.open(create=True, truncate=False)
        # Validate existing activities before storing the candidate. A rejected
        # profile must not replace a schema under which the archive was valid.
        validate_campaign_activities(profiles, self._stored_activities())
        cmd_args = self._build_command_args(
            "text",
            {
                "files": [str(schema_path)],
                "name": _CAMPAIGN_SCHEMA_NAME,
                "store": True,
                "filename_as_recorded": _CAMPAIGN_SCHEMA_NAME,
            },
        )
        update(cmd_args, self.cur, self.con)

    def validate_schema(self) -> dict:
        if not self.connected:
            self.open(create=False, truncate=False)

        schema = self._parse_campaign_schema(self._read_embedded_schema_text(), _CAMPAIGN_SCHEMA_NAME)
        profiles = interpret_activity_profiles(schema)
        layout = interpret_campaign_schema_layout(
            schema,
            datasets=self._live_dataset_names(),
            timeseries=self._time_series_membership(),
        )
        layout["activity_profiles"] = profiles
        layout["activity_validation"] = validate_campaign_activities(profiles, self._stored_activities())
        return layout

    @staticmethod
    def _parse_campaign_schema(schema_text: str, source_name: str) -> dict:
        """Parse one schema and preserve a useful source name in diagnostics."""
        try:
            schema = yaml.safe_load(schema_text)
        except yaml.YAMLError as exc:
            raise SchemaInterpretationError(f"Invalid {source_name}: {exc}") from exc
        if not isinstance(schema, dict):
            raise SchemaInterpretationError(f"{source_name} must contain a mapping")
        return schema

    def _read_embedded_schema_text(self) -> str:
        row = self.cur.execute(
            """
            select
              r.keyid as keyid,
              f.compression as compression,
              f.data as data
            from dataset as d
            join replica as r on r.datasetid = d.rowid
            join repfiles as rf on rf.replicaid = r.rowid
            join file as f on f.fileid = rf.fileid
            where d.name = ? and d.fileformat = 'TEXT' and d.deltime = 0 and r.deltime = 0
            order by r.rowid desc, f.fileid desc
            limit 1
            """,
            (_CAMPAIGN_SCHEMA_NAME,),
        ).fetchone()

        if row is None:
            raise FileNotFoundError(f"{_CAMPAIGN_SCHEMA_NAME} is not stored in this campaign")

        data = bytes(row["data"])
        key_id = int(row["keyid"])
        if key_id > 0:
            if not self.args.encryption_key:
                raise SchemaInterpretationError(
                    f"{_CAMPAIGN_SCHEMA_NAME} is encrypted; open Manager with keyfile to validate"
                )
            box = nacl.secret.SecretBox(self.args.encryption_key)
            data = box.decrypt(data)

        if int(row["compression"]):
            data = zlib.decompress(data)

        return data.decode("utf-8")

    def _live_dataset_names(self) -> list[str]:
        rows = self.cur.execute(
            """
            select name
            from dataset
            where deltime = 0 and name != ? and fileformat != 'VARIABLES'
            order by name
            """,
            (_CAMPAIGN_SCHEMA_NAME,),
        ).fetchall()
        return [str(row["name"]) for row in rows]

    def _time_series_membership(self) -> dict[str, list[str]]:
        rows = self.cur.execute(
            """
            select t.name as timeseries_name, d.name as dataset_name
            from timeseries as t
            join dataset as d on d.tsid = t.tsid
            where d.deltime = 0
            order by t.name, d.tsorder
            """
        ).fetchall()

        membership: dict[str, list[str]] = {}
        for row in rows:
            membership.setdefault(str(row["timeseries_name"]), []).append(str(row["dataset_name"]))
        return membership

    @staticmethod
    def _decode_action_spec(raw_spec: str | None, identity: str):
        if raw_spec is None:
            return None
        try:
            return json.loads(raw_spec)
        except (json.JSONDecodeError, TypeError) as exc:
            raise SchemaInterpretationError(f"{identity}: stored action_spec is not valid JSON") from exc

    def _stored_activities(self) -> list[tuple[str, str, Any]]:
        """Read the minimal activity fields needed for profile checks."""
        table = self.cur.execute("select 1 from sqlite_master where type = 'table' and name = 'activity'").fetchone()
        if table is None:
            return []
        rows = self.cur.execute(
            "select a.uuid, kind.name as action, spec.metadata as action_spec "
            "from activity as a join activity_kind as kind on kind.kindid = a.kindid "
            "left join action_spec as spec on spec.specid = a.specid order by a.activityid"
        ).fetchall()
        return [
            (
                str(row["uuid"]),
                str(row["action"]),
                self._decode_action_spec(row["action_spec"], f"activity {row['uuid']}"),
            )
            for row in rows
        ]

    def _stored_activity_profiles(self) -> dict[str, dict[str, Any]] | None:
        """Return active profiles, treating an absent schema as unconstrained."""
        try:
            schema_text = self._read_embedded_schema_text()
        except FileNotFoundError:
            return None
        schema = self._parse_campaign_schema(schema_text, _CAMPAIGN_SCHEMA_NAME)
        return interpret_activity_profiles(schema)

    def _validate_action_write(
        self,
        action: str,
        action_spec,
        *,
        append_output: VariableRef | None = None,
    ) -> None:
        """Validate the effective action specification before mutation."""
        profiles = self._stored_activity_profiles()
        effective_spec = action_spec
        if append_output is not None and action_spec is None:
            row = self.cur.execute(
                "select spec.metadata as action_spec from logical_variable as lv "
                "join campaign_run as run on run.runid = lv.runid "
                "join dataset as d on d.rowid = lv.datasetid "
                "join activity_output as output on output.variableid = lv.variableid "
                "join activity as a on a.activityid = output.activityid "
                "left join action_spec as spec on spec.specid = a.specid "
                "where run.name = ? and d.name = ? and d.deltime = 0 and lv.name = ?",
                (append_output.run, append_output.dataset, append_output.variable),
            ).fetchone()
            if row is not None:
                effective_spec = self._decode_action_spec(row["action_spec"], "existing activity")
        validate_action_spec(action, effective_spec, profiles)

    def delete_uuid(self, uuid: str):
        if not self.connected:
            self.open(create=True, truncate=False)
        delete_dataset(self.args, self.cur, self.con, uniqueid=uuid)
        sql_commit(self.con)

    def delete_name(self, name: str):
        if not self.connected:
            self.open(create=True, truncate=False)
        delete_dataset(self.args, self.cur, self.con, name=name)
        sql_commit(self.con)

    def delete_replica(self, replicaid: int):
        if not self.connected:
            self.open(create=True, truncate=False)
        delete_replica(self.args, self.cur, self.con, replicaid, True)
        sql_commit(self.con)

    def delete_time_series(self, name: str):
        if not self.connected:
            self.open(create=True, truncate=False)
        delete_time_series(name, self.cur, self.con)

    def add_archival_storage(
        self,
        system: str,
        host: str,
        directory: str,
        tarfilename: str = "",
        tarfileidx: str = "",
        longhostname: str = "",
        note: str = "",
    ) -> tuple[int, int, int]:
        check_archival_storage_system_name(system)
        cmd_args = self._build_command_args(
            "archival_storage",
            {
                "system": system,
                "host": host,
                "directory": directory,
                "tarfilename": tarfilename,
                "tarfileidx": tarfileidx,
                "longhostname": longhostname,
                "note": note,
            },
        )
        if not self.connected:
            self.open(create=True, truncate=False)
        host_id, dir_id, archive_id = add_archival_storage(cmd_args, self.cur, self.con)
        return host_id, dir_id, archive_id

    def archived_replica(
        self, name: str, dirid: int, archiveid: int = 0, newpath: str = "", replica: int = 0, move: bool = False
    ):
        cmd_args = self._build_command_args(
            "archived_replica",
            {
                "name": name,
                "dirid": dirid,
                "archiveid": archiveid,
                "newpath": newpath,
                "replica": replica,
                "move": move,
            },
        )
        if not self.connected:
            self.open(create=True, truncate=False)
        archive_dataset(cmd_args, self.cur, self.con)

    def add_time_series(self, name: str, datasets: str | list[str], replace: bool = False):
        dslist = datasets
        if isinstance(datasets, str):
            dslist = [datasets]
        cmd_args = self._build_command_args(
            "add_time_series",
            {"name": name, "datasets": dslist, "replace": replace},
        )
        if not self.connected:
            self.open(create=True, truncate=False)
        add_time_series(cmd_args, self.cur, self.con)

    def add_variable(
        self,
        *,
        dataset: str,
        variable: str,
        run: str = DEFAULT_RUN,
        definition: str | None = None,
        chunks=None,
        primary: bool = False,
        preferred_preview: VariableRef | None = None,
        append: bool = False,
    ) -> VariableRef:
        """Register a source data product or append chunks to one."""
        if not self.connected:
            self.open(create=True, truncate=False)
        return add_logical_variable(
            self.cur,
            self.con,
            dataset=dataset,
            variable=variable,
            run=run,
            definition=definition,
            chunks=chunks,
            primary=primary,
            preferred_preview=preferred_preview,
            append=append,
        )

    def set_primary_variable(self, variable: VariableRef) -> None:
        """Bind an existing data product as its definition's primary value."""
        if not self.connected:
            self.open(create=True, truncate=False)
        set_logical_primary_variable(self.cur, self.con, variable)

    def add_activity(
        self,
        *,
        action: str,
        inputs: Mapping[str, VariableRef],
        outputs: Mapping[str, VariableSpec | Mapping[str, Any]],
        action_spec: Mapping[str, Any] | None = None,
        source_steps=None,
    ) -> ActivityResult:
        """Atomically record an action, its inputs, and its generated outputs."""
        if not self.connected:
            self.open(create=True, truncate=False)

        append_output = None
        if len(outputs) == 1:
            output = next(iter(outputs.values()))
            if isinstance(output, VariableSpec) and output.append:
                append_output = VariableRef(output.run, output.dataset, output.variable)
            elif isinstance(output, Mapping) and output.get("append"):
                append_output = VariableRef(
                    str(output.get("run", DEFAULT_RUN)),
                    str(output.get("dataset", "")),
                    str(output.get("variable", "")),
                )
        self._validate_action_write(action, action_spec, append_output=append_output)
        return add_provenance_activity(
            self.cur,
            self.con,
            action=action,
            inputs=inputs,
            outputs=outputs,
            action_spec=action_spec,
            source_steps=source_steps,
        )

    def variable_delete_impact(self, variable: VariableRef) -> VariableDeleteImpact:
        """Report variables affected by deleting a logical variable."""
        if not self.connected:
            self.open(create=True, truncate=False)
        return variable_delete_impact(self.cur, variable)

    def delete_variable(self, variable: VariableRef, *, cascade: bool = False) -> VariableDeleteImpact:
        """Delete a logical variable and optionally its downstream products."""
        if not self.connected:
            self.open(create=True, truncate=False)
        return delete_logical_variable(self.cur, self.con, variable, cascade=cascade)

    def add_image_sequence(
        self,
        *,
        dataset: str,
        variable: str,
        images,
        inputs: Mapping[str, VariableRef],
        run: str = DEFAULT_RUN,
        definition: str | None = None,
        source_steps=None,
        action_spec: Mapping[str, Any] | None = None,
        store: bool = False,
        thumbnail: list[int] | tuple[int, int] | None = None,
        preferred_preview: VariableRef | None = None,
        append: bool = False,
    ) -> VariableRef:
        """Ingest images and record the visualization activity that made them."""
        if not self.connected:
            self.open(create=True, truncate=False)

        # Validate before image ingestion so a profile error cannot leave
        # payload datasets behind.
        append_output = VariableRef(run, dataset, variable) if append else None
        self._validate_action_write(
            "visualization",
            action_spec,
            append_output=append_output,
        )

        image_inputs = self._expand_image_sequence_inputs(images)
        if not image_inputs:
            action = "append" if append else "create"
            raise ValueError(f"add_image_sequence requires one or more images to {action} a sequence")
        descriptors = [self._describe_image_sequence_input(image) for image in image_inputs]
        self._validate_image_sequence_descriptors(descriptors)
        if append:
            existing_signature = self._existing_image_sequence_signature(run, dataset, variable)
            self._validate_image_sequence_append(descriptors[0], existing_signature)
        if not store and any(descriptor["data"] is not None for descriptor in descriptors):
            raise ValueError("In-memory images require store=True because they have no external replica path")

        thumb_value = None
        if thumbnail is not None:
            if len(thumbnail) != 2:
                raise ValueError("thumbnail must contain [width, height]")
            thumb_value = [int(thumbnail[0]), int(thumbnail[1])]
            if any(value <= 0 for value in thumb_value):
                raise ValueError("thumbnail dimensions must be positive")

        payload_names: list[str] = []
        with variable_transaction(self.con, "image_sequence_write"):
            long_host_name, short_host_name = get_host_name(self.args)
            verbose = bool(self.args.verbose)
            host_id = add_host_name(long_host_name, short_host_name, self.cur, verbose=verbose)
            key_id = add_key_id(self.args.encryption_key_id, self.cur, verbose=verbose)
            rootdir = getcwd()
            dir_id = add_directory(host_id, rootdir, self.cur, verbose=verbose)

            run_token = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(run)).strip("_") or "run"
            variable_token = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(variable)).strip("_") or "variable"
            for descriptor in descriptors:
                suffix = "." + str(descriptor["format"]).lower().replace("jpeg", "jpg")
                payload_name = (
                    f"{dataset}/.variable-payloads/{run_token}/{variable_token}/{descriptor['identity']}{suffix}"
                )
                payload_names.append(payload_name)
                if descriptor["path"] is not None:
                    cmd_args = self._build_command_args(
                        "image",
                        {
                            "file": str(descriptor["path"]),
                            "name": payload_name,
                            "store": bool(store),
                            "thumbnail": thumb_value,
                            "verbose": self.args.verbose,
                        },
                    )
                    process_image(
                        cmd_args,
                        self.cur,
                        host_id,
                        dir_id,
                        key_id,
                        long_host_name + rootdir,
                        rootdir,
                    )
                else:
                    image_format = str(descriptor["format"])
                    cmd_args = self._build_command_args(
                        "image_data",
                        {
                            "image_data": descriptor["data"],
                            "image_format": image_format,
                            "name": payload_name,
                            "thumbnail": thumb_value,
                            "replica_name": f"generated/{descriptor['identity']}{suffix}",
                            "store": True,
                            "verbose": self.args.verbose,
                        },
                    )
                    process_image_data(
                        cmd_args,
                        self.cur,
                        host_id,
                        dir_id,
                        key_id,
                        long_host_name + rootdir,
                        rootdir,
                    )

            result = self.add_activity(
                action="visualization",
                inputs=inputs,
                outputs={
                    "result": VariableSpec(
                        run=run,
                        dataset=dataset,
                        variable=variable,
                        definition=definition,
                        chunks=payload_names,
                        preferred_preview=preferred_preview,
                        append=append,
                    )
                },
                action_spec=action_spec,
                source_steps=source_steps,
            )
            return result.outputs["result"]

    def upgrade(self) -> str:
        if not self.connected:
            self.open(create=True, truncate=False)
        new_version = upgrade_aca(self.args, self.cur, self.con)
        return new_version

    def normalize_files(self, files: list[str | Path] | str | Path) -> list[str]:
        if isinstance(files, (str, Path)):
            return [str(files)]
        return [str(entry) for entry in files]

    @staticmethod
    def _natural_path_key(path: str | Path) -> list[tuple[int, str | int]]:
        parts = re.split(r"(\d+)", str(path).casefold())
        return [(1, int(part)) if part.isdigit() else (0, part) for part in parts]

    def _expand_image_sequence_inputs(self, images) -> list:
        if isinstance(images, (str, Path, bytes, bytearray, memoryview, PILImage.Image)):
            raw_inputs: list[Any] = [images]
        elif self._is_matplotlib_figure(images):
            raw_inputs = [images]
        else:
            raw_inputs = list(images)

        expanded: list[Any] = []
        for image in raw_inputs:
            if not isinstance(image, (str, Path)):
                expanded.append(image)
                continue
            path_text = str(image)
            if glob.has_magic(path_text):
                matches = sorted(glob.glob(path_text), key=self._natural_path_key)
                if not matches:
                    raise ValueError(f"Image pattern matched no files: {path_text}")
                expanded.extend(Path(match) for match in matches)
            else:
                expanded.append(Path(image))
        return expanded

    def _describe_image_sequence_input(self, image) -> dict:
        if isinstance(image, (str, Path)):
            path = Path(image)
            if not path.is_file():
                raise FileNotFoundError(f"Image file not found: {path}")
            with PILImage.open(path) as opened:
                opened.load()
                image_format = str(opened.format or "").upper()
                if not image_format:
                    raise ValueError(f"Could not determine image encoding: {path}")
                size = tuple(int(value) for value in opened.size)
                mode = str(opened.mode)
            identity = sha1(str(path.resolve()).encode("utf-8")).hexdigest()[:24]
            return {
                "path": path,
                "data": None,
                "format": image_format,
                "size": size,
                "mode": mode,
                "identity": identity,
            }

        image_data, image_format = self._coerce_image_input(image, None)
        try:
            with PILImage.open(BytesIO(image_data)) as opened:
                opened.load()
                detected_format = str(opened.format or image_format).upper()
                size = tuple(int(value) for value in opened.size)
                mode = str(opened.mode)
        except Exception as exc:
            raise ValueError("Invalid in-memory image payload") from exc
        identity = sha1(image_data).hexdigest()[:24]
        return {
            "path": None,
            "data": image_data,
            "format": detected_format,
            "size": size,
            "mode": mode,
            "identity": identity,
        }

    @staticmethod
    def _validate_image_sequence_descriptors(descriptors: list[dict]) -> None:
        first = descriptors[0]
        first_width, first_height = first["size"]
        for descriptor in descriptors[1:]:
            if descriptor["size"] != first["size"]:
                raise ValueError("All images in a sequence must have the same resolution and aspect ratio")
            if descriptor["format"] != first["format"]:
                raise ValueError("All images in a sequence must use the same encoding")
            if descriptor["mode"] != first["mode"]:
                raise ValueError("All images in a sequence must use the same pixel mode")
            width, height = descriptor["size"]
            if width * first_height != first_width * height:
                raise ValueError("All images in a sequence must have the same aspect ratio")

    def _existing_image_sequence_signature(self, run: str, dataset: str, variable: str) -> tuple | None:
        row = self.cur.execute(
            "select resolution.x, resolution.y, replica.name as replica_name, file.name as file_name "
            "from logical_variable as logical "
            "join campaign_run as campaign_run on campaign_run.runid = logical.runid "
            "join dataset as owner on owner.rowid = logical.datasetid "
            "join variable_chunk as chunk on chunk.variableid = logical.variableid "
            "join replica on replica.datasetid = chunk.payload_datasetid and replica.deltime = 0 "
            "left join resolution on resolution.replicaid = replica.rowid "
            "left join repfiles on repfiles.replicaid = replica.rowid "
            "left join file on file.fileid = repfiles.fileid "
            "where campaign_run.name = ? and owner.name = ? and logical.name = ? "
            "order by chunk.chunk_index, replica.rowid, file.fileid limit 1",
            (run, dataset, variable),
        ).fetchone()
        if row is None or row["x"] is None or row["y"] is None:
            return None
        suffix = Path(str(row["replica_name"] or "")).suffix.lower()
        if not suffix:
            suffix = Path(str(row["file_name"] or "")).suffix.lower()
        encoding = {
            ".jpg": "JPEG",
            ".jpeg": "JPEG",
            ".png": "PNG",
            ".gif": "GIF",
            ".webp": "WEBP",
            ".bmp": "BMP",
            ".tif": "TIFF",
            ".tiff": "TIFF",
        }.get(suffix)
        return (int(row["x"]), int(row["y"]), encoding)

    @staticmethod
    def _validate_image_sequence_append(descriptor: dict, existing_signature: tuple | None) -> None:
        if existing_signature is None:
            return
        width, height, encoding = existing_signature
        if descriptor["size"] != (width, height):
            raise ValueError("Appended images must have the same resolution and aspect ratio as the sequence")
        if encoding is not None and descriptor["format"] != encoding:
            raise ValueError("Appended images must use the same encoding as the sequence")

    def _is_matplotlib_figure(self, image) -> bool:
        image_type = type(image)
        return image_type.__module__.startswith("matplotlib.") and hasattr(image, "savefig")

    def _infer_image_format(self, data: bytes) -> str | None:
        if data.startswith(b"\x89PNG\r\n\x1a\n"):
            return "PNG"
        if data.startswith(b"\xff\xd8\xff"):
            return "JPEG"
        if data.startswith(b"GIF87a") or data.startswith(b"GIF89a"):
            return "GIF"
        return None

    def _coerce_image_input(self, image, image_format: str | None) -> tuple[bytes, str]:
        if isinstance(image, memoryview):
            image = image.tobytes()
        if isinstance(image, bytearray):
            image = bytes(image)
        if isinstance(image, bytes):
            resolved_format = image_format or self._infer_image_format(image)
            if not resolved_format:
                raise ValueError("image_format is required for unrecognized in-memory image bytes")
            return image, resolved_format
        if isinstance(image, PILImage.Image):
            if not image_format:
                image_format = "PNG"
            buf = BytesIO()
            image.save(buf, format=image_format.upper())
            return buf.getvalue(), image_format
        if self._is_matplotlib_figure(image):
            resolved_format = image_format or "PNG"
            buf = BytesIO()
            image.savefig(buf, format=resolved_format.lower())
            return buf.getvalue(), resolved_format
        raise TypeError(f"Unsupported image-sequence input type: {type(image)!r}")


def _load_json_object(path: str, label: str) -> dict:
    with open(path, encoding="utf-8") as json_file:
        data = json.load(json_file)
    if not isinstance(data, dict):
        raise ValueError(f"{label} must contain a JSON object")
    return data


def _require_manifest_fields(
    manifest: dict,
    required_fields: tuple[str, ...],
    label: str = "manifest",
) -> None:
    missing = [field for field in required_fields if field not in manifest]
    if missing:
        raise ValueError(f"{label} is missing required field(s): {', '.join(missing)}")


def _reject_unknown_manifest_fields(manifest: dict, supported_fields: set[str], label: str) -> None:
    """Catch obsolete provenance fields and manifest typos instead of ignoring them."""
    unknown = sorted(str(field) for field in manifest if field not in supported_fields)
    if unknown:
        raise ValueError(f"{label} contains unsupported field(s): {', '.join(unknown)}")


def _manifest_variable_ref(value, label: str) -> VariableRef:
    if not isinstance(value, dict):
        raise TypeError(f"{label} must be an object with run, dataset, and variable fields")
    _require_manifest_fields(value, ("dataset", "variable"), label)
    return VariableRef(
        str(value.get("run", DEFAULT_RUN)),
        str(value["dataset"]),
        str(value["variable"]),
    )


def _manifest_inputs(value, label: str = "inputs") -> dict[str, VariableRef]:
    """Decode a role-qualified activity input mapping."""
    if not isinstance(value, dict) or not value:
        raise ValueError(f"{label} must be a non-empty role mapping")
    return {str(role): _manifest_variable_ref(reference, f"{label}.{role}") for role, reference in value.items()}


def _apply_variable_manifest(manager: Manager, manifest: dict, append: bool = False) -> VariableRef:
    _require_manifest_fields(manifest, ("dataset", "variable"), "variable manifest")
    _reject_unknown_manifest_fields(
        manifest,
        {
            "run",
            "dataset",
            "variable",
            "definition",
            "chunks",
            "primary",
            "preferred_preview",
            "append",
        },
        "variable manifest",
    )
    preview = manifest.get("preferred_preview")
    return manager.add_variable(
        dataset=manifest["dataset"],
        variable=manifest["variable"],
        run=manifest.get("run", DEFAULT_RUN),
        definition=manifest.get("definition"),
        chunks=manifest.get("chunks"),
        primary=bool(manifest.get("primary", False)),
        preferred_preview=(_manifest_variable_ref(preview, "preferred_preview") if preview is not None else None),
        append=bool(manifest.get("append", False) or append),
    )


def _apply_activity_manifest(manager: Manager, manifest: dict) -> ActivityResult:
    _require_manifest_fields(manifest, ("action", "inputs", "outputs"), "activity manifest")
    _reject_unknown_manifest_fields(
        manifest,
        {"action", "inputs", "outputs", "action_spec", "source_steps"},
        "activity manifest",
    )
    outputs = manifest["outputs"]
    if not isinstance(outputs, dict) or not outputs:
        raise ValueError("activity manifest outputs must be a non-empty role mapping")
    return manager.add_activity(
        action=str(manifest["action"]),
        inputs=_manifest_inputs(manifest["inputs"]),
        outputs=outputs,
        action_spec=manifest.get("action_spec"),
        source_steps=manifest.get("source_steps"),
    )


def _apply_image_sequence_manifest(manager: Manager, manifest: dict, append: bool = False) -> VariableRef:
    _require_manifest_fields(
        manifest,
        ("dataset", "variable", "images", "inputs"),
        "image-sequence manifest",
    )
    _reject_unknown_manifest_fields(
        manifest,
        {
            "run",
            "dataset",
            "variable",
            "definition",
            "images",
            "inputs",
            "source_steps",
            "action_spec",
            "store",
            "thumbnail",
            "preferred_preview",
            "append",
        },
        "image-sequence manifest",
    )
    preview = manifest.get("preferred_preview")
    return manager.add_image_sequence(
        dataset=manifest["dataset"],
        variable=manifest["variable"],
        images=manifest["images"],
        inputs=_manifest_inputs(manifest["inputs"]),
        run=manifest.get("run", DEFAULT_RUN),
        definition=manifest.get("definition"),
        source_steps=manifest.get("source_steps"),
        action_spec=manifest.get("action_spec"),
        store=bool(manifest.get("store", False)),
        thumbnail=manifest.get("thumbnail"),
        preferred_preview=(_manifest_variable_ref(preview, "preferred_preview") if preview is not None else None),
        append=bool(manifest.get("append", False) or append),
    )


# pylint:disable = too-many-statements
def main(args=None, prog=None):
    parser = ArgParser(args=args, prog=prog)
    manager = Manager(
        archive=parser.args.archive,
        hostname=parser.args.hostname,
        campaign_store=parser.args.campaign_store,
        keyfile=parser.args.keyfile,
        verbose=parser.args.verbose,
    )

    n_cmd = 0
    while parser.parse_next_command():
        print("=" * 10, f"  {parser.args.command}  ", "=" * 50)
        # print(parser.args)
        # print("--------------------------")
        n_cmd += 1
        create_allowed = True
        if parser.args.command in (
            "info",
            "add-archival-storage",
            "archived-replica",
            "time-series",
            "upgrade",
        ):
            create_allowed = False
        if n_cmd == 1:
            try:
                manager.open(create=create_allowed, truncate=parser.args.truncate)
            except FileNotFoundError as e:
                print(f"ERROR: {e}")
                sys.exit(1)

        if parser.args.command == "info":
            info_data = manager.info(
                parser.args.list_replicas, parser.args.list_files, parser.args.show_deleted, parser.args.show_checksum
            )
            print_info(info_data)
        elif parser.args.command == "data":
            manager.data(parser.args.files, parser.args.name)
        elif parser.args.command == "text":
            manager.text(parser.args.files, parser.args.name, parser.args.store)
        elif parser.args.command == "schema":
            manager.set_schema(parser.args.schema_file)
        elif parser.args.command == "variable":
            manifest = _load_json_object(parser.args.manifest, "variable manifest")
            _apply_variable_manifest(manager, manifest, append=parser.args.append)
        elif parser.args.command == "activity":
            manifest = _load_json_object(parser.args.manifest, "activity manifest")
            _apply_activity_manifest(manager, manifest)
        elif parser.args.command == "image-sequence":
            manifest = _load_json_object(parser.args.manifest, "image-sequence manifest")
            _apply_image_sequence_manifest(manager, manifest, append=parser.args.append)
        elif parser.args.command == "delete":
            if parser.args.uuid is not None:
                for uid in parser.args.uuid:
                    manager.delete_uuid(uid)
            if parser.args.name is not None:
                for name in parser.args.name:
                    manager.delete_name(name)
            if parser.args.replica is not None:
                for rep in parser.args.replica:
                    manager.delete_replica(int(rep))
        elif parser.args.command == "add-archival-storage":
            host_id, dir_id, archive_id = manager.add_archival_storage(
                parser.args.system,
                parser.args.host,
                parser.args.directory,
                parser.args.tarfilename,
                parser.args.tarfileidx,
                parser.args.longhostname,
                parser.args.note,
            )
            if archive_id > 0:
                print(f"Archive storage added: host id = {host_id}, directory id = {dir_id} archive id = {archive_id}")
            else:
                print("Adding archive storage FAILED")
        elif parser.args.command == "archived-replica":
            manager.archived_replica(
                parser.args.name, parser.args.dirid, parser.args.archiveid, parser.args.newpath, parser.args.replica
            )
        elif parser.args.command == "time-series":
            if parser.args.remove:
                manager.delete_time_series(parser.args.name)
            manager.add_time_series(parser.args.name, parser.args.dataset, parser.args.replace)
        elif parser.args.command == "upgrade":
            manager.upgrade()
        else:
            print(f"This should not happen. Unknown command accepted by argparser: {parser.args.command}")

    if len(sql_error_list) > 0:
        print()
        print("!!!! SQL Errors encountered")
        for serr in sql_error_list:
            print(f"  {serr.sqlite_errorcode}  {serr.sqlite_errorname}: {serr}")
        print("!!!!")
        print()


if __name__ == "__main__":
    main()
