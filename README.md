# manage-task-files · 任务过程文件管理

A Codex skill for managing newly generated task intermediates, protecting deliverables, and cleaning explicitly retired scratch files after a seven-day recovery period. Permanent cleanup currently supports Windows only.

让 Agent 在生成文件时就记录它们的用途，在完成任务后明确哪些过程文件可以回收。确认可回收的文件原地保留至少 7 天，再由 Windows 计划任务检查并删除。

这套工具管理的是**磁盘文件占用**。它由 Agent 使用规范、Python 管理程序和 Windows 定时执行器组成，覆盖新任务中的受管过程文件。

## 什么会清理，什么会保留

| 文件类别 | 默认处理 |
| --- | --- |
| 用户原始文件、已有目录、历史文件 | 不自动接管，不按文件名或年龄猜测用途 |
| 最终成果及其明确登记的依赖 | 保护；依赖的受管资源会被固定保留 |
| `working`：未完成成果、必要的编辑源文件、独有研究或数据 | 保留，不自动到期 |
| `scratch`：可重新生成且已不再需要的预览、转换结果、临时批次 | 明确退役后，进入 7 天恢复期 |
| Codex 会话、插件、共享运行环境、Git worktree | 不由本工具清理 |

即使启用了一个根目录，也只有管理程序**新建并登记**的资源目录可以进入清理流程。目录下原有的文件不会因此成为删除目标。

## 七天从什么时候开始

```text
开始任务 → 预留新目录 → 生成文件 → 记录文件快照
        → 登记真实成果及依赖 → 标记任务空闲
        → 明确退役 scratch → 原地保留至少 7 天 → 到期复核 → 删除
```

**七天从成功执行 `retire` 开始计算**，不是文件创建时间、最后修改时间，也不是聊天结束时间。退役不会移动文件，也不会立即释放磁盘空间。

到期后，第一次成功执行的清理仍会检查：根目录是否启用、任务是否空闲、使用锁是否释放、成果是否仍有效、资源是否被固定保留，以及内容和文件身份是否与快照一致。检查失败则保留并报告原因。

删除开始前可取消退役。永久删除后，本工具没有备份可供恢复；删除中断可能只剩部分文件，需人工检查，不能保证完整恢复。

## 安装与启用

需要 Windows、Git 和一个现有的可信 Python 3 解释器。核心程序只使用 Python 标准库；不需要安装 Python 第三方包。

下面的步骤分别完成安装、范围登记、Agent 接入和定时执行。**仅克隆仓库不会开始自动删除**；自动流程还需要已登记的范围、Agent 实际使用本技能、`auto` 模式、成功退役的资源以及已安装的定时任务。

### 1. 安装技能

在 PowerShell 中运行。目标目录应不存在；如果已经安装，请按后文更新流程处理。

```powershell
$SkillDir = Join-Path $env:USERPROFILE '.codex\skills\manage-task-files'
if (Test-Path -LiteralPath $SkillDir) { throw '目标目录已存在，请先检查现有安装。' }
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $SkillDir) | Out-Null
git clone https://github.com/ikki528/manage-task-files.git $SkillDir
if ($LASTEXITCODE -ne 0) { throw '仓库克隆失败。' }

$PythonExe = Read-Host '输入现有 python.exe 的绝对路径（不加引号）'
if (-not [IO.Path]::IsPathRooted($PythonExe) -or -not (Test-Path -LiteralPath $PythonExe -PathType Leaf)) {
    throw '请指定一个现有 Python 解释器的绝对路径。'
}
& $PythonExe --version
if ($LASTEXITCODE -ne 0) { throw 'Python 无法运行。' }

$CoreScript = Join-Path $SkillDir 'scripts\task_files.py'
$SchedulerScript = Join-Path $SkillDir 'scripts\windows_integration.ps1'
$StateDir = Join-Path $env:USERPROFILE '.codex\task-files-state'
```

可以使用 Codex 提供的 Python，但应填写它实际存在的解释器路径。定时任务会记录这个路径；解释器移动或移除后，需要重新配置。

### 2. 登记范围，先检查模式

继续在同一个 PowerShell 窗口运行，选择实际用于任务的现有目录。不要把整个磁盘或用户目录作为默认范围。

```powershell
$TaskRoot = Read-Host '输入允许管理新任务过程文件的根目录绝对路径'
& $PythonExe $CoreScript --state-dir $StateDir init --root $TaskRoot --mode audit
& $PythonExe $CoreScript --state-dir $StateDir status
& $PythonExe $CoreScript --state-dir $StateDir sweep --dry-run
```

| 模式 | 行为 |
| --- | --- |
| `audit` | 查看状态和候选，不允许新退役，不永久删除 |
| `recycle` | 允许退役，原地保留，不永久删除 |
| `auto` | 允许退役，并在至少 7 天后复核删除 |

`recycle` 是本工具的保留模式，不是 Windows 回收站。模式作用于同一个状态库中的受管任务；启用或停用根目录可单独控制范围。

### 3. 让 Agent 在工作时使用技能

可以在任务中直接说：

> 使用 $manage-task-files 管理这次任务新生成的过程文件，保留成果和必要源文件。

如果希望在登记范围内持续使用，将下面的规则合并到自己的全局或项目 `AGENTS.md`，保留其中原有内容：

```text
对已启用根目录下会生成过程文件的任务，使用 $manage-task-files。
创建过程文件前预留受管目录，区分 scratch 与 working；记录真实成果和依赖。
在生产进程停止、任务标记空闲后，只退役已确认可丢弃的 scratch。
已登记范围及其既定清理策略适用于后续日常管理；不要擅自扩大根目录或删除范围。
原始文件、成果、未完成工作及用途不明的文件应保留。
```

技能允许自动发现，但它不是文件系统监控器。只有 Agent 按流程登记和处理的资源才能进入自动回收。完整操作约定见 [SKILL.md](SKILL.md)。

### 4. 启用 Windows 定时执行

确认登记范围与模式后，可先运行测试，再切换到自动模式。测试使用独立临时目录。

```powershell
& $PythonExe -m unittest discover -s (Join-Path $SkillDir 'tests') -v
if ($LASTEXITCODE -ne 0) { throw '测试未通过，请先处理失败。' }

& $PythonExe $CoreScript --state-dir $StateDir set-mode --mode auto
& $PythonExe $CoreScript --state-dir $StateDir sweep --dry-run

$SchedulerArgs = @(
    '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
    '-File', $SchedulerScript,
    '-StateDir', $StateDir, '-SkillPath', $SkillDir, '-PythonPath', $PythonExe
)
powershell.exe @SchedulerArgs -Action Plan
powershell.exe @SchedulerArgs -Action Install -DailyAt '12:00'
powershell.exe @SchedulerArgs -Action Status
```

`Plan` 只预览；`Install` 仅配置定时任务，不负责克隆技能或初始化状态。这里的执行策略只影响该 PowerShell 进程，不修改机器的长期策略。

默认每天本地时间 12:00，以及当前用户登录 5 分钟后执行。任务由 Windows 直接运行脚本，**Codex 关闭时也能执行，不消耗模型 Token**。要求电脑开机且该用户已登录；退出登录、关机或休眠时不会执行，也不会主动唤醒电脑。错过的运行会在后续符合条件的启动或登录时补跑，而不是保证在到期那一刻删除。

## 日常查看、保留与恢复

沿用上文 PowerShell 变量；`RESOURCE_ID` 和 `TASK_ID` 替换为 `status` 返回的真实 ID。

```powershell
& $PythonExe $CoreScript --state-dir $StateDir status
& $PythonExe $CoreScript --state-dir $StateDir sweep --dry-run
& $PythonExe $CoreScript --state-dir $StateDir keep --resource RESOURCE_ID
& $PythonExe $CoreScript --state-dir $StateDir restore --resource RESOURCE_ID
& $PythonExe $CoreScript --state-dir $StateDir resume --task TASK_ID
```

- `keep`：取消退役并固定保留资源。
- `restore`：删除开始前取消退役，文件仍在原位置；清除旧快照，再次退役前需要重新评估和记录快照。
- `resume`：恢复整个任务，取消其中所有待执行的退役并清除快照，已有保留标记继续生效。
- `sweep --dry-run`：只预览；`sweep` 才按当前模式执行。

暂停定时执行或停止某个根目录的管理：

```powershell
powershell.exe @SchedulerArgs -Action Disable
& $PythonExe $CoreScript --state-dir $StateDir disable --root $TaskRoot
```

对应的 `Enable` / `enable` 可恢复。`Uninstall` 移除定时集成，保留状态和任务文件；已开始的清理可能继续完成。

## 文件存在哪里

| 内容 | 默认位置 |
| --- | --- |
| 技能代码 | `%USERPROFILE%\.codex\skills\manage-task-files` |
| 状态、运行记录和定时启动脚本 | `%USERPROFILE%\.codex\task-files-state` |
| 新建的受管过程文件 | 工作目录下的 `work\managed-task-files\任务ID\资源目录` |
| 最终成果 | 原任务约定的位置，登记不会移动成果 |

默认状态目录有意使用用户目录下的共享物理路径，以避免 MSIX 打包应用对 `LocalAppData` 的虚拟化导致 Codex 与外部计划任务读取不同位置。自定义路径时，让 Agent 和定时任务使用同一个 `--state-dir` / `-StateDir`。

定时执行的最近结果保存在 `last-scheduled-run.json`。状态使用原子写入和锁；清理前会核对边界、所有权及内容，删除过程中使用 Windows 文件句柄保护。不要手改登记信息、所有权标记或时间戳来绕过检查。

## 更新与边界

更新前检查并保留本地修改，使用 `git -C $SkillDir pull --ff-only` 获取更新，重新运行测试，再执行 `powershell.exe @SchedulerArgs -Action Install`。定时启动器固定了核心脚本的 SHA256；更新核心后必须重新安装定时集成，才能接受新版本。

当前版本的边界：

- 仅 Windows 支持永久删除；不提供跨平台自动清理保证。
- 不扫描全盘、不接管历史文件、不清理所有第三方缓存，也不实施磁盘配额。
- `working` 和未完成任务不会因时间过去而自动删除。
- 退役必须有真实、已登记且仍有效的文件成果。只有聊天回答的任务暂不支持这一步；不要生成虚假收据绕过条件。
- 不自动推断完整依赖关系；Agent 需通过 `publish --requires` 登记受管依赖，并保留其它必要文件。
- 后台生产进程应通过 `run --task ID -- ...` 持有使用锁。未接入的进程无法被全面追踪，使用情况不明时应保留。
- 这不是备份系统。已永久删除的内容不能通过 `restore` 找回。

详细说明：[Agent 使用流程](SKILL.md) · [集成与故障处理](references/integration.md) · [记录格式与生命周期](references/record-format.md)
