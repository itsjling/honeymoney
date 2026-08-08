# Honeymoney

Honeymoney turns household statement sources into reviewed financial records and
period-based views while keeping the data local.

## Language

**Import record**:
The durable lifecycle record for one logical statement source. It holds
the last successful normalized facts, if any, and retains each import attempt's
outcome. Its readiness comes from whether it has a current successful attempt.
It does not own a copy of the original statement.
_Avoid_: Import report, imported file

**Import attempt**:
One accepted request to import, replace, or reset a logical statement source.
Preflight rejects invalid commands before an attempt starts. An attempt's
outcome does not by itself change the source's last successful normalized facts.
It succeeds only when the new source facts and linked state finish saving.
Pending review and unavailable optional local categorization do not make it
fail.
_Avoid_: Import record, source

**Output period**:
The calendar month that contains a transaction's posting date. If no valid
posting date exists, it uses the transaction date. A transaction with neither
date belongs to the undated view.
_Avoid_: Statement period, import month

**Undated view**:
The generated view for transactions without a valid posted date. It is not an
output period.
_Avoid_: Import month, unknown month

**Generated view**:
A replaceable rendering of current import records and durable user decisions.
Edits to a generated view do not become source facts or saved decisions.
_Avoid_: Ledger, record

**Statement transaction**:
A normalized transaction stored in one import record before overlap handling.
_Avoid_: Source occurrence, raw transaction

**View transaction**:
A transaction rendered in a generated view after overlap handling.
Its stable workspace identity does not change when support grows from one
import record to several.
_Avoid_: Final transaction, canonical record

**Saved correction**:
A workspace-wide human decision attached to a stable view transaction.
It does not belong to one import record or output period, and it stays saved
when that transaction is no longer current.
_Avoid_: Output edit, import correction

**Workspace index**:
The workspace-wide, value-free record of stable source, statement-transaction,
and overlap identity, plus duplicate decisions bound to the exact supporting
records. It keeps active and retired identities for the life of the workspace
so imports and saved corrections can reconnect without guessing.
_Avoid_: Cumulative ledger, transaction store

**Workspace generation**:
One checked set of durable state and generated views published by one accepted
state-changing command. The workspace index commits the generation last.
_Avoid_: File update, ledger generation

**Publication journal**:
The checked recovery record that holds the exact old and new bytes needed to
settle a stopped workspace generation. Normal commands fail while it remains;
only `doctor --fix` may settle it.
_Avoid_: Write cache, transaction log

**Registered view**:
A calendar-month or undated view recorded in the workspace index. Registration
lets a full rebuild remove a view that current durable facts no longer imply.
_Avoid_: Report month, range folder
