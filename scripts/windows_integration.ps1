[CmdletBinding()]
param(
    [ValidateSet('Plan', 'Install', 'Status', 'Run', 'Disable', 'Enable', 'Uninstall')]
    [string]$Action = 'Status',
    # LocalAppData can be virtualized by the packaged Codex app, making a
    # scheduled process see a different directory. Use the shared user path.
    [string]$StateDir = (Join-Path $env:USERPROFILE '.codex\task-files-state'),
    [string]$SkillPath = (Join-Path $env:USERPROFILE '.codex\skills\manage-task-files'),
    [string]$PythonPath,
    [ValidatePattern('^[^\\/:*?"<>|\r\n]{1,100}$')]
    [string]$TaskName = 'Codex Task Files Cleanup',
    [ValidatePattern('^(?:[01][0-9]|2[0-3]):[0-5][0-9]$')]
    [string]$DailyAt = '12:00'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$OwnerTag = 'manage-task-files/windows-v1'
$TaskFolder = '\CodexTaskFiles'

function Assert-PlainPath([string]$Path, [switch]$FileRequired, [switch]$DirectoryRequired) {
    if ([string]::IsNullOrWhiteSpace($Path) -or -not [IO.Path]::IsPathRooted($Path)) {
        throw "An absolute local path is required: $Path"
    }
    $full = [IO.Path]::GetFullPath($Path).TrimEnd('\', '/')
    if ($full.StartsWith('\\') -or $full -match '^[A-Za-z]:$') {
        throw "A local path below a drive root is required: $Path"
    }
    $cursor = $full
    while ($cursor) {
        if (Test-Path -LiteralPath $cursor) {
            $item = Get-Item -LiteralPath $cursor -Force
            if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw "Reparse points are not supported in integration paths: $cursor"
            }
        }
        $parent = [IO.Path]::GetDirectoryName($cursor)
        if ($parent -eq $cursor) { break }
        $cursor = $parent
    }
    if ($FileRequired -and -not (Test-Path -LiteralPath $full -PathType Leaf)) {
        throw "Required file is missing: $full"
    }
    if ($DirectoryRequired -and -not (Test-Path -LiteralPath $full -PathType Container)) {
        throw "Required directory is missing: $full"
    }
    return $full
}

function Write-AtomicText([string]$Path, [string]$Text) {
    $target = Assert-PlainPath $Path
    $temp = Join-Path ([IO.Path]::GetDirectoryName($target)) ('.scheduler-' + [guid]::NewGuid().ToString('N') + '.tmp')
    try {
        [IO.File]::WriteAllText($temp, $Text, [Text.UTF8Encoding]::new($false))
        # Atomic same-directory commit; do not delete the old configuration first.
        if (Test-Path -LiteralPath $target) { [IO.File]::Replace($temp, $target, [System.Management.Automation.Language.NullString]::Value) }
        else { [IO.File]::Move($temp, $target) }
    } finally {
        if (Test-Path -LiteralPath $temp) { Remove-Item -LiteralPath $temp -Force }
    }
}

function Read-SchedulerConfig {
    $configPath = Join-Path $StateDir 'scheduler.json'
    if (-not (Test-Path -LiteralPath $configPath)) { return $null }
    $null = Assert-PlainPath $configPath -FileRequired
    $config = Get-Content -LiteralPath $configPath -Raw | ConvertFrom-Json
    if ($config.owner -ne $OwnerTag -or $config.state_dir -ine $StateDir) {
        throw 'Existing scheduler configuration has a different owner or state directory.'
    }
    if ($config.task_name -cne $TaskName -or $config.task_folder -cne $TaskFolder) {
        throw 'Existing scheduler configuration belongs to another task name or folder. Supply the original TaskName.'
    }
    [guid]$parsedId = [guid]::Empty
    if (-not [guid]::TryParse([string]$config.installation_id, [ref]$parsedId)) {
        throw 'Invalid scheduler installation identifier.'
    }
    return $config
}

function Connect-Scheduler {
    $service = New-Object -ComObject 'Schedule.Service'
    $service.Connect()
    return $service
}

function Get-SchedulerFolder($Service, [switch]$Create) {
    try { return $Service.GetFolder($TaskFolder) }
    catch {
        $hr = $_.Exception.GetBaseException().HResult
        if ($hr -notin @(-2147024894, -2147024893)) { throw }
        if ($Create) { return $Service.GetFolder('\').CreateFolder($TaskFolder.TrimStart('\'), $null) }
        return $null
    }
}

function Get-ExistingTask($Folder) {
    if ($null -eq $Folder) { return $null }
    try { return $Folder.GetTask($TaskName) }
    catch {
        $hr = $_.Exception.GetBaseException().HResult
        if ($hr -notin @(-2147024894, -2147024893)) { throw }
        return $null
    }
}

function Get-OwnerDescription($Config) {
    return 'Managed by ' + $OwnerTag + '; installation=' + $Config.installation_id + '; state=' + $Config.state_dir
}

function Assert-TaskOwner($Task, $Config) {
    if ($null -eq $Task) { return }
    if ($null -eq $Config -or $Task.Definition.RegistrationInfo.Description -cne (Get-OwnerDescription $Config)) {
        throw 'A scheduled task with this name exists but is not owned by this installation. It was not changed.'
    }
    if ($Task.Definition.Actions.Count -ne 1) { throw 'The registered task action was changed; refusing to modify it.' }
    $scheduledAction = $Task.Definition.Actions.Item(1)
    if ($scheduledAction.Path -ine $Config.powershell_path -or $scheduledAction.Arguments -cne $Config.arguments) {
        throw 'The registered task action does not match the owned installation.'
    }
}

function Quote-TaskArgument([string]$Value) {
    # Windows file names cannot contain quotes. No command interpolation or shell evaluation is used.
    if ($Value.Contains('"') -or $Value.Contains("`r") -or $Value.Contains("`n")) { throw 'Invalid task argument.' }
    return '"' + $Value + '"'
}

function Get-LauncherSource {
    return @'
# manage-task-files managed cleanup launcher v1
# This file is generated by windows_integration.ps1. It contains no user data or credentials.
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$outputLimit = 16000
$resultPath = Join-Path $PSScriptRoot 'last-scheduled-run.json'
$record = [ordered]@{ started_at = [DateTime]::UtcNow.ToString('o'); finished_at = $null; status = 'failed'; exit_code = 1; output = ''; error = $null }

function Assert-LauncherPath([string]$Path, [switch]$FileRequired) {
    if ([string]::IsNullOrWhiteSpace($Path) -or -not [IO.Path]::IsPathRooted($Path)) { throw 'A registered path is not absolute.' }
    $full = [IO.Path]::GetFullPath($Path).TrimEnd('\', '/')
    if ($full.StartsWith('\\') -or $full -match '^[A-Za-z]:$') { throw 'Invalid registered local path.' }
    $cursor = $full
    while ($cursor) {
        if (Test-Path -LiteralPath $cursor) {
            $item = Get-Item -LiteralPath $cursor -Force
            if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw "Registered path contains a reparse point: $cursor" }
        }
        $next = [IO.Path]::GetDirectoryName($cursor)
        if ($next -eq $cursor) { break }
        $cursor = $next
    }
    if ($FileRequired -and -not (Test-Path -LiteralPath $full -PathType Leaf)) { throw "Registered runtime or program is missing: $full" }
    return $full
}

try {
    $state = Assert-LauncherPath $PSScriptRoot
    $configPath = Assert-LauncherPath (Join-Path $state 'scheduler.json') -FileRequired
    $config = Get-Content -LiteralPath $configPath -Raw | ConvertFrom-Json
    if ($config.owner -ne 'manage-task-files/windows-v1' -or $config.state_dir -ine $state) { throw 'Scheduler configuration ownership does not match.' }
    if ($config.status -ne 'installed') { throw 'Scheduler integration is not enabled for execution.' }
    $python = Assert-LauncherPath $config.python_path -FileRequired
    $skill = Assert-LauncherPath $config.skill_path
    $script = Assert-LauncherPath $config.script_path -FileRequired
    if ($script -ine (Join-Path $skill 'scripts\task_files.py')) { throw 'Registered cleanup script is outside the installed skill.' }
    if ((Get-FileHash -LiteralPath $script -Algorithm SHA256).Hash -ine $config.script_sha256) { throw 'Cleanup script changed after installation. Re-run integration Install to validate and refresh the registration.' }
    $buffer = [Text.StringBuilder]::new()
    $oldPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        & $python $script --state-dir $state sweep 2>&1 | ForEach-Object {
            $line = [string]$_
            $available = $outputLimit - $buffer.Length
            if ($available -gt 0) {
                $null = $buffer.Append(($line + [Environment]::NewLine).Substring(0, [Math]::Min($available, $line.Length + [Environment]::NewLine.Length)))
            }
        }
        $record.exit_code = $LASTEXITCODE
    } finally { $ErrorActionPreference = $oldPreference }
    $record.output = $buffer.ToString()
    if ($null -eq $record.exit_code) { throw 'The cleanup process did not return an exit code.' }
    if ($record.exit_code -eq 0) { $record.status = 'completed' }
    else { $record.error = 'Cleanup exited with a nonzero status. Review the bounded output and task-files status.' }
} catch {
    $record.error = $_.Exception.Message
    $record.exit_code = 1
} finally {
    $record.finished_at = [DateTime]::UtcNow.ToString('o')
    try {
        $null = Assert-LauncherPath $resultPath
        $temp = Join-Path $PSScriptRoot ('.run-result-' + [guid]::NewGuid().ToString('N') + '.tmp')
        try {
            [IO.File]::WriteAllText($temp, ($record | ConvertTo-Json -Depth 5), [Text.UTF8Encoding]::new($false))
            if (Test-Path -LiteralPath $resultPath) { [IO.File]::Replace($temp, $resultPath, [System.Management.Automation.Language.NullString]::Value) }
            else { [IO.File]::Move($temp, $resultPath) }
        } finally {
            if (Test-Path -LiteralPath $temp) { Remove-Item -LiteralPath $temp -Force }
        }
    } catch {
        [Console]::Error.WriteLine('Could not save cleanup status: ' + $_.Exception.Message)
        $record.exit_code = 1
    }
}
if ($record.status -ne 'completed') { [Console]::Error.WriteLine([string]$record.error) }
exit [int]$record.exit_code
'@
}

try {
    if ($env:OS -ne 'Windows_NT') { throw 'This integration supports Windows only.' }
    $StateDir = Assert-PlainPath $StateDir
    $config = Read-SchedulerConfig
    $service = Connect-Scheduler
    $folder = Get-SchedulerFolder $service
    $existing = Get-ExistingTask $folder
    Assert-TaskOwner $existing $config

    if ($Action -eq 'Status') {
        $lastRun = $null
        $lastPath = Join-Path $StateDir 'last-scheduled-run.json'
        if (Test-Path -LiteralPath $lastPath) {
            $null = Assert-PlainPath $lastPath -FileRequired
            try { $lastRun = Get-Content -LiteralPath $lastPath -Raw | ConvertFrom-Json } catch { $lastRun = @{ error = 'Last-run record is unreadable.' } }
        }
        $status = [ordered]@{ installed = ($null -ne $existing); enabled = $false; task_name = $TaskName; task_folder = $TaskFolder; state_dir = $StateDir; requires_user_logged_in = $true; requires_codex_running = $false; last_run = $lastRun }
        if ($existing) {
            $status.enabled = [bool]$existing.Enabled
            $status['scheduler_last_result'] = $existing.LastTaskResult
            $status['scheduler_last_run_time'] = $existing.LastRunTime.ToString('o')
            $status['scheduler_next_run_time'] = $existing.NextRunTime.ToString('o')
        }
        $status | ConvertTo-Json -Depth 8
        exit 0
    }

    if ($Action -in @('Plan', 'Install')) {
        $SkillPath = Assert-PlainPath $SkillPath -DirectoryRequired
        if ($StateDir -ieq $SkillPath -or $StateDir.StartsWith($SkillPath + '\', [StringComparison]::OrdinalIgnoreCase)) { throw 'StateDir must be outside the installed skill directory.' }
        $scriptPath = Assert-PlainPath (Join-Path $SkillPath 'scripts\task_files.py') -FileRequired
        if (-not $PythonPath -and $config) { $PythonPath = $config.python_path }
        if (-not $PythonPath) { throw 'Supply an explicit validated PythonPath for initial installation.' }
        $PythonPath = Assert-PlainPath $PythonPath -FileRequired
        $shellPath = Assert-PlainPath (Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe') -FileRequired
        $launcherPath = Join-Path $StateDir 'run-cleanup.ps1'
        $arguments = '-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File ' + (Quote-TaskArgument $launcherPath)
        if (-not $config -and (Test-Path -LiteralPath $launcherPath)) { throw 'An unowned launcher already exists in StateDir; it was not overwritten.' }
        $plan = [ordered]@{ action = $Action; state_dir = $StateDir; skill_path = $SkillPath; python_path = $PythonPath; script_path = $scriptPath; task_path = $TaskFolder + '\' + $TaskName; daily_at_local = $DailyAt; logon_delay_minutes = 5; start_when_available = $true; wake_computer = $false; requires_user_logged_in = $true; requires_codex_running = $false; maximum_run_minutes = 5; script_sha256 = (Get-FileHash -LiteralPath $scriptPath -Algorithm SHA256).Hash; will_create_or_update = @('scheduler.json', 'run-cleanup.ps1', 'Owned Windows scheduled task'); will_preserve = @('Task records', 'Recovery files', 'All existing workspace files') }
        if ($Action -eq 'Plan') { $plan | ConvertTo-Json -Depth 5; exit 0 }

        $null = [IO.Directory]::CreateDirectory($StateDir)
        $null = Assert-PlainPath $StateDir -DirectoryRequired
        $installLockPath = Assert-PlainPath (Join-Path $StateDir 'scheduler.install.lock')
        $installLock = [IO.File]::Open($installLockPath, [IO.FileMode]::OpenOrCreate, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)
        try {
            # Re-check ownership under the installation lock.
            $config = Read-SchedulerConfig
            $folder = Get-SchedulerFolder $service -Create
            $existing = Get-ExistingTask $folder
            Assert-TaskOwner $existing $config
            $configPath = Join-Path $StateDir 'scheduler.json'
            $oldConfigText = if (Test-Path -LiteralPath $configPath) { Get-Content -LiteralPath $configPath -Raw } else { $null }
            $oldLauncherText = if (Test-Path -LiteralPath $launcherPath) { Get-Content -LiteralPath $launcherPath -Raw } else { $null }
            $oldXml = if ($existing) { $existing.Xml } else { $null }
            $installationId = if ($config) { $config.installation_id } else { [guid]::NewGuid().ToString() }
            $nextConfig = [ordered]@{ owner = $OwnerTag; installation_id = $installationId; state_dir = $StateDir; skill_path = $SkillPath; script_path = $scriptPath; script_sha256 = $plan.script_sha256; python_path = $PythonPath; powershell_path = $shellPath; arguments = $arguments; task_folder = $TaskFolder; task_name = $TaskName; daily_at = $DailyAt; status = 'installed'; installed_at = [DateTime]::UtcNow.ToString('o') }
            $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
            $definition = $service.NewTask(0)
            $definition.RegistrationInfo.Description = Get-OwnerDescription ([pscustomobject]$nextConfig)
            $definition.RegistrationInfo.Author = $identity.Name
            $definition.Principal.UserId = $identity.User.Value
            $definition.Principal.LogonType = 3 # TASK_LOGON_INTERACTIVE_TOKEN
            $definition.Principal.RunLevel = 0 # Least privilege
            $definition.Settings.Enabled = $false
            $definition.Settings.StartWhenAvailable = $true
            $definition.Settings.WakeToRun = $false
            $definition.Settings.DisallowStartIfOnBatteries = $false
            $definition.Settings.StopIfGoingOnBatteries = $false
            $definition.Settings.MultipleInstances = 2 # TASK_INSTANCES_IGNORE_NEW
            $definition.Settings.ExecutionTimeLimit = 'PT5M'
            $definition.Settings.AllowHardTerminate = $true
            $definition.Settings.RunOnlyIfIdle = $false
            $definition.Settings.Hidden = $false
            $daily = $definition.Triggers.Create(2)
            $start = [DateTime]::Today.Add([TimeSpan]::ParseExact($DailyAt, 'hh\:mm', [Globalization.CultureInfo]::InvariantCulture))
            if ($start -le [DateTime]::Now) { $start = $start.AddDays(1) }
            $daily.StartBoundary = $start.ToString('yyyy-MM-ddTHH:mm:ss')
            $daily.DaysInterval = 1
            $daily.Enabled = $true
            $logon = $definition.Triggers.Create(9)
            $logon.UserId = $identity.User.Value
            $logon.Delay = 'PT5M'
            $logon.Enabled = $true
            $exec = $definition.Actions.Create(0)
            $exec.Path = $shellPath
            $exec.Arguments = $arguments
            # All launcher/core paths are absolute. Leave the scheduler's startup
            # directory unset to avoid a separate process-launch dependency.
            $registered = $false
            try {
                Write-AtomicText $launcherPath (Get-LauncherSource)
                $registeredTask = $folder.RegisterTaskDefinition($TaskName, $definition, 6, $identity.User.Value, $null, 3, $null)
                $registered = $true
                Write-AtomicText $configPath ($nextConfig | ConvertTo-Json -Depth 6)
                $registeredTask.Enabled = $true
            } catch {
                $installError = $_.Exception.Message
                $rollbackErrors = @()
                try {
                    if ($registered) {
                        if ($oldXml) { $null = $folder.RegisterTask($TaskName, $oldXml, 6, $identity.User.Value, $null, 3, $null) }
                        else { $folder.DeleteTask($TaskName, 0) }
                    }
                } catch { $rollbackErrors += $_.Exception.Message }
                try {
                    if ($null -ne $oldConfigText) { Write-AtomicText $configPath $oldConfigText }
                    elseif (Test-Path -LiteralPath $configPath) { $null = Assert-PlainPath $configPath; Remove-Item -LiteralPath $configPath -Force }
                    if ($null -ne $oldLauncherText) { Write-AtomicText $launcherPath $oldLauncherText }
                    elseif (Test-Path -LiteralPath $launcherPath) { $null = Assert-PlainPath $launcherPath; Remove-Item -LiteralPath $launcherPath -Force }
                } catch { $rollbackErrors += $_.Exception.Message }
                throw ('Scheduled-task installation failed: ' + $installError + $(if ($rollbackErrors.Count) { ' Rollback also needs attention: ' + ($rollbackErrors -join '; ') }))
            }
            $plan['installed'] = $true
            $plan['enabled'] = $true
            $plan | ConvertTo-Json -Depth 5
        } finally { $installLock.Dispose() }
        exit 0
    }

    if ($null -eq $config) { throw 'Scheduler integration is not installed for this state directory.' }
    if ($Action -eq 'Run') {
        if ($config.status -ne 'installed') { throw 'Scheduler integration is disabled or uninstalled.' }
        $shell = Assert-PlainPath $config.powershell_path -FileRequired
        $launcher = Assert-PlainPath (Join-Path $StateDir 'run-cleanup.ps1') -FileRequired
        & $shell -NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File $launcher
        $runExit = $LASTEXITCODE
        $lastPath = Join-Path $StateDir 'last-scheduled-run.json'
        if (Test-Path -LiteralPath $lastPath) { Get-Content -LiteralPath $lastPath -Raw }
        exit $runExit
    }
    if ($null -eq $existing -and $Action -ne 'Uninstall') { throw 'Owned scheduled task is missing. Run Install again.' }
    if ($Action -eq 'Disable') { $existing.Enabled = $false; $config.status = 'disabled' }
    if ($Action -eq 'Enable') { $config.status = 'installed'; Write-AtomicText (Join-Path $StateDir 'scheduler.json') ($config | ConvertTo-Json -Depth 6); $existing.Enabled = $true }
    if ($Action -eq 'Uninstall') {
        if ($existing) { $existing.Enabled = $false; $folder.DeleteTask($TaskName, 0) }
        $config.status = 'uninstalled'
    }
    Write-AtomicText (Join-Path $StateDir 'scheduler.json') ($config | ConvertTo-Json -Depth 6)
    [ordered]@{ action = $Action; task_name = $TaskName; status = $config.status; state_preserved = $true } | ConvertTo-Json
    exit 0
} catch {
    [ordered]@{ action = $Action; status = 'error'; error = $_.Exception.Message } | ConvertTo-Json -Depth 4
    exit 1
}
