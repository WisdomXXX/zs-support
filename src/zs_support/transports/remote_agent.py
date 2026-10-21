"""Remote Linux helper, sent over SSH; depends only on Python's standard library.

Its stdout is a JSON protocol. Never log requests (sudo secrets travel on stdin).
"""
import base64
import fcntl
import glob
import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path


class Fault(Exception):
    def __init__(self, message, code=1, reason="remote_error", details=None):
        super().__init__(message)
        self.code, self.reason, self.details = code, reason, details or {}


def write_json(path, data):
    path = Path(path)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".write-")
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(data, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, str(path))
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def read_json(path):
    return json.loads(Path(path).read_text())


def path_value(value):
    if "\x00" in value or "\n" in value or "\r" in value:
        raise Fault("路径不能含换行或 NUL", 2, "invalid_path")
    path = Path(os.path.expanduser(value))
    if not path.is_absolute():
        raise Fault("请提供远端完整绝对路径", 2, "invalid_path")
    return path


def inspect_path(value):
    path = path_value(value)
    requested = str(path)
    linked = path.is_symlink()
    try:
        stat = path.stat()
    except FileNotFoundError:
        return {"path": str(path), "status": "missing"}
    except PermissionError:
        return {"path": str(path), "status": "permission_denied"}
    if linked:
        path = path.resolve()
    if not os.access(str(path), os.R_OK | (os.X_OK if path.is_dir() else 0)):
        return {"path": str(path), "status": "permission_denied"}
    if not path.is_file() and not path.is_dir():
        return {"path": str(path), "status": "unsupported"}
    return {"path": str(path), "requested_path": requested, "symlink": linked,
            "status": "ok", "is_dir": path.is_dir(), "size": stat.st_size}


def require_path(value):
    info = inspect_path(value)
    if info["status"] != "ok":
        code = 8 if info["status"] == "missing" else (5 if info["status"] == "permission_denied" else 2)
        raise Fault("远程路径不可用：" + value, code, info["status"], info)
    return info


def digest(path):
    checksum = hashlib.sha256()
    with open(str(path), "rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            checksum.update(block)
    return checksum.hexdigest()


def manifest(value, hashes=False):
    info = require_path(value)
    root = Path(info["path"])
    entries, skipped = [], []
    def add(path):
        rel = str(path.relative_to(root)) if info["is_dir"] else path.name
        if path.is_symlink() or not path.is_file():
            skipped.append(rel)
            return
        stat = path.stat()
        entry = {"name": rel, "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}
        if hashes:
            entry["sha256"] = digest(path)
        entries.append(entry)
        if len(entries) > 200000:
            raise Fault("目录文件超过扫描上限，请缩小采集路径", 2, "scan_limit")
    if info["is_dir"]:
        def fail(error):
            raise error
        for current, dirs, names in os.walk(str(root), followlinks=False, onerror=fail):
            for name in list(dirs):
                if Path(current, name).is_symlink():
                    skipped.append(str(Path(current, name).relative_to(root)))
                    dirs.remove(name)
            for name in names:
                add(Path(current, name))
    else:
        add(root)
    return {**info, "files": sorted(entries, key=lambda e: e["name"]), "skipped": skipped,
            "bytes": sum(e["size"] for e in entries), "captured_at": time.time()}


def identity():
    values = []
    for file in ("/etc/machine-id", "/sys/firmware/devicetree/base/serial-number"):
        try:
            value = Path(file).read_text().strip().strip("\x00")
            if value:
                values.append(value)
        except OSError:
            pass
    if not values:
        raise Fault("无法读取稳定板卡身份，拒绝将 IP 作为设备身份", 7, "identity_unavailable")
    hardware = "unknown"
    try:
        model = Path("/proc/device-tree/model").read_text().strip("\x00")
        if "orin" in model.lower() or "xavier" in model.lower():
            hardware = "NX (" + model + ")"
        elif "3588" in model:
            hardware = "RK3588"
        elif "s100" in model.lower():
            hardware = "S100"
    except OSError:
        model = ""
    return {"id": hashlib.sha256("|".join(values).encode()).hexdigest(),
            "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
            "home": str(Path.home()), "hardware": hardware, "hardware_evidence": model}


def probe(request):
    data = identity()
    version_path = request.get("version_path", "/opt/release/version.yaml")
    try:
        with open(version_path) as stream:
            data["version_text"] = stream.read(1024 * 1024)
        data["version_status"] = "ok"
    except FileNotFoundError:
        data["version_status"] = "missing"
    except PermissionError:
        data["version_status"] = "permission_denied"
    if request.get("device_path"):
        source = request["device_path"]
        device = {"source": source, "status": "ok"}
        try:
            with open(source) as stream:
                content = json.loads(stream.read(65536))
            if not isinstance(content, dict):
                raise ValueError("device.json 顶层必须为对象")
            device.update({key: content.get(key, "") if isinstance(content.get(key, ""), str) else ""
                           for key in ("sn", "tag", "type")})
        except FileNotFoundError:
            device["status"] = "missing"
        except PermissionError:
            device["status"] = "permission_denied"
        except (ValueError, UnicodeError):
            device["status"] = "invalid"
        data["device"] = device
    if request.get("release"):
        try:
            data["release_names"] = os.listdir("/etc/release")
        except OSError as exc:
            data["release_error"] = str(exc)
    return data


def scan(request):
    info = require_path(request["path"])
    root = Path(info["path"])
    if not info["is_dir"]:
        return {"items": [info]}
    kind = request["kind"]
    items = []
    if kind == "map":
        parent = root / "history_map" if (root / "history_map").is_dir() else root
        for path in sorted(parent.iterdir()):
            if path.is_dir() and not path.is_symlink():
                items.append({"path": str(path), "name": path.name, "complete": None})
        return {"items": items, "root": str(root), "integrity": "地图格式规则待实机确认"}
    max_depth = min(request.get("depth", 3), 8)
    visited = 0
    def fail(error):
        raise error
    for current, dirs, names in os.walk(str(root), followlinks=False, onerror=fail):
        dirs[:] = [d for d in dirs if not d.startswith(".") and not Path(current, d).is_symlink()]
        visited += 1
        if visited > 10000:
            raise Fault("扫描超过 10000 个目录，请缩小 --remote-path", 2, "scan_limit")
        data_files = [n for n in names if n.endswith((".db3", ".mcap"))]
        if data_files or "metadata.yaml" in names:
            items.append({"path": current, "name": Path(current).name,
                          "complete": bool(data_files and "metadata.yaml" in names)})
            dirs[:] = []
        if len(Path(current).relative_to(root).parts) >= max_depth:
            dirs[:] = []
    return {"items": items}


def ros_environment(request):
    runtime = request.get("runtime_env", "/opt/runtime/env.bash")
    runtime = str(path_value(runtime)) if runtime and Path(runtime).is_file() else None
    setups = request.get("ros_setups", [])
    if not setups:
        setups = sorted(glob.glob("/opt/ros/*/setup.bash"))
        if len(setups) > 1:
            raise Fault("设备有多个 ROS 环境，请用 --ros-setup 指定", 3, "ambiguous_ros", {"paths": setups})
    for setup in setups:
        require_path(setup)
    overlays = [p for p in request.get("ros_overlays", []) if Path(p).is_file()]
    scripts = setups + ([runtime] if runtime else []) + overlays
    prefix = "; ".join("source " + shlex.quote(p) + " >/dev/null || exit 7" for p in scripts)
    prefix = prefix + "; " if prefix else ""
    def run(command):
        return subprocess.run(["bash", "-c", prefix + command], stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, universal_newlines=True, timeout=25)
    help_result = run("ros2 bag record --help")
    if help_result.returncode:
        raise Fault("ROS 录制环境不可用，请指定有效的 --ros-setup", 7, "ros_unavailable",
                    {"stderr": help_result.stderr[-1000:], "setups": setups})
    plugins = run("ros2 bag list storage")
    topics = run("ros2 topic list --no-daemon -t")
    if topics.returncode and ("unrecognized arguments" in topics.stderr or "No executable" in topics.stderr):
        topics = run("ros2 topic list")
    if topics.returncode:
        raise Fault("ROS Topic 探测失败", 7, "topics_unavailable", {"stderr": topics.stderr[-1000:]})
    topic_types = {}
    visible_topics = []
    for line in topics.stdout.splitlines():
        match = re.fullmatch(r"(\S+)\s+\[(.+)\]", line.strip())
        if match:
            topic_types[match.group(1)] = [item.strip() for item in match.group(2).split(",")]
            visible_topics.append(match.group(1))
        elif line.startswith("/"):
            visible_topics.append(line.strip())
    type_probe = r'''
import ctypes, json, os, sys
from ament_index_python.packages import get_package_prefix
bad = {}
cache = {}
for topic, types in json.loads(sys.argv[1]).items():
    if len(types) != 1:
        bad[topic] = {'types': types, 'reason': '同一话题存在多种消息类型'}
        continue
    msg = types[0]
    if msg not in cache:
        try:
            package, category, name = msg.split('/')
            library = ctypes.CDLL(os.path.join(get_package_prefix(package), 'lib', 'lib' + package + '__rosidl_typesupport_cpp.so'))
            getattr(library, 'rosidl_typesupport_cpp__get_message_type_support_handle__' + package + '__' + category + '__' + name)
            cache[msg] = None
        except Exception as exc:
            cache[msg] = type(exc).__name__ + ': ' + str(exc)
    if cache[msg]:
        bad[topic] = {'types': types, 'reason': cache[msg]}
print(json.dumps(bad))
'''
    unsupported = {}
    if topic_types:
        checked = run("python3 -c " + shlex.quote(type_probe) + " " + shlex.quote(json.dumps(topic_types)))
        if checked.returncode:
            raise Fault("消息类型库探测失败，无法确认当前可录话题", 7, "typesupport_probe_failed", {"stderr": checked.stderr[-1000:]})
        unsupported = json.loads(checked.stdout)
    environment = run("python3 -c " + shlex.quote("import os,json;print(json.dumps({k:v for k,v in os.environ.items() if k in ('ROS_DOMAIN_ID','RMW_IMPLEMENTATION','ROS_DISTRO')}))"))
    try:
        ros_values = json.loads(environment.stdout)
    except ValueError:
        ros_values = {}
    return {"prefix": prefix, "setups": scripts, "environment": ros_values, "help": help_result.stdout,
            "storage_plugins": plugins.stdout.split() if plugins.returncode == 0 else [],
            "storage_probe_ok": plugins.returncode == 0, "visible_topics": visible_topics,
            "topic_types": topic_types, "unsupported_topics": unsupported}


def preflight(request):
    path = path_value(request["path"])
    path.mkdir(parents=True, exist_ok=True)
    if path.is_symlink() or not path.is_dir():
        raise Fault("录制根目录必须为普通目录", 2, "invalid_path")
    with tempfile.NamedTemporaryFile(dir=str(path)):
        pass
    free = shutil.disk_usage(str(path)).free
    if free < request["min_free_bytes"]:
        raise Fault("录制文件系统可用空间不足", 7, "disk_full", {"free_bytes": free, "path": str(path)})
    result = {"path": str(path.resolve()), "free_bytes": free, "writable": True}
    return {**result, **ros_environment(request)} if request.get("check_ros", True) else result


def session_root():
    root = Path.home() / ".local/state/zs-support/sessions"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    return root


def session_path(sid):
    if not re.fullmatch(r"[0-9a-f]{32}", sid):
        raise Fault("无效会话 ID", 2, "invalid_session")
    return session_root() / sid


def process_info(pid):
    try:
        fields = Path("/proc/{}/stat".format(pid)).read_text().rsplit(")", 1)[1].split()
        return {"state": fields[0], "pgid": int(fields[2]), "start_ticks": fields[19]}
    except (FileNotFoundError, ProcessLookupError):
        return None


def owned_alive(meta):
    if meta["boot_id"] != identity()["boot_id"]:
        return False
    info = process_info(meta["pid"])
    if not info or info["state"] == "Z" or info["start_ticks"] != meta["start_ticks"] or info["pgid"] != meta["pid"]:
        return False
    command = Path("/proc/{}/cmdline".format(meta["pid"])).read_bytes().split(b"\x00")
    return str(session_path(meta["id"]) / "worker.py").encode() in command


WORKER = r'''
import json, os, signal, subprocess, sys, time
from pathlib import Path
root = Path(sys.argv[1])
request = json.loads((root / 'request.json').read_text())
def save(name, data):
    temp = root / (name + '.tmp')
    temp.write_text(json.dumps(data))
    os.replace(str(temp), str(root / name))
try:
    # The supervisor stays alive to reap the recorder after group SIGINT.
    signal.signal(signal.SIGINT, lambda *_: None)
    with (root / 'record.log').open('ab', buffering=0) as log:
        child = subprocess.Popen(['bash', '-c', request['command']], stdin=subprocess.DEVNULL,
                                 stdout=log, stderr=log)
        save('ready.json', {'child_pid': child.pid})
        rc = child.wait()
    save('result.json', {'exit_code': rc, 'ended_at': time.time()})
except BaseException as exc:
    save('result.json', {'exit_code': 1, 'ended_at': time.time(), 'error': str(exc)})
'''


@contextmanager
def board_lock(name):
    path = session_root().parent / (name + ".lock")
    with path.open("a") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Fault("该板卡已有同类任务运行", 7, "busy")
        yield


def record_start(request):
    with board_lock("record"):
        root = session_path(request["id"])
        if root.exists():
            return record_status(request)
        for metadata in session_root().glob("*/meta.json"):
            if owned_alive(read_json(metadata)):
                raise Fault("该板卡已有本工具录制会话，请先停止", 7, "already_recording")
        path = path_value(request["path"])
        if path.exists():
            raise Fault("录制目标已存在，拒绝覆盖", 7, "output_exists")
        space = preflight({**request, "path": str(path.parent), "check_ros": False})
        pre = {**(request.get("prepared_ros") or ros_environment(request)), **space}
        args = ["ros2", "bag", "record", "-o", str(path)]
        storage = request.get("storage")
        if storage:
            if "--storage" not in pre["help"]:
                raise Fault("设备不支持指定存储格式", 7, "unsupported_storage")
            if not pre["storage_probe_ok"] or storage not in pre["storage_plugins"]:
                raise Fault("未探测到请求的存储插件", 7, "unsupported_storage", {"plugins": pre["storage_plugins"]})
            args += ["--storage", storage]
        if request.get("qos"):
            if "--qos-profile-overrides-path" not in pre["help"]:
                raise Fault("设备不支持 QoS 覆盖", 7, "unsupported_qos")
            require_path(request["qos"])
            args += ["--qos-profile-overrides-path", request["qos"]]
        if not request["topics"]:
            raise Fault("没有可录制 Topic", 2, "invalid_topics")
        unsupported = {topic: pre.get("unsupported_topics", {})[topic] for topic in request["topics"]
                       if topic in pre.get("unsupported_topics", {})}
        if unsupported:
            raise Fault("消息类型可用性发生变化，请重新选择录制场景", 7, "unsupported_topic_types", unsupported)
        args += request["topics"]
        root.mkdir(mode=0o700)
        write_json(root / "request.json", {"command": pre["prefix"] + "export RCUTILS_LOGGING_BUFFERED_STREAM=0; exec " + shlex.join(args)})
        (root / "worker.py").write_text(WORKER)
        with (root / "supervisor.log").open("ab") as output:
            proc = subprocess.Popen([sys.executable, str(root / "worker.py"), str(root)],
                                    stdin=subprocess.DEVNULL, stdout=output, stderr=output,
                                    start_new_session=True, close_fds=True)
        info = process_info(proc.pid)
        meta = {"id": request["id"], "pid": proc.pid, "start_ticks": info["start_ticks"],
                "boot_id": identity()["boot_id"], "board_id": identity()["id"], "path": str(path),
                "topics": request["topics"], "storage": storage or "device_default"}
        write_json(root / "meta.json", meta)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if (root / "result.json").exists():
                break
            if recorder_ready(root):
                break
            time.sleep(.2)
        return {**record_status(request), "preflight": pre,
                "missing_topics": [t for t in request["topics"] if t not in pre["visible_topics"]]}


def recorder_ready(root):
    """Watch the recorder's own ready event; never lock its live SQLite DB."""
    try:
        with (root / "record.log").open("rb") as stream:
            # A large scenario can have many subscriptions before its ready event.
            stream.seek(max(0, os.fstat(stream.fileno()).st_size - 65536))
            logs = stream.read()
        return b"Recording..." in logs or b"Recording started" in logs
    except FileNotFoundError:
        return False


def recorded_data(path):
    """Inspect only a finished recorder's SQLite files, never a live writer."""
    import sqlite3
    from contextlib import closing
    count = 0
    inspected = False
    for database in path.glob("*.db3"):
        inspected = True
        try:
            with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=.05)) as connection:
                # LIMIT avoids repeatedly scanning a large live bag.
                row = connection.execute("SELECT 1 FROM messages LIMIT 1").fetchone()
                count += int(row is not None)
        except sqlite3.Error:
            pass
    if inspected:
        return {"has_messages": count > 0, "source": "sqlite_messages"}
    return {"has_messages": any(p.stat().st_size > 4096 for p in path.glob("*.mcap")), "source": "mcap_data"}


def record_status(request):
    root = session_path(request["id"])
    if not (root / "meta.json").is_file():
        return {"state": "unknown", "reason": "远端会话标识不存在"}
    meta = read_json(root / "meta.json")
    if meta["board_id"] != identity()["id"]:
        raise Fault("会话设备身份不匹配", 5, "identity_mismatch")
    path = Path(meta["path"])
    alive = owned_alive(meta)
    evidence = {"has_messages": False, "started": recorder_ready(root), "source": "recorder_log"} if alive else recorded_data(path)
    has_data = evidence["has_messages"]
    metadata = path / "metadata.yaml"
    complete = metadata.is_file() and metadata.stat().st_size > 0 and has_data
    if alive:
        state = "recording" if evidence["started"] else "starting"
    elif (root / "result.json").exists():
        result = read_json(root / "result.json")
        state = "completed" if result["exit_code"] in (0, 130, -2) and complete else "abnormal"
    else:
        result = {}
        state = "abnormal"
    data = {**meta, "state": state, "complete": complete, "data_evidence": evidence,
            "result": read_json(root / "result.json") if (root / "result.json").exists() else None}
    if state == "abnormal":
        if data["result"] and data["result"].get("exit_code") in (0, 130, -2) and not has_data:
            data["reason"] = "录制已停止，但未收到任何话题消息；请检查话题发布状态与 ROS 环境。"
        else:
            data["reason"] = "录制进程已结束，但未得到有效 Bag；请查看录制日志。"
    if state in ("abnormal", "starting"):
        try:
            with (root / "record.log").open("rb") as log:
                log.seek(max(0, os.fstat(log.fileno()).st_size - 2000))
                data["log_tail"] = log.read().decode("utf-8", "replace")
        except OSError:
            pass
    return data


def record_stop(request):
    status = record_status(request)
    if status["state"] not in ("recording", "starting"):
        return status
    # Recheck immediately before signalling, including boot, process start and cmdline.
    if not owned_alive(status):
        raise Fault("会话进程身份已变化，拒绝发送信号", 5, "process_mismatch")
    root = session_path(request["id"])
    if not (root / "ready.json").exists():
        raise Fault("录制启动尚未就绪，请稍后重试", 7, "not_ready")
    os.killpg(status["pid"], signal.SIGINT)
    deadline = time.monotonic() + request.get("stop_timeout", 40)
    while time.monotonic() < deadline:
        status = record_status(request)
        if status["state"] not in ("recording", "starting"):
            return status
        time.sleep(.3)
    return {**status, "state": "stop_timeout", "reason": "已发送 SIGINT，但未确认退出；可再次 status/stop 核验"}


def log_archive(request):
    with board_lock("log"):
        marker = session_root().parent / "log-active.json"
        if marker.exists():
            previous = read_json(marker)
            if previous["boot_id"] == identity()["boot_id"]:
                for entry in Path("/proc").iterdir():
                    if entry.name.isdigit():
                        process = process_info(int(entry.name))
                        if process and process["pgid"] == previous["pgid"] and process["state"] != "Z":
                            raise Fault("前次日志脚本仍在运行，请稍后重试", 7, "script_busy")
        candidates = request.get("scripts", [])
        # Bounded HOME discovery is a capability probe, not a claimed firmware path.
        if not candidates:
            for current, dirs, names in os.walk(str(Path.home())):
                dirs[:] = [d for d in dirs if not d.startswith(".") and not Path(current, d).is_symlink()]
                if len(Path(current).relative_to(Path.home()).parts) >= 2:
                    dirs[:] = []
                if "get_robot_log.sh" in names:
                    candidates.append(str(Path(current) / "get_robot_log.sh"))
        available = [p for p in candidates if inspect_path(p)["status"] == "ok" and Path(p).is_file()]
        if not available:
            raise Fault("未发现 get_robot_log.sh；可用 --log-script 指定绝对路径", 8, "script_missing", {"tried": candidates})
        if len(available) != 1:
            raise Fault("发现多个日志脚本，请用 --log-script 明确选择", 3, "script_ambiguous", {"paths": available})
        script = Path(available[0])
        before = set(glob.glob("/tmp/robot_log_*.tar.gz"))
        if not shutil.which("sudo") or not shutil.which("flock"):
            raise Fault("设备缺少 sudo 或 flock", 6, "remote_dependency")
        # This lock inside sudo survives SSH/helper disconnects and sudo's
        # optional PTY/process-group changes while the script is running.
        lock_path = session_root().parent / "log-script.lock"
        wrapper = 'umask 077; exec 9>"$1"; flock -n 9 || exit 75; exec bash "$2"'
        proc = subprocess.Popen(["sudo", "-S", "-p", "", "--", "bash", "-c", wrapper,
                                 "zs-support-log", str(lock_path), str(script)], cwd=str(script.parent),
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                start_new_session=True, universal_newlines=True)
        write_json(marker, {"boot_id": identity()["boot_id"], "pgid": proc.pid})
        try:
            out, err = proc.communicate(request["password"] + "\n", timeout=request.get("script_timeout", 180))
        except subprocess.TimeoutExpired:
            # The script may run as root. Best-effort sudo termination, retaining the lock
            # until the command is reaped; never kill an unrelated process by name.
            subprocess.run(["sudo", "-S", "-p", "", "--", "kill", "-TERM", "--", "-" + str(proc.pid)],
                           input=request["password"] + "\n", stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, universal_newlines=True, timeout=5)
            try:
                proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                raise Fault("日志脚本超时且仍在运行，后续任务需先核验", 7, "script_timeout")
            raise Fault("日志脚本超时", 7, "script_timeout")
        if proc.returncode:
            if proc.returncode == 75:
                raise Fault("该板卡已有日志脚本运行", 7, "script_busy")
            # Never echo script output: a device script could echo its sudo input.
            raise Fault("日志脚本执行失败（退出码 {}）".format(proc.returncode), 7, "script_failed")
        created = set(glob.glob("/tmp/robot_log_*.tar.gz")) - before
        mentioned = set(re.findall(r"/tmp/robot_log_[A-Za-z0-9_.-]+\.tar\.gz", out)) & created
        choices = mentioned or created
        if len(choices) != 1:
            raise Fault("无法唯一确定本次新归档（旧归档不会作为结果）", 7, "archive_ambiguous")
        archive = choices.pop()
        # The helper uses umask 077. sudo inherits it, so a newly generated archive
        # is root:root/0600 on real M1 firmware. Transfer ownership of only THIS
        # job's regular archive to the SSH user; keep its private permissions.
        import stat as stat_module
        archive_stat = os.lstat(archive)
        if not stat_module.S_ISREG(archive_stat.st_mode):
            raise Fault("本次日志产物不是普通文件", 7, "archive_invalid")
        if archive_stat.st_uid != os.getuid():
            ownership = subprocess.run(["sudo", "-S", "-p", "", "--", "chown", "--no-dereference",
                                        str(os.getuid()) + ":" + str(os.getgid()), "--", archive],
                                       input=request["password"] + "\n", stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, universal_newlines=True, timeout=10)
            if ownership.returncode:
                raise Fault("本次归档已生成，但无法授权当前账号下载", 5, "archive_permission")
        if os.lstat(archive).st_ino != archive_stat.st_ino or Path(archive).is_symlink():
            raise Fault("本次日志归档身份发生变化", 5, "archive_changed")
        require_path(archive)
        stat = os.stat(archive)
        if stat.st_size == 0:
            raise Fault("本次归档为空", 7, "archive_empty")
        time.sleep(.5)
        if (stat.st_size, stat.st_mtime_ns) != (os.stat(archive).st_size, os.stat(archive).st_mtime_ns):
            raise Fault("本次归档仍在写入", 7, "archive_changing")
        try:
            # Read every byte through gzip to verify CRC, then validate tar structure.
            import gzip
            with gzip.open(archive, "rb") as stream:
                while stream.read(1024 * 1024):
                    pass
            with tarfile.open(archive, "r:gz") as stream:
                if not stream.getmembers():
                    raise ValueError("empty tar")
        except (OSError, EOFError, tarfile.TarError, ValueError):
            raise Fault("本次归档损坏或内容为空", 7, "archive_corrupt")
        return {"path": archive, "script": str(script), "sha256": digest(archive),
                "size": stat.st_size, "collected_at": time.time()}


def dispatch(request):
    action = request["action"]
    if request.get("expected_id") and request["expected_id"] != identity()["id"]:
        raise Fault("当前板卡与已保存设备不一致，请重新 connect", 5, "identity_mismatch")
    handlers = {"probe": probe, "scan": scan, "preflight": preflight, "record_start": record_start,
                "record_status": record_status, "record_stop": record_stop, "log_archive": log_archive}
    if action == "paths":
        return {"paths": [inspect_path(p) for p in request["paths"]]}
    if action == "manifest":
        return manifest(request["path"], request.get("hashes", False))
    if action == "ros":
        return ros_environment(request)
    if action not in handlers:
        raise Fault("未知远端操作", 2, "invalid_action")
    return handlers[action](request)


def main():
    os.umask(0o077)
    try:
        request = json.load(sys.stdin)
        data = dispatch(request)
        response = {"ok": True, "data": data}
    except Fault as exc:
        response = {"ok": False, "error": {"message": str(exc), "code": exc.code,
                    "reason": exc.reason, "details": exc.details}}
    except PermissionError as exc:
        response = {"ok": False, "error": {"message": "远端权限不足：" + str(exc.filename), "code": 5, "reason": "permission_denied"}}
    except FileNotFoundError as exc:
        response = {"ok": False, "error": {"message": "远端文件或命令不存在：" + str(exc.filename), "code": 8, "reason": "missing"}}
    except subprocess.TimeoutExpired:
        response = {"ok": False, "error": {"message": "远端能力探测超时", "code": 7, "reason": "timeout"}}
    except Exception as exc:
        response = {"ok": False, "error": {"message": "远端操作失败：" + type(exc).__name__, "code": 1, "reason": "remote_error"}}
    print(json.dumps(response, ensure_ascii=False))


if __name__ == "__main__":
    main()
