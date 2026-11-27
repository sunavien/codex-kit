# codex-kit

Codex 本地聊天管理 CLI，按归档时间批量清理聊天，提供预览、备份和恢复。

需要 Python 3.11+。第一版面向 macOS，已使用 Codex CLI 0.146.0 与 `state_5.sqlite` 格式验证。运行时使用 Python 标准库。

## 安装与预览

在仓库目录执行安装，再查看归档超过 30 天的聊天。

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/codex-kit prune --days 30 --dry-run
```

也可以直接从源码运行。

```bash
python3 -m codex_kit --help
python3 -m codex_kit prune --days 30 --dry-run
```

## 查看聊天与占用

```bash
codex-kit list
codex-kit list --days 30
codex-kit list --all
codex-kit stats
```

`list` 默认显示归档聊天。`stats` 展示活动、归档聊天数量、会话文件大小、默认备份目录占用，以及缺文件或归档时间缺失的数量。大小统计限于数据库中可识别的会话文件。

数据目录按 `--home`、`CODEX_HOME`、`~/.codex` 的顺序确定。`--home` 和 `--json` 可以放在子命令之前或之后。

```bash
codex-kit --home /path/to/codex list --json
codex-kit stats --json
```

## 清理归档聊天

```bash
codex-kit prune --days 30 --dry-run
codex-kit prune --days 30
```

第一条生成只读预览。第二条显示清单，要求输入 `yes`，完成备份后调用官方 `codex delete --force UUID`。需要在脚本中执行时，显式传入 `--yes`。

```bash
codex-kit prune --days 90 --yes --json
codex-kit prune --days 30 --backup-dir /Volumes/Backup/codex-kit
```

需要直接删除时，使用 `--no-backup`。执行时仍会显示清单并要求输入 `yes`，这个模式删除的聊天无法通过 codex-kit 恢复。`--no-backup` 和 `--backup-dir` 互斥。

```bash
codex-kit prune --days 30 --no-backup
codex-kit prune --days 30 --no-backup --dry-run
```

`--days 30` 表示归档时间严格早于运行时刻前 30 × 24 小时。`prune` 始终筛选归档聊天，`--archived` 可以显式传入。

清理计划包含官方命令会连带删除的子代理聊天。置顶、归档时间缺失、会话文件缺失、路径异常和存在循环关系的聊天会跳过。如果后代中存在活动聊天、归档未满指定天数或其他不符合条件的记录，父聊天会跳过，并在输出中显示原因。

执行前会重新检查归档状态、关系和文件内容。官方命令失败或删除结果检查失败时停止，备份模式的批次记录保留已成功删除的 ID。无备份模式仅在内存记录进度，失败信息会列出已确认删除的 ID，当前失败项可能已部分删除。官方仍可能因分叉聊天引用而拒绝删除。

## 备份与恢复

默认备份位于 `$CODEX_HOME/codex-kit/backups/<批次ID>/`。清理结束会打印完整路径，JSON 输出包含 `backup`。恢复时使用该批次目录。

```bash
codex-kit restore /absolute/path/to/batch
codex-kit restore /absolute/path/to/batch --yes
```

恢复前退出 Codex 桌面版和相关 CLI 进程。恢复只合并该批次涉及的聊天行、父子关系、关联状态、会话文件和名称索引。已存在且内容不同的记录会报冲突，相同记录可以重复恢复。中断后可以重新执行相同恢复命令。多个数据库逐库提交，整个恢复过程可能部分完成，操作日志记录当前状态。

备份包含状态、历史、目标、记忆、日志数据库的一致 SQLite 快照，以及选中聊天的 `.jsonl` / `.jsonl.zst` 文件和名称索引记录。数据库快照可能包含其他聊天的敏感内容，批次目录权限为 `0700`。工具不会复制 `auth.json`。备份保留删除数据并消耗额外空间，所显示的会话文件大小不代表净释放磁盘空间。

当前没有自动过期备份或定时清理功能。保留备份直到确认恢复需求已结束，再自行管理备份目录。

## 兼容性与错误

官方会话存储属于内部格式。删除前会检测官方 CLI 是否提供 `delete --force`。未知状态数据库版本或缺少父子关系表时会报错。预览无需启动 Codex。`--codex /path/to/codex` 可以指定官方可执行文件。

`codex-kit` 锁只协调自身的并发操作。它与其他 Codex 进程之间没有跨进程事务，检查完成与官方删除之间仍存在竞争窗口。批量清理前退出其他 Codex 进程，可以减少状态变化。工具会固定官方命令的本地 SQLite 目录为所选 `--home`。

退出码 `0` 表示命令成功，`2` 表示输入、兼容性或操作失败，`130` 表示中断。JSON 模式下操作错误写入标准错误的 `error` 字段。参数语法错误沿用 argparse 输出。

## 开发验证

```bash
python3 -m unittest discover -v
CODEX_KIT_NATIVE_TEST=1 python3 -m unittest discover -v
```

第一条运行隔离数据库测试。第二条额外使用已安装的 Codex 创建临时数据库和合成父子聊天，验证真正的官方删除与恢复。原生集成使用独立 `CODEX_HOME`。

设计与实现记录见 [实现计划](docs/implementation-plan.md)。项目遵循 [Apache-2.0](LICENSE) 许可证。
