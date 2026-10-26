# Integration

Read this reference only for setup, scheduler/hook administration, or integration diagnosis. Normal task work uses the entrypoint workflow.

## Enrollment and operating mode

The main program is `scripts/task_files.py`. Its `--state-dir PATH` global option precedes the subcommand. Default Windows state is `%USERPROFILE%\.codex\task-files-state`. LocalAppData can be virtualized inside the packaged Codex app, making it invisible to the external Windows scheduler; use a shared physical path for state and launcher. V1 permanent deletion is implemented only on Windows; do not promise equivalent cleanup on other platforms.

- `init --root ABSOLUTE_PATH --mode audit|recycle|auto` initializes or enrolls an explicitly chosen root.
- `enable --root ABSOLUTE_PATH` / `disable --root ABSOLUTE_PATH` controls future cleanup for an enrolled root.
- `set-mode --mode audit|recycle|auto` changes the policy. Do not change mode merely to bypass a blocked cleanup.

`audit` reports candidates and does not permit new retirement. `recycle` retains retired resources for recovery without permanent removal. `auto` permits permanent deletion only after the seven-day minimum and every check passes. Allowed retirement is in place; there is no automatic copy to the Windows Recycle Bin.

Enroll the narrowest useful root. Enrollment does not classify old files or turn its entire tree into a deletion target. Only manager-created task/resource directories become managed candidates. Existing files, supplied inputs, and native Codex state remain outside deletion scope.

Initial deployment may enroll the user's Documents\Codex folder to cover future standalone chats. Other project roots require deliberate enrollment. Do not infer approval to enroll the entire user profile or a drive.

## Runtime and scheduler

The Windows helper is `scripts/windows_integration.ps1`:

```text
-Action Plan|Install|Status|Run|Disable|Enable|Uninstall
[-StateDir ABSOLUTE_PATH]
[-SkillPath INSTALLED_SKILL_DIRECTORY]
[-PythonPath ABSOLUTE_PYTHON_EXE]
[-TaskName "Codex Task Files Cleanup"]
[-DailyAt "12:00"]
```

Call the trusted local helper using `powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File HELPER_PATH` followed by separate parameter values. The execution-policy override applies only to that process; do not change the machine's persistent policy. `Plan` is read-only. `Install` installs scheduler integration only: install the skill files and initialize/enroll core state first. Default task folder is `\CodexTaskFiles\`; the default skill path is `~/.codex/skills/manage-task-files`.

The schedule is daily plus current-user logon delayed five minutes, with start-when-available enabled. It runs without Codex, but under the current user's interactive logon: it does not run while that user is signed out. A missed execution is handled on a later eligible run or logon. Waking the computer is disabled. The machine must be on for execution.

The stable launcher `run-cleanup.ps1` and `scheduler.json` live in state storage. Installation validates the trusted absolute Python path and the installed absolute script path, and pins the core script's SHA256. After updating the core script, rerun `-Action Install` to refresh the pinned hash. The launcher avoids concurrent copies and writes its latest bounded result to `last-scheduled-run.json`. A missing interpreter or changed core must fail; do not silently download or choose another runtime. Installation should be idempotent and preserve unrelated scheduled tasks. Disabling/uninstalling scheduling preserves state and task files; an already-running sweep may still finish.

Before enabling permanent cleanup:

1. Validate the skill and run the isolated lifecycle tests.
2. Run a sweep preview against the intended state.
3. Install the schedule using the user's existing authorization and chosen policy.
4. Inspect the registered action, account, triggers, and enabled state; trigger one manual run and check its result.

Do not use Codex's scheduled-agent feature as a substitute when cleanup must work with Codex closed. This Windows task calls the script directly and consumes no model tokens.

## Codex entrypoint

A small instruction in the active global/project guidance can route enrolled file-producing tasks to `$manage-task-files`. Keep other task instructions intact. The helper's `hook` command accepts a Codex event JSON on stdin and is for lightweight status/reminders; it must not purge resources from a lifecycle callback.

Hooks are optional. Verify the installed Codex version and supported local execution mode before wiring events. Merge only owned hook entries, preserve unrelated configuration, and record how to remove the integration. A hook or a final response is not proof that a task's work is disposable. Do not mark an interrupted task successfully delivered.

Coverage requires producer integration and registration. This skill cannot observe every third-party cache, browser download, or native program write. Prefer the `run` command for producers operating on managed resources, and retain material when use cannot be established reliably.

## Diagnosis and stopping

- Run core `status` and Windows helper `Status` separately: enabled data policy and installed scheduling are different facts.
- If a sweep reports a changed resource, inspect it. Use `keep` or recover it; do not automatically reseal and retire it again.
- If the manager is locked, wait for the active producer/operation to finish. Do not remove a live lock or terminate an unrelated process.
- If state is damaged, disable scheduling and preserve data for repair. The presence of old-looking directories is not a replacement for valid records.
- A previously registered path that disappears or changes ownership must be reported, not recreated and purged by its old path alone.
- Restore before purge starts. An interrupted purge refuses `restore`; inspect remaining files manually because complete recovery is not guaranteed. Once permanent deletion succeeds, the manager has no backup copy to restore.
