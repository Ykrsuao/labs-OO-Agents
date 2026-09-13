# 原生三平台支持说明

## 产品要求

本 fork 的产品必须原生支持 Windows、Linux 和 macOS，三者都是正式交付平台。

WSL 可以用于在 Windows 机器上进行 Linux 开发和回归测试，但不能替代原生 Windows 产品的交付。

这是产品目标和分阶段实现的约定，不代表现有 NOOA 的全部功能已经可以在三个平台上使用。

## 原生基础能力

已配置 `native-core` CI 测试矩阵，覆盖 Windows、Linux、macOS，以及 Python 3.12 和 3.13。其中基础测试步骤验证以下不依赖真实模型供应商或 shell 的能力；另有原生命令执行与编码流程测试步骤。

- 导入公开包，以及查看已安装命令的帮助信息。
- 执行 CodeAct，并在同一次调用的多个代码单元之间保留局部变量。
- 通过 Predict 返回结构化结果。
- 读取、写入和替换 UTF-8 文本文件，包括带中文等 Unicode 字符的路径。
- 使用 SQLite 持久化事件和快照。
- 连续保存的快照具有相同时间戳时，仍能正确选出最新快照。
- 保证会话只能由一个进程持有，报告持有者的 PID，在进程退出后释放锁，并拒绝删除正在使用的会话。
- 检测平台能力，并明确拒绝当前平台无法使用的沙箱进程后端。

会话锁在 Windows 上使用 `msvcrt.locking`，在 POSIX 平台上使用 `fcntl.flock`。两者都由操作系统管理，而不是简单地通过“锁文件是否存在”判断占用状态。释放锁后仍保留锁文件，避免竞争进程锁住不同文件对象的竞态问题。

在 Windows 上，锁定的字节位于 PID 元数据区域之后，因此其他竞争进程仍然可以读取当前持有者的 PID。

文件工具显式使用 UTF-8，不依赖用户系统的默认文本编码。现有文本模式的换行处理行为保持不变；这不代表新增了二进制文件编辑能力，也不保证所有编码的文件都能按原始字节无损编辑。

可选的 `SIGUSR2` 调试钩子只在平台提供该信号时安装。Windows 仍可通过代码主动导出调试信息。

## 原生命令执行

`ShellTools` 保持相同的 `run()`、`run_stream()`、`read()`、`replace()` 和 `write_file()` 接口，但命令语言按操作系统选择：

- Windows 优先使用 PATH 中的 PowerShell 7（`pwsh`），未找到时使用 Windows PowerShell 5.1（`powershell.exe`）。不依赖 WSL 或 Git Bash。
- Linux/macOS 使用现有的 `/bin/bash`。
- 接口统一不等于命令语法相同。`shell_name` 和 `describe_environment()` 会说明当前环境，CodingAgent 也会将环境说明放入模型上下文。

Windows 后端使用独立 PowerShell 进程和持久 runspace。正常调用之间保留工作目录、环境变量、PowerShell 变量和函数。控制消息通过独立的本机连接传输，不从普通命令输出中寻找结束标记。

标准输出、错误输出可以流式读取。原生程序的退出码和显式 `exit N` 会正确返回；程序自己退出为 124 与真正超时通过 `timed_out` 区分。交互式输入提示会明确失败，应通过 `stdin=` 提供输入。

框架内部按参数列表调用外部程序时，通过 Python 标准库传递原生 argv，避免 PowerShell 5.1 丢失空参数、引号或合并带反斜杠的参数。直接传给 `run(command)` 的字符串仍然遵守当前 shell 的语法，不会自动把 bash 命令转换为 PowerShell。

`run()` 对每一路返回文本保留最多 30,000 个字符，超出后附加截断提示。需要持续消费大输出时使用 `run_stream()`。PowerShell 输出分块不会拆开 UTF-16 代理对，避免部分 Unicode 字符在分块边界变成乱码。

`stdin=` 按 UTF-8 原样传递文本，包括末尾没有换行的输入。Windows 使用子 shell 处理这类调用，因此继承工作目录和环境，但不继承主 runspace 的普通变量、函数；子 shell 对目录和环境的修改也不会回写主会话。

Windows 使用 Job Object 管理 PowerShell 及其后代进程。超时、取消或提前关闭输出流时，会回收该会话的进程树，包括此前留在该会话中的后台进程。下一次调用会重建会话，并恢复最后一次已完成命令的工作目录和环境变量：

- PowerShell 普通变量、函数和后台任务需要重新创建，结果中会给出重置提示。
- 已经发生的文件修改和外部副作用不会回滚，取消操作不是事务撤销。
- Job Object 在这里用于进程清理，不是文件系统或网络安全沙箱。

RepoTools 不再依赖 `head`、`/dev/null` 和 `command -v`，并能识别带盘符的 Windows 搜索结果。安装 ripgrep 时使用它加速搜索，未安装时保留 Python 回退路径。后台 `monitor()` 也按平台选择执行方式，并在关闭时清理其所属进程。

## 尚未完成的工作

| 领域 | 当前实现 | 产品发布前需要完成 |
|---|---|---|
| Shell 命令执行 | 已实现 Windows PowerShell 与 Linux/macOS bash 两条执行路径，覆盖流式输出、stdin、状态及取消。 | 完成真实 macOS CI 验证，并继续验证产品实际使用的外部程序和安装环境。非 UTF-8 旧程序可能仍需要显式编码适配。 |
| 后台命令与输出监听 | Windows 使用 PowerShell/Job Object，POSIX 使用独立进程组；均有流式读取和取消路径。 | 继续扩展长时间运行、复杂子进程结构以及宿主断开重连的测试。 |
| 受限代码执行 | 当前工作进程依赖 `fork`，文件系统和网络的强隔离依赖 Linux 能力。 | 为各平台选择并验证适用的隔离机制。不能将缺失的保护宣称为已生效，也不能在未重新设计 Python 对象跨进程传递机制时，直接把 `fork` 换成 `spawn`。 |
| 安装与打包 | 已构建核心 wheel 和源码分发包，确认包含 PowerShell 辅助脚本；已在独立 Python 环境中安装 wheel 并运行 Windows shell 检查。 | 仍需在各平台的干净系统中验证完整产品的安装、升级、应用数据目录，以及随产品提供的 shell 或运行时依赖。Python wheel 验证不等于桌面产品安装包验收。 |
| 完整编码流程 | 已增加临时 Git 项目中的文件读取、代码修改、符号查询、单元测试和 Git diff 闭环，使用脚本化模型响应验证执行流程。 | 通过 macOS 实际 runner，并补齐真实模型、最终产品界面和安装包层面的验收。 |

如果沙箱要求的进程启动方式在当前平台不可用，即使请求了可选保护降级，也会抛出 `SandboxUnavailable`。平台能力检测不应仅因缺少 `resource` 模块就导致程序崩溃。

## 开发与验证方式

使用 `uv` 管理依赖。Windows 从源码构建时需要启用 UTF-8 模式，因为动态版本号构建依赖会使用解释器的默认编码读取配置：

```powershell
$env:PYTHONUTF8 = "1"
$env:LITELLM_LOCAL_MODEL_COST_MAP = "True"
uv sync --frozen --group dev
uv run --frozen --no-sync pytest tests/platform/test_native_basics.py -q
uv run --frozen --no-sync pytest tests/platform/test_shell_workflow.py tests/platform/test_powershell_session.py -q
```

在 Linux 和 macOS 上，先设置环境变量 `PYTHONUTF8=1` 和 `LITELLM_LOCAL_MODEL_COST_MAP=True`，再运行相同的 `uv` 命令。

除原生基础测试矩阵外，仍需保留完整的 Linux 回归测试和 Linux 沙箱测试。较小的基础测试矩阵通过，不代表三平台已经实现完整功能对等，也不能替代真实 macOS 测试环境中的验证。

原生 Windows 和 WSL 共用同一份代码检出目录时，应分别使用独立的虚拟环境和 `PYTHONPYCACHEPREFIX` 缓存目录，避免复用包含另一操作系统源码路径的 Python 字节码。

## 首批适配验证结果

第一批跨平台适配补丁的本地验证结果如下：

| 环境 | 结果 |
|---|---|
| 原生 Windows，Python 3.12.13 | 原生基础能力及存储相关检查共 166 项通过、1 项跳过。跳过的是 Linux 专属的 virtiofs 检测。 |
| WSL Ubuntu，Python 3.12.3 | 365 项通过，覆盖存储、调试钩子、文件工具和真实沙箱工作进程。此前已有的子进程清理及 `fork` 警告仍然存在。 |
| macOS | 本机未执行。已配置 `native-core` CI 矩阵，但本次改动尚未在远程 CI 中运行。 |
| Ruff 代码检查、格式检查、SPDX 文件头检查 | 通过。 |

上表是第一批基础适配的历史结果，不包含后续新增的 shell 测试。当前已实现原生命令执行，但仍不能据此宣称真实模型、macOS 实际环境和三平台安装包均已完成验证。

## 原生 Shell 适配验证结果

2026-09-13 的本地验证结果如下。表中的 Windows/Linux 自动化回归使用脚本化模型响应，不调用付费模型接口；ACP 连通性检查和用户试用确认另行记录。

| 环境或检查 | 结果与范围 |
|---|---|
| 原生 Windows，Python 3.12 | 212 项通过。覆盖原生基础能力、存储、shell 生命周期和编码流程；shell 测试分别运行于 PowerShell 7.6.5 和 Windows PowerShell 5.1.26100.9444。 |
| Linux 默认主测试集，WSL Ubuntu / Python 3.12 | 7,190 项通过、38 项跳过、3 项预期失败、298 项按标记排除。此次默认测试不包含真实模型、压力测试和独立沙箱测试。 |
| Linux 实际沙箱工作进程 | 62 项通过、63 项按标记排除。 |
| 核心包构建与安装 | wheel 和源码分发包构建成功，均包含 `_powershell_worker.ps1`。脱离源码目录，在独立环境安装 wheel 后，两版 PowerShell 的参数、UTF-8 stdin 和工作目录检查通过。 |
| 原生 Windows ACP 试用 | ACP 握手和真实模型的工具调用往返检查通过，用户确认实际使用正常。个人模型地址、密钥和编辑器配置不随源码提交；此反馈不替代完整的跨模型任务评测。 |
| 静态检查 | 全仓库 Ruff 代码检查、格式检查、SPDX 文件头检查及 `git diff --check` 通过。新增四个 Python 实现模块的 Pyright 检查为零诊断；未清理全仓库既有类型诊断。 |
| macOS、Python 3.13 | 已纳入原生 CI 矩阵，本轮没有相应实机或远程 CI 运行结果。 |

Windows 验证还覆盖：含空格和中文的路径、空参数及引号、超过 Windows 命令行长度的 stdin、流式输出、语法错误恢复、显式退出码、超时与取消后的子进程回收、工作进程意外退出后重建，以及没有 ripgrep 时的符号查询回退。

编码流程测试在临时 Git 项目中完成“读取文件、修改代码、查询符号、执行单元测试、检查 Git diff”，并验证 CodingAgent 会将当前 shell 环境放入模型上下文。它证明了这一受控流程可执行，不等于真实模型在任意项目上的成功率评测。

Linux 回归仍报告既有的子进程退出清理和 `fork` 等警告，未在本轮顺带重构。远程 CI 的运行结果需要单独核查，不能把已配置矩阵写成已经通过矩阵。
