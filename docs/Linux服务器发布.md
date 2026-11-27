# EvoSight Linux 发布和更新

后端由 `evosight.service` 管理，只监听服务器本机的 8765。公网入口由反向代理提供 HTTPS 和访问认证。模型配置由服务从 `/etc/evosight` 和服务私有目录读取。

## 路径

| 路径 | 内容 |
| --- | --- |
| `/opt/evosight/releases/<git-commit>` | 已构建的前端、源代码和独立的 Python 环境 |
| `/opt/evosight/current` 与 `previous` | 当前及上一版代码的符号链接 |
| `/opt/evosight/runtime` | Python 3.11 或以上、Codex CLI、DSH 和可选的私有 SQLite 动态库 |
| `/var/lib/evosight/data` | SQLite、Agent 工作区和 DSH 会话状态 |
| `/var/lib/evosight/codex` | Codex 配置和原生会话 |
| `/var/lib/evosight/backups` | 每次切换前的 SQLite 快照 |

首次部署需要创建专用的 `evosight` 用户，安装运行时和 `deploy/linux/evosight.service`。系统服务需要读取已配置的 Codex `kprsi` profile。DSH 的 provider 配置放在 `/etc/evosight/dsh.yml`，服务环境放在 `/etc/evosight/agents.env`。公开入口应在访问认证后才转发到 8765。

Python 的 `sqlite3` 模块需要 SQLite 3.35 或以上，并支持 JSON 函数。对于系统 SQLite 较旧且 Python 动态链接到它的服务器，把兼容的 `libsqlite3.so.0` 放在 `/opt/evosight/runtime/lib`。工作台和更新器从这个目录加载库。可用 `LD_LIBRARY_PATH=/opt/evosight/runtime/lib /opt/evosight/runtime/bin/python -c 'import sqlite3; print(sqlite3.sqlite_version)'` 检查实际加载的版本。

将 `deploy/linux/release.sh` 和 `release.py` 安装到固定目录，保证切换和回滚时可以调用同一个更新器。

```bash
install -D -m 0755 deploy/linux/release.sh /opt/evosight/bin/release
install -D -m 0644 deploy/linux/release.py /opt/evosight/bin/release.py
```

## 发布

提交代码并推送后，从对应提交的工作区构建前端。依赖由前端的锁文件安装。

```powershell
Set-Location frontend
npm ci
npm run build
Set-Location ..
python deploy/linux/release.py package .work/evosight-release.tar.gz
```

通过 SSH 将发布包传到服务器。打包命令会输出 Git 提交和发布包的 SHA256。把 SHA256 一并传给更新命令。

```bash
/opt/evosight/bin/release apply /path/to/evosight-release.tar.gz <sha256>
```

更新器先校验发布包，再准备独立的 Python 环境，并检查 SQLite 是否满足运行要求。此时工作台仍在运行旧版。若有任务运行，更新器保留已准备的发布目录，待空闲后可重跑同一条命令。切换时会暂停服务，备份 SQLite，原子切换版本链接，启动并检查新版进程。启动或检查失败时会切回旧代码并重新启动它。

## 回滚和查询

```bash
/opt/evosight/bin/release rollback
readlink -f /opt/evosight/current
systemctl status evosight
journalctl -u evosight -n 50 --no-pager
```

回滚会切回上一版代码并沿用共享数据。涉及数据库迁移的发布可以先用备份库验证新旧版本兼容性。需要恢复数据库时，从切换前的 SQLite 快照操作，并先备份当前数据库。
