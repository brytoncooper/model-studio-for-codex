# Extension installation lifecycle contract

This B20 engine boundary connects inspected immutable artifacts to durable
installation selection and supervised activation. [ports.py](ports.py) owns
internal immutable records and Protocols only. It implements no repository,
service, migration, broker authority mapping, process launch or public dispatch.
See [architecture lifecycle plan](../../../../../docs/plans/plugin-architecture/PLAN.md)
and [B20 backlog](../../../../../docs/plans/plugin-architecture/BACKLOG.md).

## Ownership and reuse

The future coordinator consumes these ports. SQLite owns durable claims, phases,
receipts and selected pointers. Plugin adapters reuse public archive inspection,
artifact staging, manifest inspection and lifecycle-session/process APIs. Data
adapters own namespace freeze and migration; they must not expose private SQL.
Authority composition owns permission interpretation and token issuance. Scope
strings here are exact opaque tokens, never inferred grants. No bearer tokens or
filesystem paths occur in durable records. Opaque references are supervisor-owned
identifiers, not credentials or caller-provided proof of successful work.

## Records and admission

`LifecycleRequest` binds trusted principal, action, extension ID, expected revision,
request digest, required idempotency key and optional inspected candidate.
The digest must cover all semantic request fields, including candidate archive
hash and supplied expected revision. Path-based input is read once into bounded
bytes before hashing/inspection; do not later execute different bytes at that path.
`ExecutableArtifact` is an inspected identity/version/hash/permission summary;
it is not a trust or signature assertion. Entry-point and contribution resolution
remain artifact adapter checks before returning a candidate.

Repository `claim` first finds original receipt or pending claim by
principal/action/key, compares the digest, then evaluates current revision.
Exact completed retry returns the immutable ORIGINAL receipt even after later
updates/removal. Changed digest conflicts without mutation. Pending retry returns
IN_PROGRESS and is never authorization for a second worker to execute effects.
One operation owns an extension across all principals/actions/keys. Admission
captures previous selection. Absent installation revision is zero; removal keeps
a tombstone revision to prevent reinstall ABA. Non-install actions require an
existing non-removed installation. Install requires absence or a removed record.

`SelectedInstallation` binds executable, data reference, approved scopes, grant
generation and activation generation. Approved scopes must be a subset of the
candidate's requested scopes. Install approves none and selects INSTALLED, never
ENABLED. Updates preserve only the intersection of previously approved and newly
requested scopes; they never expand grants. Only CHANGE_GRANTS carries explicit
approved scopes, validated against requested scopes. Removed records retain data
and artifact references for recovery; public list/get must hide their internal
REMOVED status. Generations and extension revisions never roll backward.

All records are frozen and contain only immutable nested records/tuples/enums.
Scalar bounds reuse common UUID, opaque_ref, reverse_domain_id, revision and
idempotency_key schema definitions. Artifact hashes are lowercase SHA256; versions
match manifest version syntax and operator result maximum64; scopes are unique,
nonempty tokens at most64 characters, with at most64 scopes. No generic JSON
workflow payload is accepted.

## Phase paths and external effects

`allowed_next_phases` uses action and durably captured previous status; missing
status is rejected for UPDATE and CHANGE_GRANTS. It is the shared phase graph. Repository implementations
must also validate evidence bindings, operation ownership and phase revision CAS;
an allowed enum edge alone is not sufficient.

- All operations: CLAIMED → QUIESCED. Stop admission, drain or explicitly interrupt
  work, freeze old data under the SAME broker mutation barrier, capture its final
  revision. A write racing freeze is included or rejected. New install has no old
  activation/data and records this stage without inventing a freeze receipt.
- INSTALL and UPDATE of a non-enabled installation: QUIESCED → DATA_STAGED →
  SWITCHED. Initialize or migrate a copy using trusted data adapters only. NEVER
  execute plugin code, including activation validation or plugin migration hooks.
  Staged-data initial revision is the rollback baseline. Install approves none.
- UPDATE of ENABLED: QUIESCED → DATA_STAGED → ACTIVATION_VALIDATED. Keep old data
  intact. Validate non-serving candidate against exact selection; freeze candidate
  writes at validated_data_revision.
- ENABLE, or CHANGE_GRANTS of ENABLED: QUIESCED → ACTIVATION_VALIDATED, reusing
  selected data without migration. Grant changes retain prior enabled intent.
- CHANGE_GRANTS of non-enabled: QUIESCED → SWITCHED; no plugin execution.
- DISABLE/REMOVE: QUIESCED → SWITCHED. No candidate executable starts.
- Other paths: ACTIVATION_VALIDATED → SWITCHED → SETTLED.

`advance` persists intermediate evidence only. `switch` alone atomically changes
selected executable/data/scopes/generations, extension status/revision and phase.
It verifies the prior selection still matches the claim. Update retains prior
enabled intent; enable sets ENABLED; disable sets DISABLED; remove sets REMOVED.
New admission stays closed during switch. External `admit` synchronizes current
authority generations, revokes old authority and then opens admission ONLY for
ENABLED. Disabled/installed/removed records remain non-serving. `switch` persists the exact intended APPLIED receipt with selected state.
`settle` commits that original outcome and releases the claim only after synchronization.

No database transaction spans an external callback. External effects must be
idempotent by operation ID, with durable or safely repeatable evidence. Recovery
enumerates bounded pending operations; it never retries automatically. A recovery
owner first reconciles durable phase and external effect receipts. Persisted
activation references never imply a process/session survived restart. Composition MUST acquire the existing exclusive engine instance lock for this
DB before coordinator startup. Recovery executes serially per operation before
serving. The running service has one execution owner per admitted operation.
Phase CAS does not fence external effects and no distributed lease is implied.

## Abort, rollback and data-loss guard

Before SWITCHED, revoke staged authority and obtain trusted revocation evidence,
then atomically enter RESTORING with original previous selection and intended
ABORTED receipt. The claim remains held. Reopen old data/admission only after
revocation, then settle to ABORTED and publish the exact receipt/release the claim.
Failed install may have no selected record.
A failure without proven revocation retains the claim; do not manufacture proof.

After SWITCHED, use rollback, never abort. Freeze/revoke candidate first. Bind
`current_data` to selected data and actual committed revision under the write
barrier. Restore previous selection automatically only if data remains at the
validated/staged switch baseline (or unchanged frozen baseline for disable/remove).
Changed candidate data requires an explicit trusted export/resolution reference;
otherwise retain claim as RESOLUTION_REQUIRED. A rollback restores pointers with
NEW monotonic generations/revision and persists RESTORING plus the exact intended ROLLED_BACK receipt. Keep the claim
held through old data/authority/admission restoration, then settle to ROLLED_BACK.
It must never
resurrect the old activation token. A first install has no previous selection:
rollback produces a retained REMOVED tombstone with monotonic revision/generations
rather than deleting its data.

Both abort and rollback return pending `LifecycleOperation`, not a final receipt.
`intended_receipt` stores the outcome and exact restored record, so restart never
infers success/failure from current state. Recovery includes RESTORING even though
selected pointers already changed. A crash or restoration failure before settle
leaves the claim held, blocks another mutation and resumes synchronization under
the exclusive instance lease. Only settle publishes receipts and releases claims.

## Proposed wire correction, not implemented here

For the unreleased v1 contract: get.result requires revision; install/update
params require expected_revision; remove's existing expected_revision becomes
required; enable/disable/grants.change require idempotency_key. Other mutation
keys/revisions are already required. No schemas or generated bundles were edited.
Permission-to-authority mapping and executable migration format remain composition
work, not invented by this port. Public method authorization remains operator-only.

## Tests and extension

Focused contract tests: `python.tests.engine.test_extension_lifecycle_ports`.
They cover immutable bindings, malformed bounds, same-extension identity, exact
claim alternatives and phase paths. These tests do not prove SQLite atomicity,
write freezing, crash recovery, token revocation or end-to-end lifecycle behavior.
Implement each Protocol with discriminative integration tests before wiring public
methods. New phases or receipt meanings require coordinated consumer review.
