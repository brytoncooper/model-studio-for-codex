# V2 starts fresh

User scope decision: 2026-09-14. This decision supersedes prototype-import,
legacy saved-setup compatibility, and migration prerequisites in older plans,
research, acceptance criteria, and work assignments.

## Product goal

Deliver a separately built V2 with fresh application state and explicit setup.
The running prototype exists only to provide model/tool access during development.
Its saved models, settings, history, and databases do not need to be imported.
Preserve the useful product capabilities through the new public contracts.

## Removed requirements

- B11 prototype import, snapshot/export tooling, and import rollback rehearsal
  are removed from the delivery scope, not marked complete.
- B26 does not depend on B11 or compatibility with prototype launcher/token
  entrypoints solely to preserve the old setup.
- B27 qualifies a fresh V2 installation and an authorized switch of tool access;
  it does not migrate the prototype's saved state.
- Existing import/compatibility code may remain. This decision does not request
  deletion, replacement implementations, or reopening accepted work.

## Requirements retained

- Keep the running prototype, Codex, Cursor, router, credentials, and active
  tool sessions untouched while they provide development access. Separate V2
  state, sockets, outputs, and process ownership remain mandatory.
- V2 must persist its own new data, recover interrupted operations, safely
  update plugins, and support transactional upgrades of its own schemas with
  version bookkeeping and refusal of unsupported newer schemas. B26 must map
  evidence for these V2 storage guarantees; do not recreate prototype import
  as a prerequisite. Plugin update/recovery remains owned by B20.
- Retain host/provider contracts and helper identity checks required by V2.
  Removing prototype compatibility is not permission to remove working features.
- Switching or retiring the prototype requires separate user authorization and
  proven replacement model/tool access. A failed switch preserves V2-created
  data and restores development access; it does not require legacy-data import.

## Scheduling and accounting

B11 is removed from scope. B08's completed preview remains historical delivered
work, with no follow-on import obligation. Track 15 complete, 11 remaining,
1 blocked, and 1 removed across the original 28 IDs; removal is not completion.
B26 has no B11 dependency. Prioritize a fresh-setup-to-coding workflow through
B10 and B25, reusing the existing isolated V2 and provider evidence, alongside
independent remaining plugin and distribution work. Do not rerun completed
slices or build migration infrastructure to satisfy superseded requirements.

## Decisions approved 2026-09-15

- Python runtime: V2 keeps depending on a machine-installed Python 3.11 or newer, pinned. The V2 builder records the exact interpreter it validated, and the app refuses to start against any other interpreter. No bundled interpreter is planned; this pinning work belongs to B26 (unit C10 in NEXT-TASKS.md section 7) and B27 records the pinned path in its qualification.
- Signing: B26 signs with the personal Apple Development identity only (team 6W7ABL9KX8). No Developer ID and no notarization; distribution beyond the owner's machines is out of scope.
- Execution mode: B24 stays closed. Plugins run as trusted executable code and the UI states that; no restricted execution mode is claimed or planned.
