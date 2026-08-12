from pathlib import Path

from PIL import Image

from hpc_campaign.info import format_info
from hpc_campaign.ls import ls
from hpc_campaign.manager import Manager
from hpc_campaign.rm import rm

repo_root = Path(__file__).resolve().parents[1]
campaign_store = repo_root
data_dir = repo_root / "data"

print(f"campaign_store = {repo_root}")
print(f"data_dir = {data_dir}")

api_archive = Path("example_api.aca")  #  will find it in repo_root
sample_dataset = data_dir / "onearray.h5"
readme_file = data_dir / "readme"
image_frames = [Image.new("RGB", (128, 96), color=color) for color in ("midnightblue", "royalblue", "lightskyblue")]


def main():
    manager = Manager(archive=str(api_archive), campaign_store=str(campaign_store))
    manager.open(create=True, truncate=True)
    assert repo_root.joinpath(api_archive).exists()
    manager.data(sample_dataset, name="output")
    temperature = manager.add_variable(dataset="output", variable="data")
    manager.add_image_sequence(
        dataset="images",
        variable="temperature",
        images=image_frames,
        representation_of=temperature,
        source_steps=[0, 1, 2],
        representation_metadata={"visualization": "temperature colormap"},
        store=True,
        thumbnail=[64, 64],
    )
    manager.text(str(readme_file), name="readme", store=True)

    host_id, dir_id, archive_id = manager.add_archival_storage(
        system="fs", host="faketape", directory=str(data_dir / "archive")
    )
    print(f"Archive storage added: host id = {host_id}, directory id = {dir_id} archive id = {archive_id}")

    # add a replica of onearray.h5 located in the archival location
    # note that there is no such file, we are faking this record
    manager.archived_replica("output", dir_id, archiveid=archive_id, newpath="archived-onearray.h5")

    info_data = manager.info(True, False, False, False)
    output = format_info(info_data)
    print(output)
    manager.close()

    # ls this aca
    result = ls(str(api_archive), campaign_store=str(campaign_store))
    print(f"ls result: {result}")
    assert len(result) == 1
    assert result[0] == str(api_archive)

    # rm this aca
    result = rm(str(api_archive), campaign_store=str(campaign_store), interactive=True)
    print(f"rm result: {result}")
    assert result == [] or result == [str(api_archive)]


if __name__ == "__main__":
    main()
