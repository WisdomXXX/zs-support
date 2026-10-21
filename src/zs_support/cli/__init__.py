import argparse
import json
import logging
import logging.handlers
import os
import shutil
import signal
import sys
import time
from datetime import datetime
from importlib.resources import files
from pathlib import Path

from zs_support import version
from zs_support.config import load, product_key, topic_groups, config_dir
from zs_support.domain import SupportError, TaskControl
from zs_support.services.devices import Devices
from zs_support.services.recording import Recording
from zs_support.services.collection import Collection
from zs_support.storage import Store, state_dir

COMMANDS = {"connect": "选择 L1/L2/M1 并连接", "info": "刷新版本和 SN/tag/type",
            "bag": "录制 Bag，Ctrl+C 停止并保存", "status": "查看录制状态", "stop": "停止后台录制或恢复中的任务",
            "pull": "拉取录包、日志或地图", "doctor": "检查依赖与连接", "completion": "输出 Shell 补全"}
COMPAT_COMMANDS = {"rec": "bag", "log": "pull log", "map": "pull map", "collect": "pull all"}


class Parser(argparse.ArgumentParser):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._positionals.title = "参数"
        self._optionals.title = "选项"
        for action in self._actions:
            if action.dest == "help":
                action.help = "显示帮助并退出"

    def error(self, message):
        raise SupportError(message, 2, "invalid_arguments")


def parser():
    common = argparse.ArgumentParser(add_help=False, argument_default=argparse.SUPPRESS)
    common.add_argument("--json", action="store_true", help="输出机器可读 JSON，不进行交互")
    common.add_argument("--verbose", action="store_true", help="详细诊断信息")
    common.add_argument("--non-interactive", action="store_true", help="缺少选择时直接退出")
    main = Parser(prog="zs-support", description="智身科技售后运维工具 · 连接 → 录制 → 拉取", parents=[common],
                  formatter_class=argparse.RawDescriptionHelpFormatter,
                  epilog="常用操作：\n  zs-support connect       选择产品并连接\n  zs-support bag           选择场景录制，Ctrl+C 保存\n  zs-support pull          选择拉取录包、日志或地图\n  zs-support pull map      从算力板拉取地图\n\n查看命令参数：zs-support <命令> -h")
    main.add_argument("--version", action="version", version="zs-support " + version(), help="显示工具版本并退出")
    sub = main.add_subparsers(dest="command", parser_class=Parser, title="命令", metavar="<命令>")
    for name in [*COMMANDS, *COMPAT_COMMANDS]:
        description = COMMANDS.get(name) or "兼容入口，建议使用 zs-support " + COMPAT_COMMANDS[name]
        options = {"help": description} if name in COMMANDS else {}
        command = sub.add_parser(name, description=description, parents=[common], **options)
        if name == "completion":
            command.add_argument("shell", choices=("bash", "zsh"))
            continue
        if name == "doctor":
            command.add_argument("--connect", action="store_true", help="只读检查当前设备连接")
            continue
        command.add_argument("--product", help="产品 zsl1/zsl2/zsm1 或别名")
        if name == "connect":
            command.add_argument("--interface", help="指定机器人网卡，例如 wlan0；默认自动匹配")
            command.add_argument("--body-host", help="本体地址")
            command.add_argument("--compute-host", help="算力板地址")
            command.add_argument("--body-only", action="store_true", help="明确声明设备仅有本体")
            command.add_argument("--algorithm", choices=("standard", "navigation"), help="人工指定算法配置，保留原始探测结果")
            command.add_argument("--fast", action="store_true", help="选择 L2 算力板高速入口")
            continue
        if name not in ("info", "status", "stop"):
            command.add_argument("--board", choices=("body", "compute"), help="录包/日志的目标板卡；地图固定使用 compute" if name == "pull" else "目标板卡")
        if name in ("status", "stop", "pull"):
            command.add_argument("--session", help="指定本工具会话 ID")
        if name == "status":
            command.add_argument("--local", action="store_true", help="仅查看本地记录，状态可能过期")
        if name in ("rec", "pull", "bag", "log", "map", "collect"):
            command.add_argument("--remote-path", help="远程完整路径；录制时为保存根目录")
        if name in ("rec", "bag"):
            command.add_argument("--scene", action="append", help="场景，可重复或以逗号分隔；按产品提供")
            command.add_argument("--topic", action="append", default=[], help="增加 Topic，可重复")
            command.add_argument("--exclude-topic", action="append", default=[], help="移除 Topic，可重复")
            command.add_argument("--storage", help="指定设备支持的 rosbag2 插件，例如 sqlite3/mcap")
            command.add_argument("--ros-setup", help="远端 ROS setup.bash 完整路径")
            command.add_argument("--qos", help="远端 QoS 配置完整路径")
            command.add_argument("--min-free-bytes", type=int, help="启动最低剩余字节数，默认 1 GiB")
            command.add_argument("--list", action="store_true", help="显示当前版本的场景与话题数量，不开始录制")
            command.add_argument("--duration", type=float, help="定时录制秒数，到时自动停止")
            command.add_argument("--background", action="store_true", help="显式后台录制，稍后使用 stop 停止")
        if name == "status":
            command.add_argument("--all", action="store_true", help="查看全部历史录制记录")
        if name in ("pull", "log", "map", "collect"):
            command.add_argument("--output", help="本地下载根目录")
            command.add_argument("--hash", action="store_true", help="额外执行 SHA256 校验")
            command.add_argument("--save-path", action="store_true", help="成功后保存为当前板卡路径覆盖")
        if name in ("pull", "log", "collect"):
            command.add_argument("--log-script", help="M1 本体日志脚本的远端绝对路径")
        if name in ("pull", "map"):
            command.add_argument("--list", action="store_true", help="只列出候选，不下载")
            command.add_argument("--all", action="store_true", help="拉取所有候选；地图为首选根目录整体")
            command.add_argument("--select", action="append", help="选择列表中的完整路径，可重复")
        if name == "pull":
            command.add_argument("kind", nargs="?", choices=("last", "bag", "log", "map", "all"),
                                 help="拉取内容：最近录制/已有Bag/日志/地图/组合；省略时提供选择")
        if name in ("collect", "pull"):
            command.add_argument("--kinds", nargs="+", choices=("bag", "log", "map"), help="组合采集类型")
    internal = sub.add_parser("__complete")
    internal.add_argument("kind", choices=("sessions", "scenes", "products", "boards"))
    internal.add_argument("--product")
    return main


class UI:
    def __init__(self, args):
        self.json = getattr(args, "json", False)
        self.interactive = sys.stdin.isatty() and not self.json and not getattr(args, "non_interactive", False)
        self.last_progress = 0

    def choose(self, title, choices, multi=False, default=None):
        if not self.interactive:
            raise SupportError(title + "；无交互模式请提供相应参数", 2, "selection_required",
                               {"choices": [str(label) for _, label in choices]})
        if not choices:
            raise SupportError("没有可选项目", 8, "empty_selection")
        print(title, file=sys.stderr)
        for index, (_, label) in enumerate(choices, 1):
            print(f"  {index}. {label}", file=sys.stderr)
        while True:
            try:
                print("输入序号" + ("（可用逗号选择多个）" if multi else "") + "，q 返回：", end="", file=sys.stderr, flush=True)
                raw = input().strip()
                if not raw and default is not None:
                    return default
                if raw.lower() == "q":
                    raise SupportError("操作已取消", 1, "cancelled")
                numbers = [int(s.strip()) - 1 for s in raw.split(",")]
                if not numbers or any(n < 0 or n >= len(choices) for n in numbers) or (not multi and len(numbers) != 1):
                    raise ValueError
                selected = [choices[n][0] for n in dict.fromkeys(numbers)]
                return selected if multi else selected[0]
            except ValueError:
                print("请输入列表中的有效序号。", file=sys.stderr)

    def progress(self, event):
        current = time.monotonic()
        if event.phase == "transfer" and current - self.last_progress < 1:
            return
        self.last_progress = current
        if event.message:
            print(event.message, file=sys.stderr)
        else:
            percent = min(100, event.bytes_done * 100 / event.bytes_total) if event.bytes_total else 0
            print(f"{event.phase} {percent:.1f}% {event.bytes_done}/{event.bytes_total} 字节 "
                  f"{event.bytes_per_second / 1048576:.1f} MiB/s → {event.destination}", file=sys.stderr)

    def emit(self, data, code=0):
        if self.json:
            print(json.dumps({"ok": code == 0, "code": code, "data": data}, ensure_ascii=False))
        elif isinstance(data, dict) and ("destination" in data or "items" in data):
            display_collection(data)
        elif isinstance(data, dict) and "boards" in data and "algorithm" in data:
            display_connection(data)
        elif isinstance(data, dict) and "scenes" in data and "id" not in data:
            display_scenes(data)
        elif isinstance(data, dict) and "id" in data and "path" in data and "state" in data:
            print(f"录制状态：{VALUES.get(data['state'], data['state'])}")
            if "elapsed_seconds" in data:
                print(f"已录制时长：{data['elapsed_seconds']} s")
            print(f"保存位置：{data['path']}")
            print(f"会话：{data['id'][:8]} · {data['board']} · {len(data.get('topics', []))} 个话题")
            if data['state'] not in ('completed', 'recording'):
                reason = data.get('error', {}).get('message') or data.get('remote', {}).get('reason')
                if reason:
                    print("说明：" + reason)
                if data.get('remote', {}).get('log_tail'):
                    print("录制日志：" + data['remote']['log_tail'][-1500:])
            print(data.get("next_step", "下一步：zs-support pull" if data['state'] == 'completed'
                           else "请用 zs-support status 核验；需要时使用 zs-support stop。"))
        elif isinstance(data, dict) and "sessions" in data:
            if not data["sessions"]:
                print("当前设备没有录制记录。")
            for session in data["sessions"]:
                print(f"{VALUES.get(session['state'], session['state'])} · {session['board']} · "
                      f"{', '.join(session.get('scenes', [])) or '自定义'} · {session['id'][:8]}")
                print("  " + session["path"])
        else:
            display(data)


def format_bytes(value):
    size = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024


def display_collection(data, indent=0):
    """Customer summary; manifests and JSON retain complete diagnostic evidence."""
    prefix = " " * indent
    if "destination" in data:
        kind = {"bag": "录包", "log": "日志", "map": "地图"}.get(data.get("kind"), "数据")
        print(prefix + kind + ("拉取完成（部分失败）" if data.get("partial_failure") else "拉取完成"))
        print(prefix + "保存位置：" + data["destination"])
        print(prefix + f"文件：{len(data.get('files', []))} 个 · {format_bytes(data.get('bytes', 0))}")
        if data.get("verified"):
            print(prefix + "校验：" + ("SHA256 通过" if data.get("hashes") else "文件清单与大小通过"))
        else:
            print(prefix + "校验：未完全通过，请查看采集清单。")
        if data.get("changed_during_collection"):
            print(prefix + "提示：采集期间源文件有更新，本次日志不是完整快照。")
        if data.get("incomplete"):
            print(prefix + "提示：此录包不完整，请检查数据和元数据。")
        if data.get("kind") == "map":
            print(prefix + "提示：已完成文件校验，地图格式完整性仍以实际使用验证为准。")
        if data.get("reused"):
            print(prefix + "已校验并复用本地已有文件。")
        if data.get("script_error"):
            print(prefix + "提示：日志脚本未成功，已回退目录采集。")
        if data.get("skipped"):
            print(prefix + f"提示：跳过 {len(data['skipped'])} 项链接或特殊文件，详见采集清单。")
        for extra in data.get("additional_logs", []):
            if "destination" in extra:
                display_collection(extra, indent + 2)
            elif extra.get("error"):
                print(prefix + "附加日志：" + extra.get("source", "") + " · " + extra["error"]["message"])
        print(prefix + "采集清单：" + str(Path(data["destination"]) / "collection-manifest.json"))
    elif "roots" in data:
        print(prefix + f"找到 {len(data['items'])} 项可拉取数据：")
        for item in data["items"]:
            note = "（不完整）" if item.get("complete") is False else ""
            print(prefix + "  " + item["path"] + note)
        if not data["items"]:
            print(prefix + "  未发现单独的数据项。")
        for root in data["roots"]:
            print(prefix + "可用根目录：" + root["path"])
    elif "items" in data:
        for index, item in enumerate(data["items"], 1):
            print(prefix + f"{index}.")
            display_collection(item, indent + 2)
    else:
        kind = {"bag": "录包", "log": "日志", "map": "地图"}.get(data.get("kind"), "数据")
        print(prefix + kind + "未完成：" + data.get("error", {}).get("message", "未获取到可用数据"))
        if data.get("source"):
            print(prefix + "来源：" + data["source"])
    for error in data.get("errors", []) + data.get("scan_errors", []):
        print(prefix + "扫描未完成：" + error.get("path", "") + " · " + error["error"]["message"])


def display_scenes(data):
    print(f"录制板卡：{data['board']} · 算法：{VALUES.get(data['algorithm']['type'], '待确认')}")
    primary = {"navigation", "perception", "localization", "slam"}
    for scene in data.get("scenes", []):
        if scene["key"] not in primary:
            continue
        print(f"  {scene['name']}（{scene['key']}）：配置 {scene['topic_count']} 个话题，"
              f"当前可录 {scene.get('recordable_count', scene['visible_count'])} 个，待发布 {scene['missing_count']} 个，"
              f"类型不可用 {scene.get('unsupported_count', 0)} 个")
    if data.get("error"):
        print("录制能力探测失败：" + data["error"]["message"])
    groups = topic_groups(data["product"]) if data.get("product") else {}
    primary_topics = {topic for key, group in groups.items() if key in primary for topic in group["topics"]}
    for topic, info in data.get("unsupported_topics", {}).items():
        if topic not in primary_topics:
            continue
        print(f"  类型库不可用：{topic}（{', '.join(info['types'])}），场景录制时将跳过")


def display_connection(data):
    label = {"zsl1": "L1/1W", "zsl2": "L2", "zsm1": "M1"}.get(data["product"], data["product"])
    print(f"已连接 {label} · {VALUES.get(data['algorithm']['type'], '待确认')}")
    for board, info in data["boards"].items():
        label = "3588 本体" if board == "body" else "NX/算力板"
        if info["status"] != "connected":
            print(f"{label}：{VALUES.get(info['status'], info['status'])}")
            attempts = info.get("error", {}).get("details", {}).get("attempts", [])
            for attempt in attempts:
                details = attempt.get("error", {}).get("details", {})
                if details.get("route_command"):
                    print("  请检查机器人内网路由；需要时在本机执行：" + details["route_command"])
                    break
            continue
        print(f"{label}：{info['host']} · 版本 {info['version'].get('value') or '待确认'}")
        if info.get("product_versions"):
            print("  产品版本：" + ", ".join(info["product_versions"]))
    device = data.get("device", {})
    for key in ("sn", "tag", "type"):
        print(f"{key.upper() if key == 'sn' else key}：{device.get(key) or '未读取到'}")
    print("下一步：zs-support bag 录制 · zs-support pull 拉取")


def connection_details(context, devices, recording, config):
    # Kept as a small compatibility hook; connection never scans paths or ROS.
    return context


LABELS = {"product": "产品", "product_source": "产品来源", "device_id": "设备身份", "boards": "板卡",
          "body": "本体", "compute": "算力板", "algorithm": "算法", "type": "类型", "detected": "自动探测结果",
          "source": "来源", "reason": "说明", "version": "版本证据", "value": "原值", "status": "状态",
          "state": "状态", "path": "远程路径", "destination": "保存位置", "id": "标识", "host": "地址",
          "sessions": "录制会话", "transfers": "传输任务", "items": "项目", "error": "错误", "message": "说明",
          "hardware": "硬件", "product_versions": "产品版本", "method": "日志方式", "script_error": "脚本失败原因"}
VALUES = {"navigation": "智航版", "standard": "标准版", "unknown": "待确认", "connected": "已连接",
          "unavailable": "不可达", "recording": "录制中", "starting": "启动中（尚未确认产物）",
          "completed": "已完成", "abnormal": "异常", "stop_timeout": "停止超时（仍需核验）",
          "directory_fallback": "目录回退", "directory": "目录采集", "script": "脚本归档"}
VALUES["failed"] = "未启动/启动失败"


def finish_recording(recording, sid, ui):
    print("\n正在停止录制并保存 Bag，请稍候…", file=sys.stderr, flush=True)
    previous = signal.signal(signal.SIGINT, signal.SIG_IGN)
    try:
        session = recording.refresh(sid, stop=True)
    finally:
        signal.signal(signal.SIGINT, previous)
    if session["state"] == "completed":
        session["next_step"] = "已保存完成。下一步：zs-support pull"
    elif session["state"] == "abnormal":
        session["next_step"] = "请检查所选话题后重新录制；需要原始文件时可用 zs-support pull bag 指定路径导出。"
    return session


def watch_recording(recording, session, args, ui):
    started = time.monotonic() if session["state"] == "recording" else None
    last_check = time.monotonic()
    print("\n前台录制：按 Ctrl+C 正常停止并保存。", file=sys.stderr)
    try:
        while True:
            elapsed = int(time.monotonic() - started) if started is not None else 0
            label = f"正在录制 · 已录制 {elapsed} s" if started is not None else "正在等待录制就绪…"
            print(("\r" if sys.stderr.isatty() else "") + label + "    ", file=sys.stderr, flush=True)
            if started is not None and args.duration is not None and time.monotonic() - started >= args.duration:
                break
            if time.monotonic() - last_check >= 5:
                session = recording.refresh(session["id"])
                last_check = time.monotonic()
                if session["state"] not in ("starting", "recording"):
                    session["elapsed_seconds"] = elapsed
                    return session, 0 if session["state"] == "completed" else 7
                if started is None and session["state"] == "recording":
                    started = time.monotonic()
            time.sleep(.5 if args.duration is not None and args.duration < 1 else 1)
    except KeyboardInterrupt:
        pass
    elapsed = int(time.monotonic() - started) if started is not None else 0
    session = finish_recording(recording, session["id"], ui)
    session["elapsed_seconds"] = elapsed
    return session, 0 if session["state"] == "completed" else 7


def display(data, indent=0):
    prefix = " " * indent
    if isinstance(data, dict):
        for key, value in data.items():
            if value is None or value == [] or value == {}:
                continue
            if key in ("files", "topics", "missing_topics") and isinstance(value, list):
                print(prefix + {"files": "文件", "topics": "Topic", "missing_topics": "当前缺失 Topic"}[key] + f"：{len(value)} 项")
                continue
            if key in ("source_manifest", "preflight"):
                continue
            if key == "remote":
                if isinstance(value, dict) and value.get("log_tail"):
                    print(prefix + "远端录制日志：" + value["log_tail"])
                continue
            if key.endswith("_at") and isinstance(value, str):
                try:
                    value = datetime.fromisoformat(value).astimezone().isoformat(timespec="seconds")
                except ValueError:
                    pass
            if isinstance(value, (dict, list)):
                print(prefix + LABELS.get(key, key) + "：")
                display(value, indent + 2)
            else:
                print(prefix + LABELS.get(key, key) + "：" + str(VALUES.get(str(value), value)))
    elif isinstance(data, list):
        for value in data:
            display(value, indent)
            if isinstance(value, dict):
                print()
    else:
        print(prefix + str(data))


def pick_board(args, cfg, devices, ui):
    board = getattr(args, "board", None) or cfg.get("board")
    if board:
        return board
    context = devices.current()
    if args.command in ("bag", "rec"):
        preferred = "compute" if context["algorithm"]["type"] == "navigation" else "body"
        if context["boards"].get(preferred, {}).get("status") == "connected":
            return preferred
    available = [(key, ("本体" if key == "body" else "算力板") + " · " + value["host"])
                 for key, value in context["boards"].items() if value["status"] == "connected"]
    if len(available) == 1:
        return available[0][0]
    return ui.choose("请选择板卡（--board body/compute）", available)


def map_board(devices):
    if devices.current()["boards"].get("compute", {}).get("status") != "connected":
        raise SupportError("地图位于算力板；当前算力板未连接，请先 connect 检查连接", 4, "board_unavailable")
    return "compute"


def pick_session(args, devices, store, ui, operation):
    if args.session:
        return args.session
    sessions = store.sessions(devices.current()["device_id"])
    if operation == "pull":
        completed = [s for s in sessions if s["state"] == "completed"]
        if completed:
            # The plan explicitly defines pull as the latest completed *local session*.
            return completed[0]["id"]
    else:
        sessions = [s for s in sessions if s["state"] in ("starting", "recording", "unknown", "stop_timeout")]
        if len(sessions) == 1:
            return sessions[0]["id"]
    return ui.choose("请选择会话（--session ID）", [(s["id"], f"{s['id']} · {s['board']} · {s['started_at']} · {s['state']}") for s in sessions])


def path_retry(action, ui, args):
    while True:
        try:
            return action()
        except SupportError as exc:
            if exc.reason not in ("paths_missing", "missing", "empty_directory", "disk_full", "invalid_path", "incomplete_map") or not ui.interactive:
                raise
            print(str(exc), file=sys.stderr)
            print(json.dumps(exc.details, ensure_ascii=False, indent=2), file=sys.stderr)
            choice = ui.choose("处理方式", [("path", "输入完整路径"), ("retry", "重试"), ("back", "返回")])
            if choice == "back":
                raise SupportError("操作已取消", 1, "cancelled")
            if choice == "path":
                print("远端完整路径：", end="", file=sys.stderr, flush=True)
                args.remote_path = input().strip()


def execute(args, cfg, store, ui):
    devices = Devices(cfg, store)
    control = TaskControl(ui.progress)
    recording = Recording(devices, store, cfg, control)
    collection = Collection(devices, store, cfg, control)
    command = args.command
    if command == "doctor":
        checks = [{"name": name, "ok": bool(shutil.which(name)), "path": shutil.which(name)} for name in ("python3", "ssh", "sshpass", "rsync", "bash")]
        checks.append({"name": "config", "ok": True, "path": str(config_dir())})
        if args.connect:
            context = devices.current()
            for board, value in context["boards"].items():
                if value.get("status") == "unavailable":
                    checks.append({"name": board, "ok": False, "error": value.get("error", {})})
                if value.get("status") == "connected":
                    try:
                        devices.probe_board(context["product"], board, value["host"], value["id"])
                        checks.append({"name": board, "ok": True})
                    except SupportError as exc:
                        checks.append({"name": board, "ok": False, "error": exc.to_dict()})
        failures = [check for check in checks if not check["ok"]]
        code = failures[0].get("error", {}).get("code", 6) if failures else 0
        return {"checks": checks, "note": "仅诊断；客户端无需安装 ROS，设备需 Python 3.8+、Bash 和 ROS 2"}, code
    if command == "connect":
        if args.interface:
            cfg["interface"] = args.interface
        if args.product:
            product = args.product
        elif ui.interactive:
            product = ui.choose("请选择连接产品", [("zsl1", "L1 / L1W"), ("zsl2", "L2"), ("zsm1", "M1")])
        else:
            product = cfg.get("product")
            if not product:
                raise SupportError("请选择产品；无交互环境使用 connect --product l1/l2/m1", 2, "selection_required")
        if args.body_only and (args.compute_host or args.fast):
            raise SupportError("仅本体配置不能同时指定算力板地址或高速入口", 2, "invalid_arguments")
        hosts = {"body": args.body_host, "compute": args.compute_host}
        target = cfg.get("board", "body")
        hosts[target] = hosts[target] or cfg.get("host")
        print("正在连接本体和算力板…", file=sys.stderr)
        context = devices.connect(product, hosts, args.body_only, args.algorithm, args.fast)
        context["product_source"] = "用户选择" if ui.interactive or args.product else "配置"
        store.write("context.json", context)
        return connection_details(context, devices, recording, cfg), 0
    context = devices.current()
    requested_product = getattr(args, "product", None) or cfg.get("product")
    if requested_product and product_key(requested_product, cfg) != context["product"]:
        raise SupportError("指定产品与当前设备不同，请先 connect 选择设备", 3, "context_mismatch")
    print(f"当前设备：{context['product']} · {context['device_id'][:12]} · "
          + " / ".join(f"{b}={v.get('host', v['status'])}" for b, v in context["boards"].items()), file=sys.stderr)
    if command == "info":
        refreshed = devices.connect(context["product"], body_only=context.get("body_only", False), algorithm=context.get("algorithm_override"), refresh=True)
        return connection_details(refreshed, devices, recording, cfg), 0
    if command in ("rec", "bag"):
        board = pick_board(args, cfg, devices, ui)
        if args.duration is not None and (not 0 < args.duration <= 86400):
            raise SupportError("--duration 必须在 0～86400 秒之间", 2, "invalid_arguments")
        if args.duration is not None and args.background:
            raise SupportError("--duration 定时录制与 --background 后台录制请选择一种", 2, "invalid_arguments")
        if not args.list and not args.background and args.duration is None and not ui.interactive:
            raise SupportError("无交互录制请指定 --duration 秒数，或显式 --background 后台录制", 2, "selection_required")
        if not args.list:
            active = recording.active()
            if active:
                raise SupportError("当前设备已有录制任务；请先 zs-support stop，再开始新的录制", 7, "already_recording",
                                   {"sessions": [s["id"] for s in active]})
        preview = recording.preview(board)
        if args.list:
            return preview, 0
        if not ui.json:
            display_scenes(preview)
        groups = topic_groups(context["product"])
        scenes = [part for item in (args.scene or []) for part in item.split(",")]
        if not scenes and not args.topic:
            choices = [(s["key"], f"{s['name']} · {s['topic_count']} 个话题（当前可录 {s.get('recordable_count', s['visible_count'])} 个）") for s in preview["scenes"] if s["key"] in ("navigation", "perception", "localization", "slam")]
            additional = [(s["key"], s["name"] + f" · {s['topic_count']} 个话题") for s in preview["scenes"] if s["key"] not in ("navigation", "perception", "localization", "slam")]
            scenes = ui.choose("选择录包场景（--scene，可多选）", choices + ([("more", "其他场景（回充/相机/底盘等）")] if additional else []), True)
            if "more" in scenes:
                scenes.remove("more")
                scenes += ui.choose("其他录包场景", additional, True)
        try:
            session = path_retry(lambda: recording.start(board, scenes, args.topic, args.exclude_topic,
                                                         args.remote_path, args.storage, args.qos), ui, args)
        except KeyboardInterrupt:
            if recording.pending_session:
                session = finish_recording(recording, recording.pending_session, ui)
                return session, 0 if session["state"] == "completed" else 7
            return {"message": "已取消录制启动。"}, 0
        if args.background:
            session["next_step"] = "正在后台录制。使用 zs-support status 查看，zs-support stop 停止。"
            return session, 0
        return watch_recording(recording, session, args, ui)
    if command == "status":
        sessions = [recording.get(args.session)] if args.session else store.sessions(context["device_id"])
        if not args.session and not args.all:
            active = [s for s in sessions if s["state"] in ("starting", "recording", "unknown", "stop_timeout")]
            sessions = active or sessions[:1]
        code = 0
        if not args.local:
            for i, session in enumerate(sessions):
                try:
                    sessions[i] = recording.refresh(session["id"])
                except SupportError as exc:
                    sessions[i] = recording.get(session["id"])
                    code = exc.code
        transfers = [store.read(str(path.relative_to(store.root))) for path in (store.root / "transfers").glob("*.json")]
        board_ids = {v.get("id") for v in context["boards"].values()}
        return {"sessions": sessions, "transfers": [t for t in transfers if t.get("board_id") in board_ids],
                "local_only": args.local}, code
    if command == "pull":
        kind = args.kind or ("last" if args.session or not ui.interactive else ui.choose("请选择拉取内容", [
            ("last", "本工具最近一次已完成录包"), ("bag", "机器人已有 Bag"), ("log", "日志（本体/NX）"),
            ("map", "地图"), ("all", "组合采集")]))
        command = {"last": "pull", "bag": "history", "log": "log", "map": "map", "all": "collect"}[kind]
    if command in ("stop", "pull"):
        if command == "stop":
            if args.session:
                sid = args.session
            else:
                active = recording.active()
                if not active:
                    return {"message": "当前没有正在录制的任务；已停止的录包可用 zs-support pull 拉取。"}, 0
                sid = active[0]["id"] if len(active) == 1 else ui.choose("选择要停止的录制", [
                    (s["id"], f"{s['board']} · {', '.join(s.get('scenes', []))} · {s['id'][:8]}") for s in active])
            session = finish_recording(recording, sid, ui)
            if session["state"] == "failed":
                return {"message": "该次启动没有产生录制任务，无需停止。", "session": sid}, 0
            return session, 0 if session["state"] == "completed" else 7
        sid = pick_session(args, devices, store, ui, command)
        session = recording.refresh(sid)
        if session["state"] in ("recording", "starting", "stop_timeout", "unknown"):
            raise SupportError("录制尚未确认结束，请先 stop/status；异常数据可用 pull bag --remote-path 导出", 7, "session_not_ready")
        if args.remote_path:
            raise SupportError("本次录制使用会话精确路径；手动路径请使用 pull bag --remote-path", 2, "invalid_arguments")
        result = collection.pull(session["board"], "bag", session["path"], args.output, args.hash)
        result["session"] = sid
        result["incomplete"] = session["state"] != "completed"
        return result, 0
    if command == "map":
        if args.board and args.board != "compute":
            raise SupportError("地图固定从算力板拉取，请移除 --board body", 2, "invalid_arguments")
        board = map_board(devices)
    elif command != "collect":
        board = pick_board(args, cfg, devices, ui)
    if command == "log":
        result = path_retry(lambda: collection.logs(board, args.remote_path, args.output, args.hash, args.save_path), ui, args)
        return result, 1 if result.get("partial_failure") else 0
    if command in ("history", "map"):
        kind = "bag" if command == "history" else "map"
        if args.remote_path and not args.list and not args.select and not args.all:
            return path_retry(lambda: collection.pull(board, kind, args.remote_path, args.output, args.hash, args.save_path), ui, args), 0
        listing = path_retry(lambda: collection.list(board, kind, args.remote_path), ui, args)
        if args.list:
            return listing, 1 if listing["errors"] else 0
        paths = args.select
        if paths:
            known = {item["path"] for item in listing["items"]} | {item["path"] for item in listing["roots"]}
            if any(path not in known for path in paths):
                raise SupportError("--select 路径不在当前列表中，请刷新 --list", 2, "invalid_selection")
        elif args.all:
            paths = [listing["roots"][0]["path"]] if command == "map" else [item["path"] for item in listing["items"]]
        else:
            choices = [(item["path"], item.get("name", item["path"]) + " · " + item["path"]
                        + ("（不完整）" if item.get("complete") is False else "")) for item in listing["items"]]
            if command == "map":
                choices += [(root["path"], "整个地图根目录 · " + root["path"]) for root in listing["roots"]]
            paths = ui.choose("选择拉取内容（--select/--all/--remote-path）", choices, True)
        if not paths:
            raise SupportError("未发现可采集的数据", 8, "empty_selection")
        items = []
        for path in paths:
            try:
                items.append(collection.pull(board, kind, path, args.output, args.hash, args.save_path))
            except SupportError as exc:
                if exc.reason == "cancelled":
                    raise
                items.append({"source": path, "state": "failed", "error": exc.to_dict()})
        return {"items": items, "scan_errors": listing["errors"]}, 1 if listing["errors"] or any(i["state"] == "failed" for i in items) else 0
    if command == "collect":
        kinds = args.kinds or ui.choose("组合采集内容（--kinds）", [("bag", "已有 Bag"), ("log", "日志"), ("map", "地图根目录")], True)
        if args.remote_path and len(kinds) > 1:
            raise SupportError("组合采集多个类型时不能共用 --remote-path，请分别指定采集命令", 2, "invalid_arguments")
        board = pick_board(args, cfg, devices, ui) if any(k != "map" for k in kinds) else None
        results = []
        for kind in dict.fromkeys(kinds):
            try:
                target_board = map_board(devices) if kind == "map" else board
                if kind == "log":
                    result = path_retry(lambda: collection.logs(board, args.remote_path, args.output, args.hash, args.save_path), ui, args)
                    if result.get("partial_failure"):
                        result["state"] = "partial_failure"
                elif kind == "bag" and not args.remote_path:
                    listing = collection.list(board, "bag")
                    if not listing["items"]:
                        raise SupportError("没有找到 Bag", 8, "empty_selection")
                    bag_results = []
                    for item in listing["items"]:
                        try:
                            bag_results.append(collection.pull(board, "bag", item["path"], args.output, args.hash))
                        except SupportError as exc:
                            if exc.reason == "cancelled":
                                raise
                            bag_results.append({"state": "failed", "source": item["path"], "error": exc.to_dict()})
                    result = {"kind": "bag", "state": "partial_failure" if listing["errors"] or any(i["state"] == "failed" for i in bag_results) else "completed", "items": bag_results, "scan_errors": listing["errors"]}
                else:
                    result = path_retry(lambda: collection.pull(target_board, kind, args.remote_path, args.output, args.hash, args.save_path), ui, args)
                results.append(result)
            except SupportError as exc:
                if exc.reason == "cancelled":
                    raise
                results.append({"kind": kind, "state": "missing" if exc.code == 8 else "failed", "error": exc.to_dict()})
        return {"items": results}, 0 if all(r["state"] == "completed" for r in results) else 1
    raise SupportError("未知命令", 2, "invalid_arguments")


def complete(args, store):
    if args.kind == "sessions":
        current = store.read("context.json", {})
        return [s["id"] for s in store.sessions(current.get("device_id"))] if current else []
    if args.kind == "products":
        return ["zsl1", "zsl2", "zsm1"]
    if args.kind == "boards":
        return ["body", "compute"]
    cfg = load()
    context = store.read("context.json", {})
    product = args.product or context.get("product") or cfg.get("product")
    keys = [product_key(product, cfg)] if product else list(cfg["products"])
    return sorted({key for product in keys for key in topic_groups(product)})


def setup_logging(verbose=False):
    root = state_dir()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    logger = logging.getLogger("zs_support")
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    if not logger.handlers:
        handler = logging.handlers.RotatingFileHandler(root / "tool.log", maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(handler)
    return logger


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    json_mode = "--json" in argv
    try:
        command_parser = parser()
        args = command_parser.parse_args(argv)
        if not args.command:
            if json_mode:
                raise SupportError("请指定子命令，使用 zs-support -h 查看", 2, "selection_required")
            command_parser.print_help()
            return 0
        store = Store()
        if args.command == "completion":
            print(files("zs_support").joinpath("resources/completions/" + args.shell).read_text(), end="")
            return 0
        if args.command == "__complete":
            print("\n".join(complete(args, store)))
            return 0
        ui = UI(args)
        logger = setup_logging(getattr(args, "verbose", False))
        cfg = load({key: getattr(args, key, None) for key in ("min_free_bytes", "ros_setup", "log_script", "output")})
        logger.info("command=%s started", args.command)
        logger.debug("product=%s board=%s session=%s", getattr(args, "product", None),
                     getattr(args, "board", None), getattr(args, "session", None))
        data, code = execute(args, cfg, store, ui)
        logger.info("command=%s exit=%s", args.command, code)
        ui.emit(data, code)
        return code
    except SupportError as exc:
        logging.getLogger("zs_support").warning("reason=%s code=%s", exc.reason, exc.code)
        if exc.reason == "cancelled" and not json_mode:
            print("已取消。", file=sys.stderr)
            return 0
        if json_mode:
            print(json.dumps({"ok": False, "code": exc.code, "error": exc.to_dict()}, ensure_ascii=False))
        else:
            print("错误：" + str(exc), file=sys.stderr)
            if exc.details:
                print(json.dumps(exc.details, ensure_ascii=False, indent=2), file=sys.stderr)
        return exc.code
    except (KeyboardInterrupt, EOFError):
        if json_mode:
            print(json.dumps({"ok": False, "code": 1, "error": {"reason": "cancelled", "message": "操作已取消"}}, ensure_ascii=False))
        else:
            print("操作已取消。", file=sys.stderr)
        return 1
    except OSError as exc:
        code = 5 if isinstance(exc, PermissionError) else 1
        error = {"reason": "local_io_error", "message": str(exc), "code": code}
        if json_mode:
            print(json.dumps({"ok": False, "code": code, "error": error}, ensure_ascii=False))
        else:
            print("本地文件操作失败：" + str(exc), file=sys.stderr)
        return code
