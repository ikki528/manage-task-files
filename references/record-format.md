# Records and lifecycle

This reference describes the manager's contract. Use the current script's JSON output and implementation as the exact field/schema authority. Do not hand-edit registry entries to authorize cleanup.

## Storage boundaries

State is stable and separate from skill code and managed task directories. The program writes its central JSON atomically while holding an operating-system lock. Stored timestamps are Unix seconds derived from UTC. The registry and logs are program state, not disposable task artifacts.

Each managed task is associated with an enrolled workspace and a generated task ID. Each resource is a newly reserved directory under `workspace/work/managed-task-files/taskID/`; its generated resource ID and ownership marker distinguish it from a later directory reusing the same path. The program returns the exact path; callers must not construct it from a label.

Logical records include:

| Record | Meaning |
| --- | --- |
| Enrolled root | Absolute boundary and enabled state |
| Task | ID, workspace, label, optional chat/session ID, activity/lease information, delivery references |
| Resource | ID, task, path, name, kind, state, sealed inventory, retirement reason and deadline |
| Delivery | Protected file path, verification information, and explicit required resource IDs; recording it does not move the deliverable |
| Operation journal | Progress needed to diagnose an interrupted cleanup |

`scratch` means declared disposable intermediates. `working` means necessary unfinished or editable material. A directory name or age alone does not establish either classification.

## Lifecycle invariants

1. A resource is reserved before its producer writes to it. Existing directories are not adopted as disposable resources.
2. `seal` inventories contents and records file identity and content information. Sealing does not retire the resource.
3. Retirement is allowed only for unchanged, unpinned, sealed `scratch` content in an idle task, after the task has a verified file delivery and producers have released usage. `publish --requires RESOURCE_ID` pins managed dependencies. Required source/assets must be retained explicitly. Published references must be regular single-link files with no reparse-point parents.
4. Retirement starts a single seven-day minimum. Files stay in place until purge. `keep`/`restore` cancels retirement and clears the seal; `keep` also pins the resource. `resume` cancels all task retirements and clears seals while retaining existing pins. Reassessment and resealing are needed before future retirement.
5. `finish` marks a task idle. It neither retires resources nor treats a completed turn as a completed project.
6. A sweep previews or executes the configured mode. It rechecks root state, ownership, path boundaries, current usage, protected references, deadline, and sealed contents. Uncertainty preserves the resource.
7. Purge uses a same-parent tombstone and an operation journal, followed by guarded file removal. The tombstone is implementation state, not a recovery archive. `restore` refuses interrupted purges; inspect any remaining files manually. Some files may already be gone. A partial purge must not be reported as a complete success or fully recoverable.
8. Permanent deletion has no built-in restore. Recovery promises apply only before purge.

The implementation must distinguish retained/eligible bytes from actually removed bytes. Moving or marking a directory does not reclaim disk space. Hardlinks, open files, and filesystem allocation can make physical free-space changes differ from logical file sizes; reports should not imply a whole-disk free-space measurement when they report removed file bytes.

## Deliberate limits

This manager does not infer full document/build dependency graphs, monitor arbitrary filesystem writes, enforce a hard total-space quota, deduplicate shared installations, or clean Codex's native state. It does not guess the value of unregistered historical files. Missing delivery/ownership records, incomplete producer tracking, or unexplained changes require preservation and later review.

For interrupted tasks, the existence of a record is not proof of successful delivery. Only resources that already meet the ordinary retirement and cleanup contract can expire automatically. V1 has no chat-only delivery receipt: do not create a dummy file to make such a task eligible.
