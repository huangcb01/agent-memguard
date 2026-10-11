# agent-memguard

给 coding agent 执行的命令加一层环境内存压力保护。内存充足时允许任务继续增长；只有容器或 WSL 虚拟机整体内存紧张时，才选择受保护任务终止，并返回原因、内存指标和退出码。没有单进程或单任务 RSS 上限。

Linux（含 WSL2）、cgroup v2、Python 3.10+，运行时只依赖 Python 标准库。容器读取 cgroup 内存，WSL2 读取虚拟机可用内存；运行时无需管理员权限。

## 快速开始

无需安装，直接使用仓库内的可执行文件：

```bash
./bin/memguard --check
./bin/memguard -- python3 script.py
./bin/memguard -- bash -c 'command_a | command_b'
```

也可以安装到独立虚拟环境：

```bash
python3 -m venv .venv
.venv/bin/pip install .
.venv/bin/memguard --check
```

`--check` 打印当前环境总量、可用内存、整体软/硬阈值和交换区指标，不执行目标命令。默认使用容器模式，没有有限 cgroup 内存上限时拒绝启动；当前 WSL2 使用 `--mode wsl`。

## 环境切换

命令行用一个参数切换：

```bash
# 原集群容器：监控整个受限 cgroup 及有限父层级
./bin/memguard --mode container -- python3 script.py

# 当前 WSL2：监控整个 WSL 虚拟机的实时可用内存
./bin/memguard --mode wsl -- python3 script.py
./bin/memguard --mode wsl --check
```

CLI、OpenCode 的 shell 入口和 Codex hook 都支持同一个环境变量：

```bash
export MEMGUARD_MODE=wsl        # WSL2
# export MEMGUARD_MODE=container  # 切回集群容器（默认）
```

在启动 agent 前设置，使 shell/hook 继承该变量；已有实例需要重启。CLI 的 `--mode` 优先于环境变量，非法模式会拒绝执行命令；hook 会返回 `deny`。hook 会把模式写入改写后的命令，执行端无需再次配置环境变量。

两种模式均使用环境压力保护，`--check` 显示 `protection: "environment-pressure"`、`task_rss_limit: null`。RSS 合计只用于选择受害任务，不会单独触发内存终止。任务增长到几 GiB 也可以继续运行，只要整体内存仍充足。

WSL 模式每次采样读取 `/proc/meminfo` 的 `MemTotal`、`MemAvailable`，用 `MemTotal - MemAvailable` 表示已占用且不易回收的内存；可回收缓存由内核的 `MemAvailable` 估算。若存在有限 cgroup 上限，也同时监控这些层级。读取原生 `/proc/meminfo` 前会确认当前是 WSL 内核；容器模式不会回退读取可能属于宿主机的指标。WSL 保护范围是 Linux 虚拟机，不包含 Windows 原生进程。

启动前只检查环境是否已经达到软压力阈值，不为每个任务预留固定预算。运行中持续重读总量、可用量及有限 cgroup 范围；上限或可用内存变化后，会按最新指标判断压力。

旧版的 `--limit`、`--allow-unlimited` 参数已移除，调用脚本需要删除这些参数。`MEMGUARD_LIMIT` 不再使用，也不再提供 RSS-only 模式。更新后重启现有启动器，使所有任务统一采用新的保护策略。

## 保护机制

1. 启动前读取环境内存，若已经达到软阈值则拒绝启动；通过后登记任务和所属环境层级。
2. 将任务放进独立会话，执行目标程序前把 `oom_score_adj` 设置为 `+500`。避免高内存命令继承 Paseo/agent 的负值保护。设置失败时不执行命令。
3. 每 250 ms 读取环境指标和 `/proc`，统计任务后代 RSS 及增长量，用于受害任务排序。共享内存可能重复计算，但不会因此限制任务大小。
4. 环境达到软阈值并持续 500 ms 时，优先选择 RSS 大、近期增长明显的任务，发送 TERM。同一承压层级的并发启动器协调选一个任务。
5. 硬压力下对选中任务发送 KILL；其他情况下默认 TERM 宽限 500 ms。确认整个任务树退出后继续判断余量，压力仍高则冷却后选择下一任务。
6. 启动器作为 child subreaper，跟踪普通 worker、创建新会话的后代和父进程退出后收养的孤儿。使用 PID + 启动时间核对身份；系统支持时使用 pidfd 发送信号。

容器工作集估算（只计算实际已使用内存，不含未来任务预算）：

```text
可回收部分 = max(0, inactive_file - file_dirty - file_writeback)
工作集估算 = max(0, memory.current - 可回收部分)
```

这不是精确的不可回收内存量。不会把 `slab_reclaimable` 全部扣除；容器模式不读取 `/proc/meminfo`。

会检查本进程 cgroup 到可见挂载根之间所有有限上限。任一有限层级触发压力即可保护。

每次采样会刷新有限上限的协调范围。启动时为 `memory.max=max` 的层级，运行期间设置有限上限后也会参与压力保护；取消上限后则移出范围。多个任务只共享无限上限的祖先时，不会因此互相阻挡终止选择。

同一 PID/cgroup 命名空间内的启动器共享登记表。压力选择只考虑属于承压环境的任务，WSL 虚拟机压力可在登记任务中选择；兄弟 cgroup 各自承压时独立保护，父级承压时共同协调。只有已选任务能释放当前承压层级的内存时，才等待它的终止与冷却。不同启动器使用不同压力阈值时，只有所有报告相关层级压力的启动器都确认恢复，才会撤销尚未执行的请求。

执行终止前会保存原因。其他启动器确认根进程及全部存活后代已退出后，会释放活跃请求，让持续压力下的后续选择继续；终止结果单独保留，直到原启动器读取并退出。即使原启动器停止响应，也不会永久阻挡后续保护；恢复后仍报告 `environment_pressure` 和退出码 125。仅发送 SIGKILL 不会被当作任务树已经退出。

## 交换区

WSL 模式报告 `SwapTotal` 与 `SwapTotal - SwapFree`；容器模式在指标存在时报告 `memory.swap.max`、`memory.swap.current`。交换区单独列出，不加到物理内存容量，也不因 swap 使用率高而单独杀任务。物理内存可用量仍充足时，即使 swap 已用满，任务也允许继续运行；大量空闲 swap 也不会掩盖物理内存已经紧张的情况。当前不根据换入换出速度或 PSI 自动调整阈值。

任务的子级软终止请求尚未完成时，如果该任务受限的父级出现硬压力，会升级已有请求；其他启动器仍可执行兜底 SIGKILL，不必等待受害任务的启动器恢复采样。

## 默认参数

| 参数 | 默认值 | 对 8 GiB 容器的含义 |
| --- | --- | --- |
| 环境软阈值 | 81.25% | 6.5 GiB 工作集估算，约剩余 1.5 GiB |
| 环境硬阈值 | 87.5% | 7 GiB 工作集估算，约剩余 1 GiB |
| 采样间隔 | 250 ms | 读取 cgroup 和进程树 |
| 软阈值持续时间 | 500 ms | 避免短暂波动 |
| TERM 宽限 | 500 ms | 硬阈值不等待 |
| 目标 OOM 优先级 | +500 | 目标及后续普通子进程更易成为内核 OOM 对象 |

可用 CLI 调整，执行 `memguard --help` 查看。内存单位按 1024 计算。

shell 和 hook 模式使用环境变量调整：

```bash
export MEMGUARD_MODE=container  # WSL2 使用 wsl
export MEMGUARD_REAL_SHELL=/bin/bash
export MEMGUARD_INTERVAL=0.25
export MEMGUARD_SOFT_FRACTION=0.8125
export MEMGUARD_HARD_FRACTION=0.875
export MEMGUARD_SOFT_DURATION=0.5
export MEMGUARD_TERM_GRACE=0.5
export MEMGUARD_OOM_SCORE_ADJ=500
```

共享登记表默认放在 `XDG_RUNTIME_DIR/agent-memguard`；没有该目录时使用本地临时目录 `agent-memguard-<uid>`。推荐本地存储，避免每次采样访问 AFS/NFS。可用 `MEMGUARD_STATE_DIR` 或 `--state-dir` 指定；同一容器的启动器应使用同一目录。目录必须由当前用户拥有，且不能被其他用户写入。登记表不保存命令或环境变量。

登记表格式已升级为 v3，使用新的状态文件；旧版启动器仍可能执行旧 RSS 策略，更新后应统一重启。

部分 AFS 环境会映射 root 创建目录的 UID；这种目录也会被所有权检查拒绝，应使用本地状态目录。

`--no-admission-check` 可跳过启动时的整体压力检查，但不会关闭运行中的保护。`--cgroup-dir` 或 `MEMGUARD_CGROUP_DIR` 用于明确指定控制器位置；实际运行应指向任务所属的控制器。`--meminfo-file` 或 `MEMGUARD_MEMINFO_FILE` 可注入 WSL 模拟指标供测试，实际 WSL 应使用默认 `/proc/meminfo`。

## OpenCode v2 接入

将以下字段合并到现有配置，不要覆盖 provider 等其他设置：

```json
{
  "shell": "/absolute/path/agent-memguard/bin/memguard-shell"
}
```

OpenCode 按 `memguard-shell -c "原命令"` 调用；启动器监控真正 shell 及其后代。`-lc` 等参数也会透传给真正 shell。

本项目没有自动修改 OpenCode 配置。重新启动相应 OpenCode 实例后，先运行下面的命令确认返回值是 `500`：

```bash
cat /proc/self/oom_score_adj
```

本机研究针对 OpenCode 2.0.26：自定义路径不存在时可能回退系统 Bash。因此必须确保配置路径存在且可执行，并用模拟环境压力验收保护。

独立 PTY 插件和 MCP 工具可能直接启动程序，绕过内置 shell；需要另外包装其执行入口，或者禁用这些入口。

## Codex 接入

本机研究针对 Codex 0.160.1 的 Linux 执行器：`SHELL`、PATH 中的自定义 Bash、顶层 `shell` 配置和工具参数中的自定义文件路径，都没有提供可靠的启动器接入。使用官方 `PreToolUse` hook 改写 Bash 命令。

把 [examples/codex-hooks.json](examples/codex-hooks.json) 中的路径改为实际绝对路径，合并到活动 `CODEX_HOME/hooks.json`（通常为 `~/.codex/hooks.json`），或对应配置层的 inline hooks。不要覆盖已有其他 hooks。

```json
{
  "hooks": {
    "PreToolUse": [{
      "matcher": "Bash",
      "hooks": [{
        "type": "command",
        "command": "/absolute/path/agent-memguard/bin/memguard-codex-hook",
        "timeout": 5
      }]
    }]
  }
}
```

hook 会保留原命令为一个经过 shell 引号处理的参数，改写为：

```text
memguard --mode container -- /bin/bash -c '原命令'
```

设置 `MEMGUARD_MODE=wsl` 后，改写命令中的模式变为 `--mode wsl`。

自动寻找相邻的 `memguard`，也可用 `MEMGUARD_BIN` 指定绝对可执行路径。无效输入或找不到启动器时，hook 返回有效的 `deny` 决定，而不是用非零退出码导致 Codex 忽略 hook 错误。

根据 [Codex hooks 文档](https://developers.openai.com/codex/hooks/)，非托管 hook 需经过信任审阅；新增或更改后可能被跳过。部署时必须确认 hook 已信任且确实运行。hook 被禁用、跳过、超时或基础设施故障时，本项目不能保证拦截。测试中的临时 hook 可以使用一次性的 `--dangerously-bypass-hook-trust`；不要把它误当作 hook 已在日常会话启用的证据。

Codex 的异步命令可能先返回 session ID，之后再由 `write_stdin` 收取最终输出。保护终止的信息随正常命令输出通道返回。`Bash` matcher 不覆盖所有 MCP、自定义代码执行和后台工具，须逐入口验收。

## 输出、信号与退出码

- 正常运行：stdin/stdout/stderr 直接继承；透传目标退出码，不缓存目标输出。
- 保护终止或启动器自身失败：返回 `125`，stderr 输出 `[memguard]` 加 JSON，包含原因和内存指标，不打印命令参数或环境变量。
- 用户中断：转发信号；例如 SIGINT 返回 `130`。
- 目标收到其他信号：返回 `128 + signal`，不把 SIGKILL 自动判定成内核 OOM。
- 目标根进程退出：清理仍运行的后代。要运行后台任务，请让后台工具管理整个启动器，不要在受保护 shell 里用 `&` 后直接退出。

目标本身也可能返回 125；应结合 stderr 中的 `[memguard]` 事件区分原因。

日志示例：

```text
[memguard] {"event":"terminated","reason":"environment_pressure","task_rss":...,"environments":[...]}
```

`--verbose` 会额外输出启动事件。否则正常成功命令不增加启动器日志。

## 验证

```bash
python3 -m unittest discover -s tests -v
```

测试使用几十 MiB 的任务和模拟指标，覆盖两种环境模式、宽裕内存下超过旧 RSS 上限的任务、WSL 实时可用内存与 swap 报告、环境变量与 CLI 优先级、shell/hook 模式传递、输出/退出码、输入、OOM 优先级、孤儿/新会话清理、TERM 升级、缓存、准入、监控失败、Ctrl+C、不同压力阈值、动态上限、父级受害任务选择、停止响应的启动器、持续硬压力下的后续选择、兄弟子级独立保护、2 秒采样下的跨启动器终止原因，以及 hook 引号处理。不会制造真实容器 OOM。

安装了 Codex 的机器可运行可选端到端验收：

```bash
python3 tests/integration_codex.py
```

使用本地 HTTP 模拟模型、临时 `CODEX_HOME` 和一次性 hook 信任绕过，验收正常命令 stdout/stderr/退出码 7、宽裕内存下的大任务成功，以及模拟环境压力下的退出码 125。模拟模型会用 `write_stdin` 收取异步结果，不修改日常配置、不调用真实模型，不制造真实 OOM。容器和 WSL 指标均使用临时文件，WSL 场景使用 `MEMGUARD_MODE=wsl python3 tests/integration_codex.py`。

可选 GitHub Actions 模板在 [examples/github-actions.yml](examples/github-actions.yml)。复制到 `.github/workflows/tests.yml` 可启用 Python 3.10/3.12/3.13 的测试矩阵。

## 能力边界

- 这是用户态监视器，不是内核硬上限。瞬间分配、内存回收停顿、慢文件系统和不可中断 I/O 仍可能让终止来不及发生。
- `oom_score_adj` 调整的是内核选择倾向，不能指定内核一定杀谁。启动器自身保留继承值，不尝试未经授权降低 OOM 优先级。
- 只会终止经过启动器登记的任务；环境压力统计也包含其他服务、agent 和相关内核/缓存内存。未登记的服务不由本项目终止。
- RSS 合计不等于任务独占物理内存，只用作受害任务选择。环境压力来自别的服务时，被选中的任务不一定是元凶。
- 子进程仍可能修改自己的 OOM 优先级；启动器无法防御有意规避的程序。意外失去启动器时 parent-death 信号只保护直接目标，不能保证所有后代都退出。
- 未专门实现完整交互式 shell job control；主要面向 agent 工具中的命令和由外部工具管理的后台任务。

容器或 WSL 本身的总内存配置决定环境容量。本项目不创建任务级内核内存上限，也不修改容器或 WSL 配置。

## License

MIT。
