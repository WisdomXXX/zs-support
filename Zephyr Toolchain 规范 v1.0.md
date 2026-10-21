# Zephyr Toolchain 规范 v1.0

## 1. 目标

Zephyr Toolchain 用于统一管理个人开发的 Ubuntu CLI 工具与 `.deb` 软件包。

核心目标：

- 所有自研工具有统一命名
- 所有工具可被快速搜索和发现
- 能查看工具用途、版本、安装位置、源码位置
- 支持 `.deb` 自动注册和卸载注销
- 所有 CLI 都有统一的 `--help`、`--version`
- 后续可扩展 install / update / doctor / registry 等能力
- 不替代 apt/dpkg，而是在其上增加一层个人工具管理能力

统一入口命令：

```bash
zt
```

含义：

```text
Zephyr Tools
```

---

# 2. 命名规范

所有自行维护的 Debian 包必须使用：

```text
zephyr-<tool-name>
```

例如：

```text
zephyr-hermesctl
zephyr-kb-sync
zephyr-proxy-check
zephyr-robot-net
zephyr-ssh-tunnel
```

CLI 命令本身不强制增加 `zephyr-` 前缀。

推荐：

```text
Package: zephyr-hermesctl
Command: hermesctl
```

不要：

```text
Package: hermesctl
Command: zephyr-hermesctl-cli-v2
```

原则：

```text
Deb 包名 = zephyr-<tool>
CLI 命令 = 简洁、明确、容易记忆
```

工具名必须：

- 全小写
- 使用 `-` 分隔
- 禁止空格
- 禁止下划线
- 禁止无意义缩写
- 尽量控制在 2~20 字符

推荐：

```text
kb-sync
proxy-check
robot-net
hermesctl
logscan
```

不推荐：

```text
my_tool
tool123
abc
ubuntu_script_final_v2
```

---

# 3. CLI 基础规范

每一个 CLI 最少必须支持：

```bash
tool --help
tool --version
```

建议同时支持：

```bash
tool help
tool version
```

例如：

```bash
hermesctl --help
hermesctl --version
```

标准版本输出：

```text
hermesctl 0.3.1
```

避免：

```text
version is: V0.3.1-final-new
```

---

# 4. CLI 返回码规范

遵循 Unix CLI 习惯。

```text
0    成功
1    通用错误
2    参数错误
3    配置错误
4    网络错误
5    权限错误
6    依赖缺失
7    服务不可用
8    文件不存在
```

例如：

```bash
proxy-check
echo $?
```

正常：

```text
0
```

网络失败：

```text
4
```

禁止所有情况都：

```bash
exit 0
```

---

# 5. 推荐 CLI 子命令规范

如果工具超过一个功能，优先使用：

```text
tool <command>
```

例如：

```bash
hermesctl status
hermesctl start
hermesctl stop
hermesctl restart
hermesctl logs
hermesctl doctor
```

推荐通用子命令：

```text
status
start
stop
restart
logs
config
doctor
info
version
help
```

不同工具不必全部实现。

---

# 6. doctor 规范

较复杂工具推荐实现：

```bash
tool doctor
```

doctor 用于检查运行环境。

例如：

```bash
hermesctl doctor
```

输出：

```text
Hermes Environment Check

✓ Docker installed
✓ Docker service running
✓ docker compose available
✓ /opt/hermes exists
✓ compose.yaml found
✓ gateway container running
✗ OPENAI_API_KEY not configured

6 passed
1 failed
```

doctor 必须只做诊断。

默认禁止：

- 修改配置
- 安装软件
- 删除文件
- restart 服务

如果支持自动修复，显式使用：

```bash
tool doctor --fix
```

---

# 7. 配置文件规范

个人工具配置统一优先使用：

```text
~/.config/<tool-name>/
```

例如：

```text
~/.config/hermesctl/
~/.config/kb-sync/
```

推荐主配置：

```text
~/.config/hermesctl/config.yaml
```

或：

```text
config.toml
```

全局配置使用：

```text
/etc/<tool-name>/
```

例如：

```text
/etc/hermesctl/config.yaml
```

优先级：

```text
CLI 参数
>
环境变量
>
用户配置 ~/.config/
>
系统配置 /etc/
>
程序默认值
```

---

# 8. 数据目录规范

按照 Linux/XDG 思路管理。

用户级数据：

```text
~/.local/share/<tool-name>/
```

用户缓存：

```text
~/.cache/<tool-name>/
```

用户日志：

```text
~/.local/state/<tool-name>/
```

系统服务数据：

```text
/var/lib/<tool-name>/
```

系统日志：

```text
/var/log/<tool-name>/
```

不要把运行数据放在：

```text
/usr/bin
/usr/local/bin
/etc
```

---

# 9. Debian 包目录规范

推荐项目结构：

```text
hermesctl/
├── README.md
├── CHANGELOG.md
├── VERSION
├── LICENSE
├── src/
├── tests/
├── scripts/
├── packaging/
│   └── debian/
│       ├── control
│       ├── postinst
│       ├── prerm
│       └── postrm
└── dist/
```

如果使用简单手工打包，也可以：

```text
package-root/
├── DEBIAN/
│   ├── control
│   ├── postinst
│   ├── prerm
│   └── postrm
└── usr/
    └── bin/
        └── hermesctl
```

---

# 10. Debian control 规范

示例：

```text
Package: zephyr-hermesctl
Version: 0.3.1
Section: utils
Priority: optional
Architecture: all
Maintainer: Zephyr Lee
Depends: bash, docker.io
Description: CLI for managing Hermes services
 Provides status, start, stop, restart, logs and diagnostics
 for Hermes Docker deployments.
```

Description 第一行必须：

- 简短
- 清楚说明工具用途

不要写：

```text
Description: My tool
```

---

# 11. 版本规范

统一使用 Semantic Versioning：

```text
MAJOR.MINOR.PATCH
```

例如：

```text
0.1.0
0.2.0
0.2.1
1.0.0
```

规则：

PATCH：

```text
0.2.0 -> 0.2.1
```

Bug 修复。

MINOR：

```text
0.2.1 -> 0.3.0
```

增加兼容功能。

MAJOR：

```text
1.3.2 -> 2.0.0
```

出现破坏性修改。

开发阶段：

```text
0.x.x
```

稳定版本：

```text
1.0.0+
```

---

# 12. VERSION 文件规范

项目根目录保存：

```text
VERSION
```

内容只写：

```text
0.3.1
```

不要：

```text
v0.3.1
version=0.3.1
```

程序、Debian 包和 CI 都从这里读取版本。

---

# 13. README 规范

每个工具必须有 README。

最低结构：

```markdown
# hermesctl

一句话描述工具。

## Installation

## Usage

## Commands

## Configuration

## Examples

## Uninstall

## Development
```

最重要的是 Usage。

例如：

```bash
hermesctl status
hermesctl logs
hermesctl restart
```

---

# 14. Zephyr Tool Registry

统一注册表目录：

```text
/var/lib/zt/
```

主注册表：

```text
/var/lib/zt/registry.json
```

推荐 JSON，而不是 YAML。

原因：

- shell / Python 都容易解析
- 修改安全
- 数据结构清晰
- 不依赖 YAML parser

示例：

```json
{
  "schema_version": 1,
  "tools": {
    "hermesctl": {
      "name": "hermesctl",
      "package": "zephyr-hermesctl",
      "command": "hermesctl",
      "version": "0.3.1",
      "description": "Manage Hermes Docker services",
      "category": "ai",
      "binary": "/usr/bin/hermesctl",
      "source": "/srv/tools/src/hermesctl",
      "homepage": "",
      "installed_at": "2026-09-16T16:00:00+08:00"
    }
  }
}
```

---

# 15. Registry 字段规范

必须字段：

```text
name
package
command
version
description
category
binary
installed_at
```

可选字段：

```text
source
homepage
repository
tags
author
config
service
```

例如：

```json
"tags": [
  "docker",
  "hermes",
  "ai"
]
```

---

# 16. 分类规范

默认 category：

```text
ai
robotics
network
docker
linux
knowledge
development
system
monitoring
automation
other
```

例如：

```text
hermesctl      ai
robot-net      robotics
proxy-check    network
kb-sync        knowledge
ssh-tunnel     network
```

尽量不要随意创建重复分类。

例如避免：

```text
network
networking
net
internet
```

统一：

```text
network
```

---

# 17. zt CLI

核心管理工具：

```bash
zt
```

必须支持：

```bash
zt list
zt search
zt info
zt doctor
zt register
zt unregister
zt version
zt help
```

---

# 18. zt list

执行：

```bash
zt list
```

输出：

```text
NAME          VERSION   CATEGORY    DESCRIPTION
-------------------------------------------------------------
hermesctl     0.3.1     ai          Manage Hermes services
kb-sync       0.1.2     knowledge   Sync knowledge repository
proxy-check   0.2.0     network     Diagnose proxy connectivity
robot-net     0.1.0     robotics    Diagnose robot networking
```

支持：

```bash
zt list --category network
```

也支持：

```bash
zt list network
```

---

# 19. zt search

模糊搜索：

```bash
zt search docker
```

搜索字段：

```text
name
command
description
category
tags
package
```

例如：

```bash
zt search hermes
```

输出：

```text
hermesctl
  Manage Hermes Docker services

Package:
  zephyr-hermesctl

Command:
  hermesctl
```

---

# 20. zt info

执行：

```bash
zt info hermesctl
```

输出：

```text
Name:        hermesctl
Package:     zephyr-hermesctl
Version:     0.3.1
Category:    ai

Description:
Manage Hermes Docker services

Command:
hermesctl

Binary:
/usr/bin/hermesctl

Source:
/srv/tools/src/hermesctl

Installed:
2026-09-16 16:00
```

---

# 21. zt doctor

执行：

```bash
zt doctor
```

检查 registry 中所有软件：

```text
Zephyr Toolchain Doctor

✓ hermesctl
  package installed
  binary exists
  executable

✓ kb-sync
  package installed
  binary exists
  executable

✗ proxy-check
  package installed
  binary missing

Summary:
2 healthy
1 broken
```

检查内容：

```text
dpkg package exists
binary exists
binary executable
--version works
registry valid
```

---

# 22. zt register

CLI：

```bash
sudo zt register \
  --name hermesctl \
  --package zephyr-hermesctl \
  --command hermesctl \
  --version 0.3.1 \
  --description "Manage Hermes Docker services" \
  --category ai \
  --binary /usr/bin/hermesctl
```

允许：

```text
--source
--tags
--homepage
```

重复注册默认更新。

---

# 23. zt unregister

执行：

```bash
sudo zt unregister hermesctl
```

只删除 Registry 信息。

禁止：

- 删除 binary
- 删除 package
- 删除配置
- 删除用户数据

软件卸载交给：

```bash
apt remove
```

---

# 24. Debian postinst

每一个 Zephyr deb 安装完成后调用：

```bash
zt register
```

例如：

```bash
#!/bin/bash

set -e

if command -v zt >/dev/null 2>&1; then
    zt register \
        --name hermesctl \
        --package zephyr-hermesctl \
        --command hermesctl \
        --version 0.3.1 \
        --description "Manage Hermes Docker services" \
        --category ai \
        --binary /usr/bin/hermesctl
fi

exit 0
```

重要：

如果 zt 不存在：

```text
不能导致 deb 安装失败
```

因此必须先：

```bash
command -v zt
```

---

# 25. Debian prerm

卸载：

```bash
#!/bin/bash

set -e

if command -v zt >/dev/null 2>&1; then
    zt unregister hermesctl || true
fi

exit 0
```

registry 清理失败：

```text
不能阻止 apt remove
```

---

# 26. zt 自己的 Debian 包

zt 自身包：

```text
zephyr-tools
```

安装命令：

```bash
sudo apt install ./zephyr-tools_1.0.0_all.deb
```

安装：

```text
/usr/bin/zt
```

Registry：

```text
/var/lib/zt/
```

配置：

```text
/etc/zt/
```

推荐：

```text
/etc/zt/config.toml
```

---

# 27. 推荐服务器目录

服务器源代码统一：

```text
/srv/tools/
```

结构：

```text
/srv/tools/
├── src/
│   ├── hermesctl/
│   ├── kb-sync/
│   ├── proxy-check/
│   └── robot-net/
│
├── packages/
│   ├── amd64/
│   ├── arm64/
│   └── all/
│
├── scripts/
│
└── README.md
```

其中：

```text
src/
```

放源码。

```text
packages/
```

放生成后的 deb。

---

# 28. Git 仓库规范

推荐：

一个工具一个 Git Repository。

例如：

```text
hermesctl
kb-sync
proxy-check
robot-net
```

如果工具非常小，也可以统一：

```text
zephyr-tools-monorepo
```

结构：

```text
tools/
├── hermesctl/
├── kb-sync/
└── proxy-check/
```

初期更推荐 monorepo。

原因：

个人维护成本更低。

---

# 29. 日志规范

CLI 默认输出到 stdout / stderr。

正常：

```text
stdout
```

错误：

```text
stderr
```

支持：

```bash
tool --verbose
```

或者：

```bash
tool -v
```

如果长期运行：

```text
~/.local/state/<tool>/logs/
```

系统 daemon：

```text
journalctl
```

优先使用 systemd journal，不自己重复造日志系统。

---

# 30. Shell Completion

成熟工具推荐支持：

```text
bash
zsh
```

例如：

```bash
zt completion bash
```

输出 shell completion。

最终允许：

```bash
zt <TAB>
```

出现：

```text
list
search
info
doctor
register
unregister
```

---

# 31. 安全规范

工具禁止默认执行危险操作：

```text
rm -rf
iptables reset
ufw disable
docker system prune
apt purge
```

必须显式确认或增加：

```bash
--force
```

例如：

```bash
tool clean
```

应该提示：

```text
This will remove 3 files.

Continue? [y/N]
```

自动化环境：

```bash
tool clean --yes
```

---

# 32. sudo 规范

不要整个工具都要求 sudo。

推荐：

```bash
zt list
zt search
zt info
```

普通用户即可。

只有：

```bash
zt register
zt unregister
```

需要 root。

工具内部只在真正需要权限时提示：

```text
Permission denied.

Try:
sudo hermesctl install-service
```

---

# 33. 不要做的事情

禁止把所有自研工具直接散落：

```text
/usr/local/bin/
```

却没有任何记录。

禁止手动维护：

```text
~/tools.txt
```

作为唯一信息源。

禁止不同项目使用完全不同规范。

禁止：

```text
tool
tool2
tool-final
tool-new
tool-final-final
```

禁止包版本与 CLI 版本不一致。

---

# 34. 最低合格标准

以后你自己写任何 CLI，满足以下要求才算完成：

```text
[ ] 有清晰名称
[ ] 有 README
[ ] 有 VERSION
[ ] 支持 --help
[ ] 支持 --version
[ ] 返回码正确
[ ] Debian Package 使用 zephyr-* 前缀
[ ] 有 Description
[ ] 安装至标准 Linux 路径
[ ] 安装时注册到 zt
[ ] 卸载时从 zt 注销
```

复杂工具再增加：

```text
[ ] doctor
[ ] config
[ ] logging
[ ] shell completion
[ ] tests
```

---

# 35. 推荐第一阶段

不要一开始做完整 Package Manager。

v1 只实现：

```bash
zt list
zt search
zt info
zt register
zt unregister
zt doctor
```

Registry：

```text
/var/lib/zt/registry.json
```

Package：

```text
zephyr-tools
```

CLI：

```text
zt
```

技术方案优先：

```text
Python 3
```

只使用 Python 标准库：

```text
argparse
json
pathlib
subprocess
datetime
shutil
```

尽量做到：

```text
zero third-party dependencies
```

---

# 36. 后续路线

v1：

```text
Registry
List
Search
Info
Doctor
```

v1.5：

```text
completion
tags
JSON output
```

例如：

```bash
zt list --json
```

v2：

```text
zt install
zt remove
zt update
zt upgrade
```

v3：

```text
个人 APT Repository
```

例如最终实现：

```bash
sudo apt install zephyr-hermesctl
```

而不是：

```bash
sudo apt install ./xxx.deb
```

---

# 37. 最终使用体验

以后开发完成：

```bash
sudo apt install ./zephyr-hermesctl_0.3.1_all.deb
```

自动注册。

查询所有工具：

```bash
zt list
```

忘记名字：

```bash
zt search docker
```

查询详情：

```bash
zt info hermesctl
```

检查所有工具：

```bash
zt doctor
```

使用：

```bash
hermesctl status
```

升级：

```bash
sudo apt install ./zephyr-hermesctl_0.4.0_all.deb
```

Registry 自动更新。

卸载：

```bash
sudo apt remove zephyr-hermesctl
```

Registry 自动注销。

---

# 核心原则

Zephyr Toolchain 本身不负责重新发明 Linux 包管理。

底层继续依赖：

```text
apt
dpkg
systemd
Linux filesystem
```

zt 解决的是：

```text
“我到底写过哪些工具？”
“这个工具叫什么？”
“它是干什么的？”
“它安装在哪里？”
“它属于哪个 deb？”
“源码在哪里？”
“现在还能不能正常运行？”
```

最终形成：

```text
APT / DPKG
     │
     ▼
Zephyr Tool Registry
     │
     ▼
     zt
     │
     ├── list
     ├── search
     ├── info
     └── doctor
```

这就是 Zephyr Toolchain v1 的边界。