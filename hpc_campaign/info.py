import argparse
import json
import sqlite3
from dataclasses import dataclass, field

from .utils import sizeof_fmt, sql_execute, timestamp_to_str

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


@dataclass
class VisualizationVariableInfo:
    """Variable-role association within a visualization sequence."""

    name: str
    role: str
    source_dataset_id: int
    source_dataset_name: str


@dataclass
class VisualizationItemInfo:
    """Explicit sequence item reference."""

    item_order: int
    item_type: str
    item_uuid: str
    metadata: str | None
    dataset_id: int | None
    dataset_name: str | None
    file_format: str | None


@dataclass
class VisualizationSequenceInfo:  # pylint: disable=too-many-instance-attributes
    """Visualization sequence metadata entry."""

    name: str
    vis_type: str
    thumbnail_item_uuid: str | None
    thumbnail_dataset_id: int | None
    thumbnail_dataset_name: str | None
    metadata: str | None
    variables: list[VisualizationVariableInfo] = field(default_factory=list)
    items: list[VisualizationItemInfo] = field(default_factory=list)


@dataclass
class RepresentationSourceInfo:
    """Ground-truth variable used to produce a representation."""

    id: int
    dataset_id: int
    dataset_name: str
    variable_name: str
    label: str
    metadata: dict = field(default_factory=dict)


@dataclass
class RepresentationMetricInfo:
    """Named per-item or aggregate representation accuracy metric."""

    name: str
    value: float
    units: str | None
    norm: str | None
    relative: bool
    metadata: dict = field(default_factory=dict)


@dataclass
class RepresentationItemInfo:  # pylint: disable=too-many-instance-attributes
    """One independently addressable item or timestep in a representation."""

    id: int
    item_order: int
    dataset_id: int
    dataset_name: str
    dataset_uuid: str
    file_format: str
    logical_time: float | None
    metadata: dict = field(default_factory=dict)
    source_selections: dict[str, dict] = field(default_factory=dict)
    metrics: list[RepresentationMetricInfo] = field(default_factory=list)


@dataclass
class RepresentationInfo:  # pylint: disable=too-many-instance-attributes
    """Alternate scalar-data representation and its provenance."""

    name: str
    field_name: str
    representation_format: str
    temporal_interpolation: str
    parameter_correspondence: str
    metadata: dict = field(default_factory=dict)
    sources: dict[int, RepresentationSourceInfo] = field(default_factory=dict)
    items: list[RepresentationItemInfo] = field(default_factory=list)
    metrics: list[RepresentationMetricInfo] = field(default_factory=list)


@dataclass
class InfoResult:
    """Aggregated archive information."""

    archive: ArchiveInfo
    hosts: list[HostInfo] = field(default_factory=list)
    keys: list[KeyInfo] = field(default_factory=list)
    time_series: dict[int, TimeSeriesInfo] = field(default_factory=dict)
    visualization_sequences: dict[int, VisualizationSequenceInfo] = field(default_factory=dict)
    representations: dict[int, RepresentationInfo] = field(default_factory=dict)
    datasets: dict[int, DatasetInfo] = field(default_factory=dict)


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

SELECT_SCALAR_FIELDS_CMD = """
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

    sf.metadata         AS scalar_metadata

FROM dataset AS d
JOIN replica AS r
    ON r.datasetid = d.rowid
LEFT JOIN repfiles AS rf
    ON rf.replicaid = r.rowid
LEFT JOIN file AS f
    ON f.fileid = rf.fileid
LEFT JOIN scalar_field AS sf
    ON sf.datasetid = d.rowid
WHERE d.fileformat = 'SCALAR_FIELD'
ORDER BY d.rowid, r.rowid, f.fileid;
"""

SELECT_GAUSSIAN_SPLATS_CMD = """
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

    gs.metadata         AS gaussian_splat_metadata

FROM dataset AS d
JOIN replica AS r
    ON r.datasetid = d.rowid
LEFT JOIN repfiles AS rf
    ON rf.replicaid = r.rowid
LEFT JOIN file AS f
    ON f.fileid = rf.fileid
LEFT JOIN gaussian_splat AS gs
    ON gs.datasetid = d.rowid
WHERE d.fileformat = 'GAUSSIAN_SPLAT'
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


def info_scalar_fields(  # pylint: disable=too-many-locals
    args: argparse.Namespace,
    info_data: InfoResult,
    cur: sqlite3.Cursor,
    dirs_archived: dict[int, bool],
):
    res_tables = sql_execute(cur, "select name from sqlite_master where type = 'table' and name = 'scalar_field'")
    if res_tables.fetchone() is None:
        return

    res = sql_execute(cur, SELECT_SCALAR_FIELDS_CMD)
    for row in res:
        dataset_info = info_row(
            args,
            info_data,
            row,
            accuracy=False,
            embedded=(row["repfile_id"] is not None),
            resolution=None,
            dirs_archived=dirs_archived,
        )
        if dataset_info is None:
            continue
        if row["scalar_metadata"]:
            try:
                metadata = json.loads(row["scalar_metadata"])
                dataset_info.metadata = metadata if isinstance(metadata, dict) else None
            except (json.JSONDecodeError, TypeError):
                dataset_info.metadata = None


def info_gaussian_splats(  # pylint: disable=too-many-locals
    args: argparse.Namespace,
    info_data: InfoResult,
    cur: sqlite3.Cursor,
    dirs_archived: dict[int, bool],
):
    res_tables = sql_execute(cur, "select name from sqlite_master where type = 'table' and name = 'gaussian_splat'")
    if res_tables.fetchone() is None:
        return

    res = sql_execute(cur, SELECT_GAUSSIAN_SPLATS_CMD)
    for row in res:
        dataset_info = info_row(
            args,
            info_data,
            row,
            accuracy=False,
            embedded=(row["repfile_id"] is not None),
            resolution=None,
            dirs_archived=dirs_archived,
        )
        if dataset_info is None:
            continue
        if row["gaussian_splat_metadata"]:
            try:
                metadata = json.loads(row["gaussian_splat_metadata"])
                dataset_info.metadata = metadata if isinstance(metadata, dict) else None
            except (json.JSONDecodeError, TypeError):
                dataset_info.metadata = None


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


def _info_json_object(value: str | None) -> dict:
    if not value:
        return {}
    try:
        result = json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return {}
    return result if isinstance(result, dict) else {}


def info_representations(info_data: InfoResult, cur: sqlite3.Cursor) -> None:  # pylint: disable=too-many-locals
    required_tables = {
        "representation",
        "representation_source",
        "representation_item",
        "representation_item_source",
        "representation_metric",
    }
    rows = sql_execute(
        cur,
        "select name from sqlite_master where type = 'table' and name in "
        "('representation', 'representation_source', 'representation_item', "
        "'representation_item_source', 'representation_metric')",
    ).fetchall()
    if {str(row[0]) for row in rows} != required_tables:
        return

    representation_rows = sql_execute(
        cur,
        "select repid, name, field_name, format, temporal_interpolation, "
        "parameter_correspondence, metadata from representation order by repid",
    )
    for row in representation_rows:
        repid = int(row["repid"])
        info_data.representations[repid] = RepresentationInfo(
            name=str(row["name"]),
            field_name=str(row["field_name"]),
            representation_format=str(row["format"]),
            temporal_interpolation=str(row["temporal_interpolation"]),
            parameter_correspondence=str(row["parameter_correspondence"]),
            metadata=_info_json_object(row["metadata"]),
        )

    sources_by_id: dict[int, tuple[int, RepresentationSourceInfo]] = {}
    source_rows = sql_execute(
        cur,
        "select rs.sourceid, rs.repid, rs.datasetid, d.name as dataset_name, "
        "rs.variable_name, rs.label, rs.metadata "
        "from representation_source as rs join dataset as d on d.rowid = rs.datasetid "
        "order by rs.repid, rs.sourceid",
    )
    for row in source_rows:
        repid = int(row["repid"])
        representation = info_data.representations.get(repid)
        if representation is None:
            continue
        source = RepresentationSourceInfo(
            id=int(row["sourceid"]),
            dataset_id=int(row["datasetid"]),
            dataset_name=str(row["dataset_name"]),
            variable_name=str(row["variable_name"]),
            label=str(row["label"]),
            metadata=_info_json_object(row["metadata"]),
        )
        representation.sources[source.id] = source
        sources_by_id[source.id] = (repid, source)

    items_by_id: dict[int, tuple[int, RepresentationItemInfo]] = {}
    item_rows = sql_execute(
        cur,
        "select ri.itemid, ri.repid, ri.item_order, ri.datasetid, ri.logical_time, ri.metadata, "
        "d.name as dataset_name, d.uuid as dataset_uuid, d.fileformat as file_format "
        "from representation_item as ri join dataset as d on d.rowid = ri.datasetid "
        "order by ri.repid, ri.item_order",
    )
    for row in item_rows:
        repid = int(row["repid"])
        representation = info_data.representations.get(repid)
        if representation is None:
            continue
        item = RepresentationItemInfo(
            id=int(row["itemid"]),
            item_order=int(row["item_order"]),
            dataset_id=int(row["datasetid"]),
            dataset_name=str(row["dataset_name"]),
            dataset_uuid=str(row["dataset_uuid"]),
            file_format=str(row["file_format"]),
            logical_time=float(row["logical_time"]) if row["logical_time"] is not None else None,
            metadata=_info_json_object(row["metadata"]),
        )
        representation.items.append(item)
        items_by_id[item.id] = (repid, item)

    selection_rows = sql_execute(
        cur,
        "select ris.itemid, ris.sourceid, ris.selection "
        "from representation_item_source as ris order by ris.itemid, ris.sourceid",
    )
    for row in selection_rows:
        item_record = items_by_id.get(int(row["itemid"]))
        source_record = sources_by_id.get(int(row["sourceid"]))
        if item_record is None or source_record is None or item_record[0] != source_record[0]:
            continue
        item_record[1].source_selections[source_record[1].label] = _info_json_object(row["selection"])

    metric_rows = sql_execute(
        cur,
        "select repid, itemid, name, value, units, norm, relative, metadata "
        "from representation_metric order by repid, metricid",
    )
    for row in metric_rows:
        metric = RepresentationMetricInfo(
            name=str(row["name"]),
            value=float(row["value"]),
            units=str(row["units"]) if row["units"] is not None else None,
            norm=str(row["norm"]) if row["norm"] is not None else None,
            relative=bool(row["relative"]),
            metadata=_info_json_object(row["metadata"]),
        )
        if row["itemid"] is None:
            representation = info_data.representations.get(int(row["repid"]))
            if representation is not None:
                representation.metrics.append(metric)
        else:
            item_record = items_by_id.get(int(row["itemid"]))
            if item_record is not None:
                item_record[1].metrics.append(metric)


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
            + delete_condition_where
            + " order by rowid",
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
                info_datasets.time_series[ts_id].datasets[dataset_id] = dataset_info
            else:
                info_datasets.datasets[dataset_id] = dataset_info
    else:
        info_datas(args, info_datasets, cur, dirs_archived)
        info_texts(args, info_datasets, cur, dirs_archived)
        info_images(args, info_datasets, cur, dirs_archived)
        info_scalar_fields(args, info_datasets, cur, dirs_archived)
        info_gaussian_splats(args, info_datasets, cur, dirs_archived)

    res_tables = sql_execute(
        cur,
        "select name from sqlite_master where type = 'table' and name in "
        "('visualization_sequence', 'visualization_variable', 'visualization_item')",
    )
    available_tables = {row[0] for row in res_tables.fetchall()}

    if "visualization_sequence" in available_tables:
        res_vis = sql_execute(
            cur,
            "select vs.visid, vs.name, vs.vistype, vs.thumbnail_itemuuid, vs.metadata, "
            "thumb.rowid as thumbnail_datasetid, thumb.name as thumbnail_name "
            "from visualization_sequence as vs "
            "left join dataset as thumb on thumb.uuid = vs.thumbnail_itemuuid and thumb.deltime = 0 "
            "order by vs.visid",
        )
        for row in res_vis:
            vis_id = int(row["visid"])
            info_datasets.visualization_sequences[vis_id] = VisualizationSequenceInfo(
                name=row["name"],
                vis_type=row["vistype"],
                thumbnail_item_uuid=row["thumbnail_itemuuid"],
                thumbnail_dataset_id=(
                    int(row["thumbnail_datasetid"]) if row["thumbnail_datasetid"] is not None else None
                ),
                thumbnail_dataset_name=row["thumbnail_name"],
                metadata=row["metadata"],
            )

    if "visualization_variable" in available_tables:
        res_vis_vars = sql_execute(
            cur,
            "select vv.visid, vv.datasetid, d.name as dataset_name, vv.variable_name, vv.role "
            "from visualization_variable as vv "
            "join dataset as d on d.rowid = vv.datasetid "
            "order by vv.visid, vv.datasetid, vv.role, vv.variable_name",
        )
        for row in res_vis_vars:
            vis_id = int(row["visid"])
            sequence_info = info_datasets.visualization_sequences.get(vis_id)
            if sequence_info is None:
                continue
            sequence_info.variables.append(
                VisualizationVariableInfo(
                    name=row["variable_name"],
                    role=row["role"],
                    source_dataset_id=int(row["datasetid"]),
                    source_dataset_name=row["dataset_name"],
                )
            )

    if "visualization_item" in available_tables:
        res_vis_items = sql_execute(
            cur,
            "select vi.visid, vi.item_order, vi.item_type, vi.item_uuid, vi.metadata, "
            "d.rowid as datasetid, d.name as dataset_name, d.fileformat as dataset_fileformat "
            "from visualization_item as vi "
            "left join dataset as d on d.uuid = vi.item_uuid and d.deltime = 0 "
            "order by vi.visid, vi.item_order",
        )
        for row in res_vis_items:
            vis_id = int(row["visid"])
            sequence_info = info_datasets.visualization_sequences.get(vis_id)
            if sequence_info is None:
                continue
            sequence_info.items.append(
                VisualizationItemInfo(
                    item_order=int(row["item_order"]),
                    item_type=row["item_type"],
                    item_uuid=row["item_uuid"],
                    metadata=row["metadata"],
                    dataset_id=int(row["datasetid"]) if row["datasetid"] is not None else None,
                    dataset_name=row["dataset_name"],
                    file_format=row["dataset_fileformat"],
                )
            )

    info_representations(info_datasets, cur)
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

    if info_data.representations:
        lines.append("Data Representations:")
        for _representation_id, representation in sorted(info_data.representations.items()):
            lines.append(
                f"  {representation.name}   field={representation.field_name} "
                f"format={representation.representation_format}"
            )
            lines.append(
                f"      temporal interpolation: {representation.temporal_interpolation}; "
                f"parameter correspondence: {representation.parameter_correspondence}"
            )
            for source in representation.sources.values():
                lines.append(
                    f"      source {source.label}: {source.variable_name} "
                    f"(dataset {source.dataset_name})"
                )
            lines.append(f"      items: {len(representation.items)}")
            for item in representation.items[:3]:
                time_text = f" time={item.logical_time:g}" if item.logical_time is not None else ""
                lines.append(
                    f"      item[{item.item_order}]: {item.dataset_name} ({item.file_format}){time_text}"
                )
                for label, selection in sorted(item.source_selections.items()):
                    lines.append(f"          {label}: {json.dumps(selection, sort_keys=True)}")
                for metric in item.metrics:
                    lines.append(f"          metric {metric.name}: {metric.value:g}")
            if len(representation.items) > 3:
                lines.append(f"      ... {len(representation.items) - 3} more item(s)")
            for metric in representation.metrics:
                lines.append(f"      aggregate metric {metric.name}: {metric.value:g}")
            if representation.metadata:
                lines.append(f"      metadata: {json.dumps(representation.metadata, sort_keys=True)}")
        lines.append("")

    if info_data.visualization_sequences:
        lines.append("Visualization Sequences:")
        for _vis_id, sequence_info in sorted(info_data.visualization_sequences.items()):
            header = f"  {sequence_info.name}   type={sequence_info.vis_type}"
            lines.append(header)
            if sequence_info.thumbnail_dataset_name:
                thumb_desc = sequence_info.thumbnail_dataset_name
                if sequence_info.thumbnail_item_uuid:
                    thumb_desc += f" ({sequence_info.thumbnail_item_uuid})"
                lines.append(f"      thumbnail: {thumb_desc}")
            if sequence_info.variables:
                seen_sources: list[str] = []
                for variable_info in sequence_info.variables:
                    if variable_info.source_dataset_name not in seen_sources:
                        seen_sources.append(variable_info.source_dataset_name)
                lines.append(f"      sources: {', '.join(seen_sources)}")
                for variable_info in sequence_info.variables:
                    lines.append(
                        f"      {variable_info.role}: {variable_info.name} "
                        + f"(dataset {variable_info.source_dataset_name})"
                    )
            if sequence_info.items:
                item_types = sorted({item.item_type for item in sequence_info.items})
                lines.append(f"      items: {len(sequence_info.items)} ({', '.join(item_types)})")
                for item_info in sequence_info.items[:3]:
                    item_desc = item_info.item_uuid
                    if item_info.dataset_name:
                        item_desc += f" -> {item_info.dataset_name}"
                    lines.append(f"      item[{item_info.item_order}]: {item_info.item_type} {item_desc}")
                if len(sequence_info.items) > 3:
                    lines.append(f"      ... {len(sequence_info.items) - 3} more item(s)")
            if sequence_info.metadata:
                lines.append(f"      metadata: {sequence_info.metadata}")

    return "\n".join(lines)


def _collect_all_datasets(info_data: InfoResult) -> list[DatasetInfo]:
    datasets: list[DatasetInfo] = []
    for ts_info in info_data.time_series.values():
        datasets.extend(ts_info.datasets.values())
    datasets.extend(info_data.datasets.values())
    datasets.sort(key=lambda dataset: (dataset.name, dataset.file_format, dataset.uuid))
    return datasets


def _format_visualization_metadata(metadata: str | None) -> str | None:
    if not metadata:
        return None
    try:
        return json.dumps(json.loads(metadata), sort_keys=True)
    except json.JSONDecodeError:
        return metadata


def format_image_associations(info_data: InfoResult) -> str:
    lines = []
    sequences = list(info_data.visualization_sequences.values())
    image_datasets = [dataset for dataset in _collect_all_datasets(info_data) if dataset.file_format == "IMAGE"]

    for dataset in image_datasets:
        lines.append(f"{dataset.name}: {dataset.file_format}")

        matches: list[tuple[VisualizationSequenceInfo, bool]] = []
        for sequence in sequences:
            is_thumbnail = sequence.thumbnail_dataset_name == dataset.name
            in_sequence = any(
                item.dataset_name == dataset.name for item in sequence.items if item.dataset_name is not None
            )
            if is_thumbnail or in_sequence:
                matches.append((sequence, is_thumbnail))
        matches.sort(key=lambda item: item[0].name)

        if not matches:
            lines.append("  associations: none")
            continue

        for sequence, is_thumbnail in matches:
            lines.append(f"  sequence: {sequence.name}")
            lines.append(f"    vis_type: {sequence.vis_type}")
            if is_thumbnail:
                lines.append("    thumbnail: true")
            if sequence.variables:
                source_names: list[str] = []
                for variable in sequence.variables:
                    if variable.source_dataset_name not in source_names:
                        source_names.append(variable.source_dataset_name)
                lines.append(f"    sources: {', '.join(source_names)}")
                variables = ", ".join(
                    f"{variable.role}={variable.name}@{variable.source_dataset_name}" for variable in sequence.variables
                )
                lines.append(f"    variables: {variables}")
            item_uuids = ", ".join(item.item_uuid for item in sequence.items if item.dataset_name == dataset.name)
            if item_uuids:
                lines.append(f"    item_uuid: {item_uuids}")
            metadata = _format_visualization_metadata(sequence.metadata)
            if metadata:
                lines.append(f"    metadata: {metadata}")
        lines.append("")

    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines)


def print_info(info_data: InfoResult):
    output_text = format_info(info_data)
    if output_text:
        print(output_text)


def print_image_associations(info_data: InfoResult):
    output_text = format_image_associations(info_data)
    if output_text:
        print(output_text)
