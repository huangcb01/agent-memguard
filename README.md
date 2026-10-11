# agent-memguard

给 coding agent 执行的命令加一层内存保护。命令超过任务预算或容器内存压力过高时，终止命令及其后代，并把原因、内存指标和退出码返回给 agent。

Linux、cgroup v2、Python 3.10+，运行时只依赖 Python 标准库。适用于 cgroup 只读、不能给每个任务创建独立内存限制的容器。

## 快速开始

无需安装，直接使用仓库内的可执行文件：

```bash
./bin/memguard --check
./bin/memguard --limit 1G -- python script.py
./bin/memguard --limit 512M -- bash -c 'command_a | command_b'
```

也可以安装到独立虚拟环境：

```bash
python3 -m venv .venv
.venv/bin/pip install .
.venv/bin/memguard --check
```

`--check` 只打印有效配置和当前 cgroup 内存，不执行目标命令。没有有限 cgroup 内存上限时默认拒绝启动；明确使用 `--allow-unlimited` 才允许仅按任务 RSS 保护。

## 保护机制

1. 启动前读取 cgroup 内存并登记任务预算。并发任务共享登记表，对还没有使用的预算做保守准入检查。
2. 将任务放进独立会话，执行目标程序前把 `oom_score_adj` 设置为 `+500`。避免高内存命令继承 Paseo/agent 的负值保护。设置失败时不执行命令。
3. 每 250 ms 读取容器指标和 `/proc`，统计任务后代的 RSS 合计。共享内存会重复计算，因此偏保守。
4. 任务超过预算时发送 TERM；容器到达软阈值并持续 500 ms 时，在登记任务中优先选择占用大、近期增长明显的任务。并发启动器协调选一个任务，避免同时全部终止。
5. 容器到达硬阈值时立即 KILL；其他情况下 TERM 宽限最多 500 ms。KILL 后继续清理后代。
6. 启动器作为 child subreaper，跟踪普通 worker、创建新会话的后代和父进程退出后收养的孤儿。使用 PID + 启动时间核对身份；系统支持时使用 pidfd 发送信号。

容器工作集估算：

```text
可回收部分 = max(0, inactive_file - file_dirty - file_writeback)
工作集估算 = max(0, memory.current - 可回收部分)
```

这不是精确的不可回收内存量。不会把 `slab_reclaimable` 全部扣除，也不会使用可能显示宿主机内存的 `/proc/meminfo`。

会检查本进程 cgroup 到可见挂载根之间所有有限上限。任一有限层级触发压力即可保护。

## 默认参数

| 参数 | 默认值 | 对 8 GiB 容器的含义 |
| --- | --- | --- |
| 任务 RSS 预算 | 1 GiB | 单次命令及其后代合计 |
| 容器软阈值 | 81.25% | 6.5 GiB 工作集估算 |
| 容器硬阈值 | 87.5% | 7 GiB 工作集估算 |
| 采样间隔 | 250 ms | 读取 cgroup 和进程树 |
| 软阈值持续时间 | 500 ms | 避免短暂波动 |
| TERM 宽限 | 500 ms | 硬阈值不等待 |
| 目标 OOM 优先级 | +500 | 目标及后续普通子进程更易成为内核 OOM 对象 |

可用 CLI 调整，执行 `memguard --help` 查看。内存单位按 1024 计算。

shell 和 hook 模式使用环境变量调整：

```bash
export MEMGUARD_LIMIT=1G
export MEMGUARD_REAL_SHELL=/bin/bash
export MEMGUARD_INTERVAL=0.25
export MEMGUARD_SOFT_FRACTION=0.8125
export MEMGUARD_HARD_FRACTION=0.875
export MEMGUARD_SOFT_DURATION=0.5
export MEMGUARD_TERM_GRACE=0.5
export MEMGUARD_OOM_SCORE_ADJ=500
```

共享登记表默认放在 `XDG_RUNTIME_DIR/agent-memguard`；没有该目录时使用本地临时目录 `agent-memguard-<uid>`。推荐本地存储，避免每次采样访问 AFS/NFS。可用 `MEMGUARD_STATE_DIR` 或 `--state-dir` 指定；同一容器的启动器应使用同一目录。目录必须由当前用户拥有，且不能被其他用户写入。登记表不保存命令或环境变量。

部分 AFS 环境会映射 root 创建目录的 UID；这种目录也会被所有权检查拒绝，应使用本地状态目录。

`--no-admission-check` 可跳过启动前预算检查，但不会关闭运行中的保护。`--cgroup-dir` 或 `MEMGUARD_CGROUP_DIR` 用于明确指定控制器位置和模拟指标测试；实际运行应指向任务所属的控制器。

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

本机研究针对 OpenCode 2.0.26：自定义路径不存在时可能回退系统 Bash。因此必须确保配置路径存在且可执行，并用低预算任务验收保护。

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
memguard -- /bin/bash -c '原命令'
```

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
[memguard] {"event":"terminated","reason":"task_rss_limit","task_rss":...,"limit":...}
```

`--verbose` 会额外输出启动事件。否则正常成功命令不增加启动器日志。

## 验证

```bash
python3 -m unittest discover -s tests -v
```

测试使用几十 MiB 的任务和模拟 cgroup 指标，覆盖输出/退出码、输入、OOM 优先级、worker 合计、孤儿/新会话清理、TERM 升级、缓存、准入、监控失败、Ctrl+C、并发受害任务选择和 hook 引号处理。不会制造真实容器 OOM。

安装了 Codex 的机器可运行可选端到端验收：

```bash
python3 tests/integration_codex.py
```

使用本地 HTTP 模拟模型、临时 `CODEX_HOME` 和一次性 hook 信任绕过，验收正常命令 stdout/stderr/退出码 7，以及小预算终止的原因和退出码 125。模拟模型会用 `write_stdin` 收取异步结果，不修改日常配置、不调用真实模型。需要本机存在有限 cgroup v2 内存上限。

可选 GitHub Actions 模板在 [examples/github-actions.yml](examples/github-actions.yml)。复制到 `.github/workflows/tests.yml` 可启用 Python 3.10/3.12/3.13 的测试矩阵。

## 能力边界

- 这是用户态监视器，不是内核硬上限。瞬间分配、内存回收停顿、慢文件系统和不可中断 I/O 仍可能让终止来不及发生。
- `oom_score_adj` 调整的是内核选择倾向，不能指定内核一定杀谁。启动器自身保留继承值，不尝试未经授权降低 OOM 优先级。
- 仅保护经过启动器的命令。Paseo/agent 本身的增长、Codex 外层 shell/profile 加载、其他执行入口不在此任务预算内。
- RSS 合计不等于任务独占物理内存；容器指标还能覆盖部分缓存和内核增长。压力来自别的服务时，被选中的任务不一定是元凶。
- 子进程仍可能修改自己的 OOM 优先级；启动器无法防御有意规避的程序。意外失去启动器时 parent-death 信号只保护直接目标，不能保证所有后代都退出。
- 未专门实现完整交互式 shell job control；主要面向 agent 工具中的命令和由外部工具管理的后台任务。

如果可以在容器配置侧提供独立子 cgroup，应优先使用每任务 `memory.max`、`memory.high` 和 `memory.oom.group` 做内核隔离，本项目作为反馈与提前终止层。

## License

MIT。
