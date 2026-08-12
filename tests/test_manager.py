import subprocess
import sys
from pathlib import Path

from hpc_campaign import Manager, ls, rm
from hpc_campaign.info import format_info

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_manager_data_text_info_and_cli(tmp_path: Path):
    """Core ingestion and CLI info expose payloads and logical variables."""
    archive_name = "manager.aca"
    text_path = tmp_path / "notes.txt"
    text_path.write_text("campaign notes", encoding="utf-8")

    manager = Manager(archive_name, campaign_store=str(tmp_path))
    manager.open(create=True)
    manager.data(REPO_ROOT / "data" / "onearray.h5", name="output")
    manager.text(text_path, name="notes", store=True)
    manager.add_variable(dataset="output", variable="temp")

    info = manager.info(list_replicas=True, list_files=True, show_checksum=True)
    output = format_info(info)
    assert "output/temp" in output
    assert {dataset.name for dataset in info.datasets.values()} == {"output", "notes"}
    manager.close()

    command = [
        sys.executable,
        "-m",
        "hpc_campaign",
        "manager",
        "--campaign_store",
        str(tmp_path),
        archive_name,
        "info",
        "-rf",
    ]
    result = subprocess.run(command, check=True, capture_output=True, text=True, cwd=REPO_ROOT)
    assert "Variables:" in result.stdout
    assert "output/temp" in result.stdout


def test_ls_and_rm_api(tmp_path: Path):
    """Archive lifecycle helpers list and remove the selected campaign."""
    archive_name = "lifecycle.aca"
    manager = Manager(archive_name, campaign_store=str(tmp_path))
    manager.open(create=True)
    manager.close()

    assert ls(archive_name, campaign_store=str(tmp_path)) == [archive_name]
    rm(archive_name, campaign_store=str(tmp_path), force=True)
    assert not (tmp_path / archive_name).exists()
