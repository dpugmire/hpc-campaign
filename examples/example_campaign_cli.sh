#!/bin/bash
#
# run this in the hpc-campaign directory, so that data/heat.bp is a valid local path
# the output file will  be in the hpc-campaign directory, named example_cli.aca

# aca file with full path
ACA=$PWD/example_cli.aca

# wipe the aca (in case it exists) to start from scratch
# add a data file with a representation name
hpc_campaign manager $ACA --truncate data data/heat.bp  --name heat

# add an image sequence with one logical variable identity
printf '%s\n' '{"dataset":"images","variable":"temperature","images":["data/T*.png"],"store":true,"thumbnail":[64,64]}' > /tmp/hpc-campaign-images.json
hpc_campaign manager $ACA image-sequence /tmp/hpc-campaign-images.json

# add a text file
hpc_campaign manager $ACA text data/readme  --name readme --store

# print info about the aca
hpc_campaign manager $ACA info -rfdc

# add an archival storage to the aca
# faking it, since the path does not exist
hpc_campaign manager $ACA add-archival-storage fs faketape $PWD/data/archive  2>&1 | tee log.archive

faketape_dirID=`grep "Archive storage added" log.archive | sed -e "s/^.*directory id = //" -e "s/ *archive id.*$//"`
echo "faketape_dirID from stdout:" ${faketape_dirID}

# determine the dirid of the newly inserted archival storage in three steps
hpc_campaign manager $ACA info -rfdc 2>&1 | tee log.1 
faketape_dirID=`grep -A1 faketape log.1 | tail -1 | sed -e "s/^ *//" -e "s/\..*$//"`
rm -rf log.1
echo "faketape_dirID from info log:" ${faketape_dirID}

# add a replica of the heat.bp located in the archival location 
# faking this, since there is no such location
hpc_campaign manager $ACA archived-replica heat ${faketape_dirID} --newpath archivedheat.bp 

# info
hpc_campaign manager $ACA info -rfdc 

# delete the aca
# hpc_campaign rm --campaign_store $PWD example_cli.aca
