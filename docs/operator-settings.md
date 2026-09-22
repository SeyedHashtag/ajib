# Operator files and account-creation defaults

Verified administrators can request complete logs and state backups in their
private bot chats. Attachments use neutral names. Authorization is checked again
at delivery; groups, customers and reseller portals cannot receive these files.
Keep downloaded files private because their contents include operational data.

For a managed website installation, the Telegram **VPN Servers** menu includes
**Inbounds for new accounts** for each 3x-ui server. Select one or more available
inbounds, review the selection, then apply it. Existing accounts are unchanged.
The same coordinated operation is available through the CLI:

```sh
ajib settings inbounds SERVER_ID --inbound-id 1 --inbound-id 2
ajib settings inbounds SERVER_ID --inbound-id 1 --inbound-id 2 --yes
```

Omitting `--yes` previews and validates the operation. Applying briefly pauses
this application's runtimes, retains prior activity/access gates, and refreshes
bot, hosted workers, API and worker configuration. Stale selections and active
migrations block conflicting edits. Account operations retain their original
inbound selection across interruptions.

To restore a missing crypto merchant ID from private configuration history while
preserving the installed API key:

```sh
ajib settings restore-crypto --source /private/configuration-backup.tar.gz
ajib settings restore-crypto --source /private/configuration-backup.tar.gz --yes
```

The source must be a private regular configuration file or configuration archive.
A unique historical merchant must match the installed API key. This is not a
general credential rotation command. Normal configuration synchronization still
rejects secret replacement or removal.

Interrupted settings changes leave financial processing paused and a private
recovery journal. Use `ajib settings recover --yes` to finish the original change;
do not restore an old database. If only the completion notification failed, use
`ajib settings retry-notification /private/result.json --yes`. This retries the
notification without repeating configuration changes.

Store real deployment evidence, file locations, hostnames and identities outside
tracked files. These instructions intentionally contain no installation inventory.
