# zs-support

智身科技售后运维采集工具，统一连接 ZSL-1/1W、ZSL2、ZSM1，远程录制 ROS 2 Bag，采集 Bag、日志和地图。

当前版本 **0.3.1**，提供 Ubuntu 命令行工具，支持设备连接、版本识别、场景录包以及 Bag、日志和地图采集。

## 安装

目标客户端：Ubuntu 22.04/24.04，Python 3.10+。deb 为 `all` 架构，架构标记不代表已完成 ARM64 验收。

```bash
sudo apt install ./dist/zephyr-zs-support_0.3.1_all.deb
zs-support --version
zs-support doctor
```

deb 安装 Python YAML、OpenSSH、sshpass、rsync 和 Bash 补全依赖；有 `zt` 时自动注册，无 `zt` 时照常安装。只安装 `zs-support`，没有 `zs` 别名。

Python 包安装适合开发环境；SSH/rsync 系统依赖仍需单独安装：

```bash
python3 -m venv --system-site-packages .venv
. .venv/bin/activate
python -m pip install -e .
zs-support --help
```

设备端需有 SSH、Bash、Python 3.8+、ROS 2 和 rsync；M1 脚本采集另需 sudo、flock。客户端不需要安装 ROS。工具不会自动修改机器人网络或安装远端依赖。

## 日常使用

```bash
zs-support             # 显示帮助，与 -h 一致
zs-support connect     # 选择 L1/L2/M1，仅显示两个板卡版本和 SN/tag/type
zs-support info        # 刷新版本与算法身份
zs-support bag         # 选择导航/感知/定位/建图，前台显示已录制秒数；Ctrl+C 保存
zs-support status      # 核验当前录制或最近记录；--all 查看全部历史
zs-support stop        # 停止后台/恢复中的会话；重复停止正常返回
zs-support pull        # 交互选择最近录包、已有 Bag、日志、地图或组合采集
zs-support pull log    # M1 本体优先脚本，失败自动回退日志目录
zs-support pull map    # 自动使用算力板，选择地图或整个根目录
zs-support pull bag    # 浏览并拉取已有 Bag，含不完整数据标记
zs-support pull all    # 组合采集 Bag/日志/地图
```

账号按产品和板卡内置，客户无需输入密码。第一次 SSH 接受并记录主机密钥，后续密钥变化会拒绝连接；核实设备身份后再人工维护 `~/.config/zs-support/known_hosts`。

每次操作都会显示当前产品、板卡地址和设备身份。`connect` 是显式选择新设备；同一设备入口自动回退会校验稳定板卡身份。连接时明确选择产品，不自动尝试其他产品账号。无交互调用使用 `connect --product l1/l2/m1`（也可在配置中固定产品）。连接仅并行读取两板身份、版本和本体 `/userdata/config/device.json` 中的 SN/tag/type；路径和 ROS 话题在进入录制/拉取时按需查询。NX 不可达时算法为“待确认”；只有明确选择“仅本体”或版本规则命中才判标准版。

M1 日志脚本默认 `/opt/runtime/bin/get_robot_log.sh`，绝对路径执行，可从任意目录启动工具。本体 `0.3.2` 及以上优先能力探测；版本未知也探测，明确更老版本使用目录。只有本次生成、唯一、非空、gzip/tar 校验通过的归档才下载；旧包不会冒充本次结果。本次新归档若由 root 生成，会只将该文件的所有权交给当前 SSH 账号，保持私有权限；不修改历史归档或日志目录权限。失败时列出原因并回退，Ctrl+C 取消不会继续回退。

## 多网卡连接

工具自动匹配机器人直连网卡，优先访问可直达的 AP 地址；`connect --interface <机器人网卡>` 可明确选择，并对后续 SSH 和 rsync 生效。网卡绑定仍依赖系统路由；VPN/代理或多个无线网卡可能使 NX 内网走错出口。连接失败时显示路由证据与建议命令，不自动修改本机路由。

连接机器人 AP 后，若需要经 AP 访问内部板卡，可在本机终端执行（替换为实际网卡名）：

```bash
ip -brief address
ip route get 192.168.168.100
sudo ip route add 192.168.168.0/24 via 192.168.234.1 dev <机器人网卡>
zs-support connect --product m1 --interface <机器人网卡>
```

若提示路由已存在，先检查已有路由与代理策略，确认后由管理员调整。通过网线直连内网时应使用该网线的实际路由。该命令通常仅对当前网络会话生效。

## 命令与脚本示例

```bash
zs-support connect --product m1 --json
zs-support connect --product zsl1 --body-only --json
zs-support connect --product zsl2 --body-host 192.168.234.1 --fast
zs-support bag --scene localization --scene perception
zs-support bag --board body --topic /odom --topic /imu \
  --remote-path '/home/robot/Downloads/现场数据' --storage sqlite3
zs-support bag --list --json                  # 查看场景/话题数量
zs-support bag --scene localization --duration 5  # 定时录制并正常停止
zs-support bag --scene navigation --background    # 显式后台；稍后 stop
zs-support pull bag --board compute --list --json
zs-support pull bag --board compute --remote-path '/opt/data/s100_bag_示例' --hash
zs-support pull map --all --output ~/Downloads/现场采集
zs-support pull log --board body
zs-support pull log --board compute
zs-support pull log --board body --remote-path /home/robot/robot_launch_log --save-path
zs-support pull all --board compute --kinds log map --json
zs-support stop --session <完整会话ID> --json
zs-support pull --session <完整会话ID> --hash --json
```

`--scene` 可重复或逗号分隔；场景按原产品文件加载，不将小狗建图/感知合组拆分。`--topic`、`--exclude-topic` 用于增减话题。Topic 配置随程序发布，位于 `src/zs_support/resources/topics/`，M1 导航 183、建图 18、定位 18 个配置话题。显示的是该产品配置与当前连接版本下的场景、类型库可加载的话题数及待发布数量；未发布的话题可以随后出现。已发现但当前环境缺少消息类型库的话题会明确列出，场景录制时跳过（例如这台 NX 的 `/nav_lite/start_cmd`）；显式 `--topic` 指定不可用类型时返回错误。录制默认智航版使用 compute、标准版使用 body，`--board` 可覆盖。

普通拉取输出仅显示保存位置、文件数量/大小、校验和必要提示；完整证据保留在采集清单与 `--json`。菜单中输入 q 正常取消，不作为错误报告。

地图始终读取算力板，忽略配置中的默认 body 板卡；算力板不可达时提示重新连接，不回退本体。组合采集中地图仍使用算力板，`--board` 指定其余录包/日志的板卡。

多个 ROS 安装并存时用 `--ros-setup /opt/ros/<发行版>/setup.bash` 明确选择，`--qos` 指向远端 QoS 文件。

录制默认在前台显示“正在录制 · 已录制 xx s”，按 **Ctrl+C** 后等待停止和保存完成，再运行 `zs-support pull`。就绪前显示启动等待，不把启动耗时算作录制时间。`--duration 5` 会在就绪后录制 5 秒并自动保存。无交互调用必须指定 `--duration` 或显式 `--background`；后台录制用 `zs-support stop` 收尾。重复 stop 没有活动任务时正常返回，失败启动不会成为下一次 stop 的目标。

`bag` 从 0.2.0 起专门用于录制，`rec` 继续作为兼容命令。`pull bag` 承担原有 Bag 浏览与下载；`pull log`、`pull map` 分别采集日志和地图。`pull` 在非交互环境仍默认拉取最近已完成会话，也可显式指定 `pull last`。主帮助与补全只展示推荐入口；`rec/log/map/collect` 仍兼容原脚本，分别对应 `bag/pull log/pull map/pull all`。

`--remote-path` 对 bag/rec 是录制父目录，对 pull bag/log/map 是准确采集路径。地图用 `--select <列表中的路径>` 可重复选择。组合采集的 Bag 会收集所有候选，地图采集首选根目录；需精细选择时使用 pull bag/pull map。

`--json` 可在子命令前后使用；stdout 只输出结果 JSON，进度/提示在 stderr。`--json`、`--non-interactive` 或非终端输入均不会等待用户选择，缺参会返回参数错误。`--verbose` 启用详细工具日志。`--help` 提供每条命令参数。

退出码：0 成功，1 通用或部分失败，2 参数错误，3 配置错误，4 网络错误，5 权限/身份错误，6 依赖缺失，7 服务不可用，8 文件不存在。JSON 中的 `reason` 与子任务结果进一步区分错误。

## 数据、恢复与校验

- 远程录包默认 `<HOME>/Downloads/zs-support/bags/`，权限不可用时尝试 `/var/tmp/zs-support/<用户>/bags/` 并提示。启动前检查目标文件系统可用空间和写权限，默认最低 1 GiB，可配置；这不是容量保证。
- 只有录制进程存活且收到 rosbag2 的就绪日志才显示“录制中”；停止后读取 SQLite 确认消息与 metadata；正常停止但没有消息时明确提示“未收到任何话题消息”，不当作有效录包，运行中不读取数据库以免与 ROS 写盘争锁。停止验证会话标识、板卡身份、boot ID、进程启动时间及进程组，只发 SIGINT；超时保持异常状态，不使用按名称 `pkill`。
- 断网或工具退出后录制可能继续，重新运行 status/stop 核验。身份或状态无法确认时不会发送停止信号。多台设备使用 `connect` 切换，旧会话仍保留。
- 下载默认系统 Downloads 下的 `zs-support/`；没有 Downloads 时使用 `~/zs-support/` 并提示。按产品、板卡身份、数据类型分目录，每个结果含 `data/` 和 `collection-manifest.json`。
- 下载中间文件位于输出根目录 `.partial/`，失败后重新执行同一采集命令会复用静态数据断点。完成后移动到结果目录。相同源清单已成功的结果会先校验本地文件，再直接复用，原结果不会被覆盖。
- 默认校验文件清单、大小和采集前后变化；`--hash` 开启 SHA256，脚本归档强制哈希。持续写入日志标注变化，不承诺严格快照。显式选择的日志/地图根路径允许解析软链接，目录内部链接与特殊文件跳过并记录；空目录不算有效数据。
- Bag 的元数据/数据文件存在性检查不等同于 ROS 可回放。地图格式未知时只报告文件层校验，格式完整性按机型配置。M1 实机 Bag info 与本地消息计数已验证；回放到机器人和地图格式语义验收未执行。
- 所有原始远程数据保留，不自动清理脚本、归档或 Bag。

配置在 `~/.config/zs-support/`；会话、路径覆盖、传输状态和滚动日志在 `~/.local/state/zs-support/`，支持 XDG 环境变量。工具日志不写密码或完整远端命令。详见 [配置说明](docs/配置说明.md)。

## Tab 补全

deb 安装后新 Bash/Zsh 终端自动加载（系统已启用对应 shell 补全）。当前 Bash 可执行：

```bash
source /usr/share/bash-completion/completions/zs-support
```

pip 安装的 Bash 会话：`source <(zs-support completion bash)`。
Zsh：先 `autoload -Uz compinit && compinit`，再 `source <(zs-support completion zsh)`。
补全覆盖子命令、参数、产品、板卡、场景、本地路径和当前设备会话 ID，仅读取本地状态，不连接设备。

## 卸载

```bash
sudo apt remove zephyr-zs-support
# pip 安装则使用：python -m pip uninstall zs-support
```

卸载注销 zt（失败不影响卸载），保留用户配置、会话与采集数据。

## 构建与发布校验

```bash
python3 scripts/build_deb.py
python3 scripts/verify_package.py
python3 -m pip wheel . --no-deps -w dist
```

Deb 构建需要 `dpkg-deb`；发布校验需要前述运行依赖及 Bash 补全。生成文件位于 `dist/`，包含 Debian 安装包和 Python wheel。
