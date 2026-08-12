import argparse
import json
import sqlite3
from dataclasses import dataclass, field
from typing import Any

from .utils import sizeof_fmt, sql_execute, timestamp_to_str
from .variables import VariableRef

# pylint: disable=too-many-lines


@dataclass
class ArchiveInfo:
    """Archive metadata stored in the info table."""

    id: str
    name: str
    version: str
    mod_time: int


@dataclass
class ArchiveEntry:
    """Archive entry tied to a directory."""

    id: int
    tar_name: str
    system: str


@dataclass
class DirectoryInfo:
    """Directory metadata with archive entries."""

    id: int
    name: str
    mod_time: int
    del_time: int
    archives: list[ArchiveEntry] = field(default_factory=list)
    has_archive: bool = False


@dataclass
class HostInfo:
    """Host metadata with its directories."""

    id: int
    hostname: str
    long_hostname: str
    directories: list[DirectoryInfo] = field(default_factory=list)


@dataclass
class KeyInfo:
    """Encryption key metadata."""

    id: int
    key: str


@dataclass
class FileInfo:
    """Replica file metadata."""

    name: str
    len_orig: int
    len_compressed: int
    mod_time: int
    checksum: str


@dataclass
class ResolutionInfo:
    """Image resolution metadata."""

    x: int
    y: int


@dataclass
class ReplicaFlags:
    """Replica state flags."""

    deleted: bool
    encrypted: bool
    accuracy: bool
    archive: bool
    embedded: bool


@dataclass
class ReplicaInfo:  # pylint: disable=too-many-instance-attributes
    """Replica metadata entry."""

    host_id: int
    dir_id: int
    archive_id: int
    name: str
    mod_time: int
    del_time: int
    key_id: int
    size: int
    flags: ReplicaFlags
    files: list[FileInfo] = field(default_factory=list)
    resolution: ResolutionInfo | None = None


@dataclass
class DatasetInfo:
    """Dataset metadata entry."""

    uuid: str
    name: str
    mod_time: int
    del_time: int
    file_format: str
    replicas: dict[int, ReplicaInfo] = field(default_factory=dict)
    metadata: dict | None = None


@dataclass
class TimeSeriesInfo:
    """Time series metadata with datasets."""

    name: str
    datasets: dict[int, DatasetInfo] = field(default_factory=dict)


@dataclass(frozen=True)
class VariableParentInfo:
    """One labeled immediate-parent edge."""

    edge_id: int
    label: str
    variable_id: int
    reference: VariableRef
    identity_steps: bool = False


@dataclass
class VariableChunkInfo:
    """One ordered payload belonging to a logical variable."""

    id: int
    chunk_index: int
    payload_dataset_id: int
    payload_dataset_name: str
    payload_dataset_uuid: str
    payload_file_format: str
    source_steps: dict[str, int] = field(default_factory=dict)


@dataclass
class LogicalVariableInfo:  # pylint: disable=too-many-instance-attributes
    """A primary campaign variable or one of its representations."""

    id: int
    dataset_id: int
    dataset: str
    variable: str
    representation_kind: str | None
    representation_metadata: Any = None
    preferred_preview: VariableRef | None = None
    parents: list[VariableParentInfo] = field(default_factory=list)
    children: list[VariableRef] = field(default_factory=list)
    chunks: list[VariableChunkInfo] = field(default_factory=list)

    @property
    def reference(self) -> VariableRef:
        return VariableRef(self.dataset, self.variable)


@dataclass
class InfoResult:
    """Aggregated archive information."""

    archive: ArchiveInfo
    hosts: list[HostInfo] = field(default_factory=list)
    keys: list[KeyInfo] = field(default_factory=list)
    time_series: dict[int, TimeSeriesInfo] = field(default_factory=dict)
    variables: dict[int, LogicalVariableInfo] = field(default_factory=dict)
    datasets: dict[int, DatasetInfo] = field(default_factory=dict)

    def find_variable(self, dataset: str, variable: str) -> LogicalVariableInfo:
        """Find a logical variable by its public identity."""
        for item in self.variables.values():
            if item.dataset == dataset and item.variable == variable:
                return item
        raise LookupError(f"Logical variable not found: {dataset}/{variable}")

    def _variable_for_ref(self, reference: VariableRef) -> LogicalVariableInfo:
        return self.find_variable(reference.dataset, reference.variable)

    def primary_ancestors(self, reference: VariableRef) -> list[LogicalVariableInfo]:
        """Return all reachable variables that have no known parent."""
        roots: dict[int, LogicalVariableInfo] = {}
        visited: set[int] = set()

        def visit(item: LogicalVariableInfo) -> None:
            if item.id in visited:
                return
            visited.add(item.id)
            if not item.parents:
                roots[item.id] = item
                return
            for parent in item.parents:
                visit(self.variables[parent.variable_id])

        visit(self._variable_for_ref(reference))
        return [roots[key] for key in sorted(roots)]

    def representations_of(
        self,
        reference: VariableRef,
        *,
        representation_kind: str | None = None,
        transitive: bool = True,
    ) -> list[LogicalVariableInfo]:
        """Return direct or transitive representations of a variable."""
        start = self._variable_for_ref(reference)
        result: dict[int, LogicalVariableInfo] = {}
        pending = list(start.children)
        while pending:
            child_ref = pending.pop(0)
            child = self._variable_for_ref(child_ref)
            if child.id in result:
                continue
            if representation_kind is None or child.representation_kind == representation_kind:
                result[child.id] = child
            if transitive:
                pending.extend(child.children)
        return [result[key] for key in sorted(result)]

    def paths_to_roots(self, reference: VariableRef) -> list[list[VariableRef]]:
        """Return every direct-provenance path from a variable to a root."""
        paths: list[list[VariableRef]] = []

        def visit(item: LogicalVariableInfo, path: list[VariableRef]) -> None:
            next_path = [*path, item.reference]
            if not item.parents:
                paths.append(next_path)
                return
            for parent in item.parents:
                visit(self.variables[parent.variable_id], next_path)

        visit(self._variable_for_ref(reference), [])
        return paths


# ruff: disable[W291]
# fmt: off
SELECT_DATA_CMD = """
SELECT
    d.rowid             AS ds_id,
    d.name              AS ds_name,
    d.uuid              AS ds_uuid,
    d.modtime           AS ds_modtime,
    d.deltime           AS ds_deltime,
    d.fileformat        AS ds_fileformat,
    d.tsid              AS ds_tsid,

    r.rowid             AS rep_id,
    r.hostid            AS hostid,
    r.dirid             AS dirid,
    r.archiveid         AS archiveid,
    r.name              AS rep_name,
    r.modtime           AS rep_modtime,
    r.deltime           AS rep_deltime,
    r.keyid             AS keyid,
    r.size              AS rep_size,

    rf.fileid           AS repfile_id,

    f.name              AS file_name,
    f.compression       AS compression,
    f.lenorig           AS lenorig,
    f.lencompressed     AS lencompressed,
    f.modtime           AS file_modtime,
    f.checksum          AS checksum,

    acc.rowid           AS acc_id

FROM dataset AS d
JOIN replica AS r
    ON r.datasetid = d.rowid
LEFT JOIN repfiles AS rf
    ON rf.replicaid = r.rowid
LEFT JOIN file AS f
    ON f.fileid = rf.fileid
LEFT JOIN accuracy AS acc
    ON acc.replicaid = r.rowid
WHERE d.fileformat = 'ADIOS' OR d.fileformat = 'HDF5'
ORDER BY d.rowid, r.rowid, f.fileid;
"""

SELECT_IMAGES_CMD = """
SELECT
    d.rowid             AS ds_id, 
    d.name              AS ds_name,
    d.uuid              AS ds_uuid,
    d.modtime           AS ds_modtime, 
    d.deltime           AS ds_deltime,
    d.fileformat        AS ds_fileformat,
    d.tsid              AS ds_tsid,

    r.rowid             AS rep_id,
    r.hostid            AS hostid,
    r.dirid             AS dirid,
    r.archiveid         AS archiveid,
    r.name              AS rep_name,
    r.modtime           AS rep_modtime,
    r.deltime           AS rep_deltime,
    r.keyid             AS keyid,
    r.size              AS rep_size,

    rf.fileid           AS repfile_id,

    f.name              AS file_name,
    f.compression       AS compression,
    f.lenorig           AS lenorig,
    f.lencompressed     AS lencompressed,
    f.modtime           AS file_modtime,
    f.checksum          AS checksum,

    res.x               AS res_x,
    res.y               AS res_y

FROM dataset AS d
JOIN replica AS r
    ON r.datasetid = d.rowid
LEFT JOIN repfiles AS rf
    ON rf.replicaid = r.rowid
LEFT JOIN file AS f
    ON f.fileid = rf.fileid
LEFT JOIN resolution AS res
    ON res.replicaid = r.rowid
WHERE d.fileformat = 'IMAGE'
ORDER BY d.rowid, r.rowid, f.fileid;
"""

SELECT_TEXTS_CMD = """
SELECT
    d.rowid             AS ds_id, 
    d.name              AS ds_name,
    d.uuid              AS ds_uuid,
    d.modtime           AS ds_modtime, 
    d.deltime           AS ds_deltime,
    d.fileformat        AS ds_fileformat,
    d.tsid              AS ds_tsid,

    r.rowid             AS rep_id,
    r.hostid            AS hostid,
    r.dirid             AS dirid,
    r.archiveid         AS archiveid,
    r.name              AS rep_name,
    r.modtime           AS rep_modtime,
    r.deltime           AS rep_deltime,
    r.keyid             AS keyid,
    r.size              AS rep_size,

    rf.fileid           AS repfile_id,

    f.name              AS file_name,
    f.compression       AS compression,
    f.lenorig           AS lenorig,
    f.lencompressed     AS lencompressed,
    f.modtime           AS file_modtime,
    f.checksum          AS checksum

FROM dataset AS d
JOIN replica AS r
    ON r.datasetid = d.rowid
LEFT JOIN repfiles AS rf
    ON rf.replicaid = r.rowid
LEFT JOIN file AS f
    ON f.fileid = rf.fileid
WHERE d.fileformat = 'TEXT'
ORDER BY d.rowid, r.rowid, f.fileid;
"""

# ruff: enable[W291]
# fmt: on


# pylint: disable=too-many-locals
# pylint: disable=too-many-positional-arguments
# pylint: disable=too-many-arguments
def info_row(
    args: argparse.Namespace,
    info_data: InfoResult,
    row,
    accuracy: bool,
    embedded: bool,
    resolution: ResolutionInfo | None,
    dirs_archived: dict[int, bool],
) -> DatasetInfo | None:

    dataset_del_time = int(row["ds_deltime"])
    replica_del_time = int(row["rep_deltime"])
    if (dataset_del_time + replica_del_time) > 0 and not args.show_deleted:
        return None

    dataset_id = int(row["ds_id"])
    ts_id = int(row["ds_tsid"])
    if ts_id > 0:
        dataset_info = info_data.time_series[ts_id].datasets.setdefault(
            dataset_id,
            DatasetInfo(
                row["ds_uuid"],
                row["ds_name"],
                int(row["ds_modtime"]),
                dataset_del_time,
                row["ds_fileformat"],
            ),
        )
    else:
        dataset_info = info_data.datasets.setdefault(
            dataset_id,
            DatasetInfo(
                row["ds_uuid"],
                row["ds_name"],
                int(row["ds_modtime"]),
                dataset_del_time,
                row["ds_fileformat"],
            ),
        )

    replica_id = int(row["rep_id"])
    replica_info = dataset_info.replicas.get(replica_id)
    if replica_info is None:
        dir_id = int(row["dirid"])
        key_id = int(row["keyid"])

        flags = ReplicaFlags(
            deleted=replica_del_time > 0,
            encrypted=key_id > 0,
            accuracy=accuracy,
            archive=dirs_archived.get(dir_id, False),
            embedded=embedded,
        )

        replica_info = ReplicaInfo(
            host_id=int(row["hostid"]),
            dir_id=dir_id,
            archive_id=int(row["archiveid"]),
            name=row["rep_name"],
            mod_time=int(row["rep_modtime"]),
            del_time=replica_del_time,
            key_id=key_id,
            size=int(row["rep_size"]),
            flags=flags,
        )
        dataset_info.replicas[replica_id] = replica_info

    if resolution is not None:
        replica_info.resolution = resolution

    if args.list_files and row["repfile_id"] is not None:
        cks = row["checksum"] if args.show_checksum else ""
        replica_info.files.append(
            FileInfo(
                name=row["file_name"],
                len_orig=int(row["lenorig"]),
                len_compressed=int(row["lencompressed"]),
                mod_time=int(row["file_modtime"]),
                checksum=cks,
            )
        )
    return dataset_info


def info_datas(  # pylint: disable=too-many-locals
    args: argparse.Namespace,
    info_data: InfoResult,
    cur: sqlite3.Cursor,
    dirs_archived: dict[int, bool],
):
    #
    # ADIOS and HDF5 datasets
    #
    res = sql_execute(cur, SELECT_DATA_CMD)
    for row in res:
        info_row(
            args,
            info_data,
            row,
            accuracy=(row["acc_id"] is not None),
            embedded=False,
            resolution=None,
            dirs_archived=dirs_archived,
        )


def info_images(  # pylint: disable=too-many-locals
    args: argparse.Namespace,
    info_data: InfoResult,
    cur: sqlite3.Cursor,
    dirs_archived: dict[int, bool],
):
    #
    # IMAGE datasets
    #
    res = sql_execute(cur, SELECT_IMAGES_CMD)
    for row in res:
        res_x = int(row["res_x"])
        res_y = int(row["res_y"])
        dataset_info = info_row(
            args,
            info_data,
            row,
            accuracy=False,
            embedded=(row["repfile_id"] is not None),
            resolution=ResolutionInfo(res_x, res_y),
            dirs_archived=dirs_archived,
        )
        if dataset_info is None:
            continue


def info_texts(  # pylint: disable=too-many-locals
    args: argparse.Namespace,
    info_data: InfoResult,
    cur: sqlite3.Cursor,
    dirs_archived: dict[int, bool],
):
    #
    # TEXT datasets
    #
    res = sql_execute(cur, SELECT_TEXTS_CMD)
    for row in res:
        info_row(
            args,
            info_data,
            row,
            accuracy=False,
            embedded=(row["repfile_id"] is not None),
            resolution=None,
            dirs_archived=dirs_archived,
        )


def _info_json_value(value: str | None) -> Any:
    if value is None:
        return None
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return None


def info_variables(info_data: InfoResult, cur: sqlite3.Cursor) -> None:  # pylint: disable=too-many-locals
    """Load the unified logical-variable graph into an InfoResult."""
    required_tables = {
        "logical_variable",
        "variable_representation_edge",
        "variable_chunk",
        "variable_chunk_source_step",
    }
    placeholders = ", ".join("?" for _name in required_tables)
    rows = sql_execute(
        cur,
        f"select name from sqlite_master where type = 'table' and name in ({placeholders})",
        tuple(required_tables),
    ).fetchall()
    if {str(row[0]) for row in rows} != required_tables:
        return

    variable_rows = sql_execute(
        cur,
        "select lv.variableid, lv.datasetid, d.name as dataset_name, lv.name, "
        "lv.representation_kind, lv.representation_metadata, "
        "preview.name as preview_name, preview_dataset.name as preview_dataset_name "
        "from logical_variable as lv "
        "join dataset as d on d.rowid = lv.datasetid and d.deltime = 0 "
        "left join logical_variable as preview on preview.variableid = lv.preferred_preview_id "
        "left join dataset as preview_dataset on preview_dataset.rowid = preview.datasetid "
        "order by lv.variableid",
    )
    for row in variable_rows:
        preview = None
        if row["preview_name"] is not None and row["preview_dataset_name"] is not None:
            preview = VariableRef(str(row["preview_dataset_name"]), str(row["preview_name"]))
        variable_id = int(row["variableid"])
        info_data.variables[variable_id] = LogicalVariableInfo(
            id=variable_id,
            dataset_id=int(row["datasetid"]),
            dataset=str(row["dataset_name"]),
            variable=str(row["name"]),
            representation_kind=(str(row["representation_kind"]) if row["representation_kind"] is not None else None),
            representation_metadata=_info_json_value(row["representation_metadata"]),
            preferred_preview=preview,
        )

    edge_rows = sql_execute(
        cur,
        "select edge.edgeid, edge.child_variable_id, edge.parent_variable_id, edge.label, edge.identity_steps, "
        "parent.name as parent_name, parent_dataset.name as parent_dataset_name, "
        "child.name as child_name, child_dataset.name as child_dataset_name "
        "from variable_representation_edge as edge "
        "join logical_variable as parent on parent.variableid = edge.parent_variable_id "
        "join dataset as parent_dataset on parent_dataset.rowid = parent.datasetid "
        "join logical_variable as child on child.variableid = edge.child_variable_id "
        "join dataset as child_dataset on child_dataset.rowid = child.datasetid "
        "order by edge.child_variable_id, edge.edgeid",
    )
    edge_labels: dict[int, str] = {}
    for row in edge_rows:
        child_id = int(row["child_variable_id"])
        parent_id = int(row["parent_variable_id"])
        child = info_data.variables.get(child_id)
        parent = info_data.variables.get(parent_id)
        if child is None or parent is None:
            continue
        edge_id = int(row["edgeid"])
        label = str(row["label"])
        edge_labels[edge_id] = label
        child.parents.append(
            VariableParentInfo(
                edge_id=edge_id,
                label=label,
                variable_id=parent_id,
                reference=VariableRef(str(row["parent_dataset_name"]), str(row["parent_name"])),
                identity_steps=bool(row["identity_steps"]),
            )
        )
        parent.children.append(VariableRef(str(row["child_dataset_name"]), str(row["child_name"])))

    chunks_by_id: dict[int, VariableChunkInfo] = {}
    chunk_rows = sql_execute(
        cur,
        "select chunk.chunkid, chunk.variableid, chunk.chunk_index, chunk.payload_datasetid, "
        "payload.name as payload_name, payload.uuid as payload_uuid, payload.fileformat as payload_fileformat "
        "from variable_chunk as chunk "
        "join dataset as payload on payload.rowid = chunk.payload_datasetid and payload.deltime = 0 "
        "order by chunk.variableid, chunk.chunk_index",
    )
    for row in chunk_rows:
        variable = info_data.variables.get(int(row["variableid"]))
        if variable is None:
            continue
        chunk = VariableChunkInfo(
            id=int(row["chunkid"]),
            chunk_index=int(row["chunk_index"]),
            payload_dataset_id=int(row["payload_datasetid"]),
            payload_dataset_name=str(row["payload_name"]),
            payload_dataset_uuid=str(row["payload_uuid"]),
            payload_file_format=str(row["payload_fileformat"]),
        )
        variable.chunks.append(chunk)
        chunks_by_id[chunk.id] = chunk

    source_rows = sql_execute(
        cur,
        "select chunkid, edgeid, source_step from variable_chunk_source_step order by chunkid, edgeid",
    )
    for row in source_rows:
        mapped_chunk = chunks_by_id.get(int(row["chunkid"]))
        edge_label = edge_labels.get(int(row["edgeid"]))
        if mapped_chunk is not None and edge_label is not None:
            mapped_chunk.source_steps[edge_label] = int(row["source_step"])


def collect_info(  # pylint: disable=too-many-locals,too-many-statements
    args: argparse.Namespace, con: sqlite3.Connection
) -> InfoResult:
    cur = con.cursor()
    res = sql_execute(cur, "select id, name, version, modtime from info")
    row = res.fetchone()
    info_datasets = InfoResult(
        archive=ArchiveInfo(
            id=row[0],
            name=row[1],
            version=row[2],
            mod_time=row[3],
        )
    )

    #
    # Hosts and directories
    #
    delete_condition_where = " where deltime = 0"
    delete_condition_and = " and deltime = 0"
    if args.show_deleted:
        delete_condition_where = ""
        delete_condition_and = ""
    res = sql_execute(
        cur,
        "select rowid, hostname, longhostname from host" + delete_condition_where + " order by rowid",
    )
    hosts = res.fetchall()
    dirs_archived: dict[int, bool] = {}
    for host in hosts:
        host_info = HostInfo(
            id=host[0],
            hostname=host[1],
            long_hostname=host[2],
        )
        res2 = sql_execute(
            cur,
            "select rowid, name, modtime, deltime from directory "
            + 'where hostid = "'
            + str(host[0])
            + '"'
            + delete_condition_and
            + " order by rowid",
        )
        dirs = res2.fetchall()
        for dirrec in dirs:
            if dirrec[3] == 0 or args.show_deleted:
                # check if it's archive dir
                res3 = sql_execute(
                    cur,
                    f"select rowid, tarname, system from archive where dirid = {dirrec[0]} order by rowid",
                )
                archs = res3.fetchall()
                archive_entries: list[ArchiveEntry] = []
                for arch in archs:
                    archive_entries.append(ArchiveEntry(id=arch[0], tar_name=arch[1], system=arch[2]))
                has_archive = bool(archive_entries)
                dirs_archived[dirrec[0]] = has_archive
                host_info.directories.append(
                    DirectoryInfo(
                        id=dirrec[0],
                        name=dirrec[1],
                        mod_time=dirrec[2],
                        del_time=dirrec[3],
                        archives=archive_entries,
                        has_archive=has_archive,
                    )
                )
        info_datasets.hosts.append(host_info)

    #
    # Keys
    #
    res = sql_execute(cur, "select rowid, keyid from key order by rowid")
    keys = res.fetchall()
    for key in keys:
        info_datasets.keys.append(KeyInfo(id=key[0], key=key[1]))

    #
    # Time Series
    #
    res_ts = sql_execute(cur, "select tsid, name from timeseries order by tsid")
    for ts in res_ts:
        ts_id = int(ts[0])
        ts_info = TimeSeriesInfo(name=ts[1])
        info_datasets.time_series[ts_id] = ts_info

    #
    # Datasets
    #
    if not args.list_replicas and not args.list_files:
        res_ds = sql_execute(
            cur,
            "select rowid, uuid, name, modtime, deltime, fileformat, tsid from dataset "
            "where fileformat != 'VARIABLES'" + delete_condition_and + " order by rowid",
        )
        for dataset in res_ds:
            dataset_id = int(dataset[0])
            dataset_info = DatasetInfo(
                uuid=dataset[1],
                name=dataset[2],
                mod_time=dataset[3],
                del_time=dataset[4],
                file_format=dataset[5],
            )
            tsid = dataset[6]
            if tsid > 0:
                info_datasets.time_series[tsid].datasets[dataset_id] = dataset_info
            else:
                info_datasets.datasets[dataset_id] = dataset_info
    else:
        info_datas(args, info_datasets, cur, dirs_archived)
        info_texts(args, info_datasets, cur, dirs_archived)
        info_images(args, info_datasets, cur, dirs_archived)

    info_variables(info_datasets, cur)
    return info_datasets


def format_info_dataset_lines(  # pylint: disable=too-many-locals
    dataset_info: DatasetInfo,
) -> list[str]:
    lines = []
    time_str = timestamp_to_str(dataset_info.mod_time)
    dataset_line = f"    {dataset_info.uuid}   {dataset_info.file_format:6}  {time_str}   {dataset_info.name}"
    if dataset_info.del_time > 0:
        dataset_line += f"  - deleted {timestamp_to_str(dataset_info.del_time)}"
    lines.append(dataset_line)

    for replica_id, replica_info in dataset_info.replicas.items():
        flags = replica_info.flags
        flag_del = "D" if flags.deleted else "-"
        flag_encrypted = "k" if flags.encrypted else "-"
        flag_accuracy = "a" if flags.accuracy else "-"
        flag_archive = "A" if flags.archive else "-"
        flag_remote = "e" if flags.embedded else "r"
        replica_line = (
            f"  {replica_id:>7} {flag_remote}{flag_encrypted}{flag_accuracy}{flag_archive}{flag_del} "
            f"{replica_info.dir_id}"
        )
        if replica_info.archive_id > 0:
            replica_line += f".{replica_info.archive_id}"
        else:
            replica_line += "  "

        if dataset_info.file_format == "IMAGE" and replica_info.resolution is not None:
            res = replica_info.resolution
            resolution_text = f" {res.x} x {res.y}".rjust(14)
        else:
            resolution_text = " ".rjust(14)
        replica_line += f"{resolution_text}"

        replica_line += f" {sizeof_fmt(replica_info.size):>11}  {timestamp_to_str(replica_info.mod_time)}"
        replica_line += f"      {replica_info.name}"
        if flags.deleted:
            replica_line += f"  - deleted {timestamp_to_str(replica_info.del_time)}"
        lines.append(replica_line)

        for file_info in replica_info.files:
            if replica_info.key_id > 0:
                prefix = " " * 30 + f"k{replica_info.key_id:<3}"
            else:
                prefix = " " * 34
            file_line = prefix + f"{sizeof_fmt(file_info.len_compressed):>11}  {timestamp_to_str(file_info.mod_time)}"
            if file_info.checksum:
                file_line += f"         {file_info.checksum}  {file_info.name}"
            else:
                file_line += f"         {file_info.name}"
            lines.append(file_line)

    return lines


def format_info(info_data: InfoResult) -> str:  # pylint: disable=too-many-statements
    lines = []
    archive_info = info_data.archive
    created_time = timestamp_to_str(archive_info.mod_time)
    lines.append(f"{archive_info.name}, version {archive_info.version}, created on {created_time}")
    lines.append("")

    lines.append("Hosts and directories:")
    for host_info in info_data.hosts:
        lines.append(f"  {host_info.hostname}   longhostname = {host_info.long_hostname}")
        for dir_info in host_info.directories:
            archive_system = "  "
            if dir_info.archives:
                archive_system = f"  - Archive: {dir_info.archives[0].system}"
            lines.append(f"     {dir_info.id}. {dir_info.name}{archive_system}")
            for archive_entry in dir_info.archives:
                tar_name = archive_entry.tar_name if archive_entry.tar_name else "."
                lines.append(f"       {dir_info.id}.{archive_entry.id} {tar_name}")
    lines.append("")

    if info_data.keys:
        lines.append("Encryption keys:")
        for key_info in info_data.keys:
            lines.append(f"  k{key_info.id}. {key_info.key}")
        lines.append("")

    if info_data.time_series:
        lines.append("Time-series and their datasets:")
        for _ts_id, ts_info in info_data.time_series.items():
            lines.append(f"  {ts_info.name}")
            for _ds_id, dataset_info in sorted(ts_info.datasets.items()):
                lines.extend(format_info_dataset_lines(dataset_info))
        lines.append("")

    if info_data.datasets:
        lines.append("Other Datasets:")
        for _ds_id, dataset_info in sorted(info_data.datasets.items()):
            lines.extend(format_info_dataset_lines(dataset_info))
        lines.append("")

    if info_data.variables:
        lines.append("Variables:")
        for _variable_id, variable in sorted(info_data.variables.items()):
            kind = f"   kind={variable.representation_kind}" if variable.representation_kind else ""
            lines.append(f"  {variable.dataset}/{variable.variable}{kind}")
            for parent in variable.parents:
                lines.append(
                    f"      representation_of[{parent.label}]: {parent.reference.dataset}/{parent.reference.variable}"
                )
            if variable.chunks:
                lines.append(f"      chunks: {len(variable.chunks)}")
            if variable.preferred_preview is not None:
                lines.append(
                    f"      preferred preview: {variable.preferred_preview.dataset}/"
                    f"{variable.preferred_preview.variable}"
                )
        lines.append("")

    return "\n".join(lines)


def print_info(info_data: InfoResult):
    output_text = format_info(info_data)
    if output_text:
        print(output_text)
