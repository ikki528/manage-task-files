---
name: manage-task-files
description: Manage newly generated local task intermediates in enrolled workspaces, record deliverables, and perform delivery cleanup; also inspect, keep, restore, or clean registered task files. Use alongside the task's document, analysis, browser, or coding workflow. Ordinary conversation and unregistered historical folders do not need this workflow.
---

# Manage task files

Use the bundled `scripts/task_files.py` for resource state and file operations. Run it with a trusted Python 3 interpreter; in Codex desktop prefer the bundled runtime returned by the workspace dependency tool. Resolve the script from this skill's installed directory. Pass paths as separate arguments, never through a constructed shell command.

Default state is `%USERPROFILE%\.codex\task-files-state` on Windows. This avoids packaged-app virtualization of LocalAppData so Windows scheduling sees the same files. Use `--state-dir PATH` **before** the subcommand for an explicit state location. Commands return JSON; use returned task/resource IDs and paths instead of inventing them. Start with `status` or `--help` if the current state or command contract is unclear.

## Scope and classification

- Automatic cleanup covers only newly created directories reserved by this manager inside enrolled workspaces. Do not adopt an existing folder or recursively clean a general `work`, `outputs`, Desktop, Downloads, or repository directory.
- Reserve `working` for unfinished deliverables, unique research, editable sources, and scripts/data needed to continue. This kind never expires automatically. Reserve `scratch` only for disposable intermediates whose necessary sources are retained elsewhere.
- Protect original inputs, actual deliverables, required assets, project source, and named versions. Preserve requested output locations and repository conventions. Do not rearrange them just to fit this skill.
- Codex state, sessions, plugins, shared runtimes, and managed Git worktrees remain outside this cleaner's scope. Use their native lifecycle tools when appropriate.
- Enrollment and the configured cleanup policy provide standing scope for routine managed cleanup. Skill selection alone does not authorize enrolling new roots, installing scheduling, or expanding deletion policy.

## Use during a task

1. Check enrollment with `status`. For a file-producing task in an enabled workspace, call `start --workspace PATH --label LABEL` (optionally `--session ID`). If continuing an existing managed task, call `resume --task ID` instead.
2. Before producing intermediates, call `reserve --task ID --name NAME --kind scratch|working`. Write only the relevant batch into the returned empty directory. A batch may contain many preview images; the script inventories them without sending each filename to the model.
3. Prefer `run --task ID -- EXECUTABLE ARG...` to run producers that touch managed directories. This holds the task's usage lease for the producer lifetime. Otherwise, do not retire a resource until every producer has finished. Background work must not be treated as finished just because a tool call returned.
4. Complete the requested task using its appropriate skills. Reuse compatible installed tools before downloading another copy. Keep separate versions only when they are useful for comparison, compatibility, recovery, or the user's request.
5. Once a batch stops changing, call `seal --resource ID`. Seal records file identity/content information; it is not approval to delete. Do not reseal an unexplained change merely to make a cleanup check pass.
6. Save and verify the actual file deliverable, then record it with `publish --task ID --path ABSOLUTE_PATH`. Add repeatable `--requires RESOURCE_ID` for every managed batch it needs; these dependencies are pinned. Published paths may be outside managed directories, but must be regular files with one hard link and no reparse-point parents. Publishing records protected references; it does not move files or send them to a service. Keep any other required assets explicitly. The manager cannot infer document/build dependencies.
7. Call `finish --task ID` after producers have stopped. This marks the task idle; it does not delete files or retire resources.
8. Call `retire --resource ID --reason TEXT` only for sealed, unchanged, unpinned `scratch` batches no longer needed, after the task is idle and at least one verified file delivery has been recorded. Keep necessary or uncertain materials with `keep --resource ID`. Mention meaningful cleanup or uncertainty briefly, while keeping the requested task result central.

V1 requires an actual file deliverable to retire scratch batches. A chat-only answer does not satisfy that condition; preserve those batches rather than creating a dummy receipt file to bypass it. A final assistant answer alone is not evidence that all task files are disposable.

## Inspect, retain, and restore

- `status`: inspect enrollment, mode, tasks, resources, and reported space.
- `sweep --dry-run`: preview eligible deletion and reasons for skips.
- `keep --resource ID`: cancel retirement, reset the resource to active, and pin it against cleanup.
- `restore --resource ID`: cancel retirement before purge; content remains at its original managed location. This resets the resource to active and clears its seal. Reassess and reseal before any future retirement.
- `resume --task ID`: reopen a task before reusing its managed resources; cancels all its outstanding retirements and clears seals. Existing pins remain.
- `sweep`: apply the configured policy. Use the script, not ad hoc file removal commands.

Retirement provides **one seven-day recovery period**, counted from explicit retirement using UTC timestamps. Recovery is in place: retirement does not move files or free disk space. The scheduled sweep deletes eligible content at its first successful run after expiry. The task must be idle, its root enabled, and its sealed contents unchanged. Changed, occupied, disabled, unsealed, or uncertain resources must be preserved, with a reason recorded.

## Setup and failure handling

Read [integration.md](references/integration.md) only to enroll a workspace, enable/disable scheduling or hooks, change operating mode, or diagnose integration failures. Read [record-format.md](references/record-format.md) when inspecting state or maintaining the implementation.

If a command fails, inspect its error and preserve affected files. Do not repair state by inventing ownership records, deleting markers, shortening retention, or editing timestamps. A missing runtime or damaged registry stops cleanup; it does not justify scanning arbitrary directories. Interrupted tasks with incomplete registration need a later review, not an age-based guess. An interrupted purge may leave only partial remnants: `restore` refuses that state, manual inspection is required, and complete recovery is not promised. Permanent deletion cannot be undone by this manager.

Do not promise full filesystem monitoring, a hard disk quota, automatic dependency discovery, or deletion while the computer is off. Report actual deletion separately from retained or eligible bytes.
