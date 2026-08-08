# Backup and restore

A supported backup contains one complete idle workspace. It keeps config,
profiles, rules, rates, account mappings, saved corrections, every import
record and attempt, the workspace index, generated views, and any recovery
proof together.

Before copying, stop all Honeymoney commands and run `honeymoney doctor`. Do not
copy a workspace with an active lock or publication journal. Copy the workspace
root as one unit and preserve file modes. Check the copied bytes and run doctor
against a separate restored copy when testing the backup.

Restore into an empty location. Copy the complete backup, keep its directory
structure and owner-only modes, then run `honeymoney doctor`. Do not merge two
workspaces, restore selected import records, replace only the index, or restore
over a damaged workspace. Those actions can split identity and attempt history.

Generated views do not replace durable state. A backup should still include
them so byte checks and recovery evidence describe one generation.
