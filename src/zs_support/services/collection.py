import hashlib
import json
import os
import shutil
import time
from pathlib import Path

from zs_support.domain import SupportError, Progress, TaskControl
from zs_support.services.devices import now
from zs_support.services.versions import numeric_version
from zs_support.storage import atomic_json


def default_output():
    downloads = Path.home() / "Downloads"
    user_dirs = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "user-dirs.dirs"
    if user_dirs.exists():
        for line in user_dirs.read_text().splitlines():
            if line.startswith("XDG_DOWNLOAD_DIR="):
                raw = line.split("=", 1)[1].strip().strip('"')
                candidate = Path(raw.replace("$HOME", str(Path.home())))
                if candidate.is_absolute():
                    downloads = candidate
    return downloads / "zs-support" if downloads.is_dir() else Path.home() / "zs-support"


def file_hash(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


class Collection:
    def __init__(self, devices, store, config, control=None):
        self.devices, self.store, self.config = devices, store, config
        self.control = control or TaskControl()

    def paths(self, board, kind, explicit=None):
        self.control.check()
        context, ident, transport = self.devices.open(board)
        transport.control = self.control
        spec = self.config["products"][context["product"]]["boards"][board]
        overrides = self.store.read("path_overrides.json", {})
        saved = overrides.get(ident["id"], {}).get(kind)
        candidates = [explicit] if explicit else list(dict.fromkeys(([saved] if saved else []) + spec["paths"][kind]))
        result = transport.call("paths", paths=candidates, expected_id=ident["id"])["paths"]
        found = [p for p in result if p["status"] == "ok"]
        if not found:
            denied = any(p["status"] == "permission_denied" for p in result)
            if denied:
                raise SupportError("候选路径权限不足", 5, "permission_denied", {"paths": result})
            raise SupportError("默认路径不存在，可能与当前固件路径不匹配；请用 --remote-path 输入完整路径", 8,
                               "paths_missing", {"paths": result})
        return context, ident, transport, found, result

    def list(self, board, kind, explicit=None):
        context, ident, transport, found, attempts = self.paths(board, kind, explicit)
        items = []
        errors = []
        if kind == "bag":
            for session in self.store.sessions(context["device_id"]):
                if session["board_id"] == ident["id"]:
                    items.append({"path": session["path"], "name": Path(session["path"]).name,
                                  "session": session["id"], "complete": session["state"] == "completed",
                                  "state": session["state"], "source": "本工具会话（状态需刷新）"})
        for root in found:
            try:
                result = transport.call("scan", path=root["path"], kind=kind,
                                        depth=self.config["scan_depth"], expected_id=ident["id"], _timeout=90)
                items.extend({**item, "source_root": root["path"]} for item in result["items"])
            except SupportError as exc:
                errors.append({"path": root["path"], "error": exc.to_dict()})
        unique = {}
        for item in items:
            previous = unique.get(item["path"])
            if previous and previous.get("session"):
                # Metadata can exist after a crash. Keep our known session result.
                unique[item["path"]] = {**item, **previous}
            else:
                unique[item["path"]] = item
        return {"items": list(unique.values()), "roots": found, "attempts": attempts, "errors": errors}

    def _download(self, transport, ident, context, board, kind, path, output=None, hashes=False, expected_hash=None):
        self.control.check()
        before = transport.call("manifest", path=path, hashes=hashes, expected_id=ident["id"], _timeout=300)
        if expected_hash and (len(before["files"]) != 1 or before["files"][0].get("sha256") != expected_hash):
            raise SupportError("本次日志归档在校验后发生变化", 1, "archive_changing")
        if not before["files"]:
            raise SupportError("远程目录为空或只有不可采集的链接", 8, "empty_directory", {"path": path})
        if kind == "map":
            required = self.config["products"][context["product"]]["boards"][board].get("map_required_files", [])
            names = {item["name"] for item in before["files"] if item["size"] > 0}
            missing = [name for name in required if name not in names]
            if missing:
                raise SupportError("地图缺少配置要求的文件", 8, "incomplete_map", {"missing": missing})
        base = Path(output or self.config.get("output") or default_output()).expanduser().resolve()
        if not output and not self.config.get("output") and base == Path.home() / "zs-support":
            self.control.progress(Progress("destination", message="系统 Downloads 不存在，保存至 " + str(base)))
        signature = json.dumps([ident["id"], path, before["files"]], sort_keys=True).encode()
        task_id = hashlib.sha256(signature).hexdigest()[:16]
        parent = base / context["product"] / (board + "-" + ident["id"][:12]) / kind
        destination = parent / (Path(path).name + "-" + task_id)
        stage = base / ".partial" / task_id
        with self.store.lock("transfer-" + task_id):
            if (destination / "collection-manifest.json").is_file():
                cached = json.loads((destination / "collection-manifest.json").read_text())
                if self._verify(destination / "data", cached["files"], hashes):
                    return {**cached, "reused": True}
            stage.mkdir(parents=True, exist_ok=True)
            data_dir = stage / "data"
            data_dir.mkdir(exist_ok=True)
            present = sum(p.stat().st_size for p in data_dir.rglob("*") if p.is_file())
            if shutil.disk_usage(stage).free < max(0, before["bytes"] - present) + 16 * 1024 * 1024:
                raise SupportError("本地磁盘空间不足", 7, "local_disk_full")
            result = {"id": task_id, "kind": kind, "board": board, "board_id": ident["id"],
                      "source": path, "destination": str(destination), "state": "transferring",
                      "started_at": now(), "bytes": before["bytes"], "version": ident["version"]}
            if kind == "bag":
                data_dirs = {str(Path(f["name"]).parent) for f in before["files"] if f["name"].endswith((".db3", ".mcap")) and f["size"] > 0}
                meta_dirs = {str(Path(f["name"]).parent) for f in before["files"] if Path(f["name"]).name == "metadata.yaml" and f["size"] > 0}
                result["incomplete"] = not data_dirs or not data_dirs.issubset(meta_dirs)
            if kind == "map":
                result["integrity"] = "配置必需文件已核验" if required else "格式规则待实机确认；仅校验文件清单与大小（可选哈希）"
            self.store.write("transfers/" + task_id + ".json", result)
            original_progress = self.control.progress
            self.control.progress = lambda event: original_progress(Progress(event.phase, event.bytes_done, before["bytes"],
                                                                            event.bytes_per_second, str(destination), event.message))
            try:
                for attempt in range(self.config["retries"] + 1):
                    self.control.check()
                    try:
                        transport.call("probe", expected_id=ident["id"])
                        transport.download(before["path"], data_dir, before["is_dir"], self.control)
                        break
                    except SupportError as exc:
                        if exc.reason == "cancelled" or exc.code in (2, 3, 5, 6) or attempt == self.config["retries"]:
                            raise
                        self.control.progress(Progress("retry", message=f"传输失败，重试 {attempt + 1}/{self.config['retries']}"))
                after = transport.call("manifest", path=path, hashes=hashes, expected_id=ident["id"], _timeout=300)
                changed = before["files"] != after["files"]
                verified = self._verify(data_dir, after["files"], hashes)
                if (kind != "log" or expected_hash) and (changed or not verified):
                    raise SupportError("源文件发生变化或传输校验失败，未完成文件保留", 1, "verification_failed")
                if kind == "log" and not verified and not changed:
                    raise SupportError("日志传输校验失败", 1, "verification_failed")
                actual_files = []
                for file in sorted(data_dir.rglob("*")):
                    if file.is_file():
                        entry = {"name": str(file.relative_to(data_dir)), "size": file.stat().st_size}
                        if hashes:
                            entry["sha256"] = file_hash(file)
                        actual_files.append(entry)
                result.update({"state": "completed", "finished_at": now(), "changed_during_collection": changed,
                               "verified": verified, "files": actual_files, "source_manifest": after,
                               "skipped": after["skipped"], "hashes": hashes})
                atomic_json(stage / "collection-manifest.json", result)
                parent.mkdir(parents=True, exist_ok=True)
                if destination.exists():
                    # Never overwrite an earlier customer collection.
                    destination = parent / (destination.name + "-" + str(time.time_ns()))
                    result["destination"] = str(destination)
                    atomic_json(stage / "collection-manifest.json", result)
                os.replace(stage, destination)
                self.store.write("transfers/" + task_id + ".json", result)
                self.control.progress(Progress("completed", before["bytes"], before["bytes"], destination=str(destination)))
                return result
            except BaseException as exc:
                result.update({"state": "cancelled" if isinstance(exc, KeyboardInterrupt) or getattr(exc, "reason", "") == "cancelled" else "failed",
                               "partial": str(stage), "error": str(exc)})
                self.store.write("transfers/" + task_id + ".json", result)
                raise
            finally:
                self.control.progress = original_progress

    @staticmethod
    def _verify(root, entries, hashes):
        expected = {entry["name"] for entry in entries}
        actual = {str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()}
        if actual != expected:
            return False
        for entry in entries:
            path = root / entry["name"]
            if path.is_symlink() or not path.is_file() or path.stat().st_size != entry["size"]:
                return False
            if hashes and entry.get("sha256") != file_hash(path):
                return False
        return True

    def pull(self, board, kind, path=None, output=None, hashes=False, save_path=False):
        context, ident, transport, roots, attempts = self.paths(board, kind, path)
        result = self._download(transport, ident, context, board, kind, roots[0]["path"], output, hashes)
        result["alternatives"] = roots[1:]
        result["path_attempts"] = attempts
        if save_path and path:
            with self.store.lock("overrides"):
                overrides = self.store.read("path_overrides.json", {})
                overrides.setdefault(ident["id"], {})[kind] = roots[0]["path"]
                self.store.write("path_overrides.json", overrides)
        self._annotate(result)
        return result

    def logs(self, board, path=None, output=None, hashes=False, save_path=False):
        self.control.check()
        context, ident, transport = self.devices.open(board)
        transport.control = self.control
        spec = self.config["products"][context["product"]]["boards"][board]
        fallback = None
        body_version = numeric_version(ident["version"].get("value", ""))
        if context["product"] == "zsm1" and board == "body" and not path and (body_version is None or body_version >= (0, 3, 2)):
            with self.store.lock("script-" + ident["id"]):
                try:
                    scripts = [self.config["log_script"]] if self.config.get("log_script") else spec.get("log_scripts", [])
                    archive = transport.call("log_archive", scripts=scripts, expected_id=ident["id"],
                                             script_timeout=self.config["script_timeout"], _timeout=self.config["script_timeout"] + 60)
                    result = self._download(transport, ident, context, board, "log", archive["path"], output, True, archive["sha256"])
                    result.update({"method": "script", "archive": archive})
                    self._annotate(result)
                    return result
                except SupportError as exc:
                    if exc.reason == "cancelled":
                        raise
                    fallback = exc.to_dict()
                    self.control.progress(Progress("fallback", message="脚本采集失败，回退目录：" + str(exc)))
        self.control.check()
        try:
            result = self.pull(board, "log", path, output, hashes, save_path)
        except SupportError as exc:
            if fallback:
                raise SupportError("日志脚本和目录回退均失败；可用 --remote-path 指定目录", exc.code, exc.reason,
                                   {"script_error": fallback, "directory_error": exc.to_dict()}) from exc
            raise
        result["method"] = "directory_fallback" if fallback else "directory"
        result["script_error"] = fallback
        result["live_log_notice"] = "日志可能持续写入；结果不是严格同一时刻的快照"
        extras = []
        if not path:
            for extra in spec.get("log_extra", []):
                if extra == result["source"]:
                    continue
                try:
                    extras.append(self.pull(board, "log", extra, output, hashes))
                except SupportError as exc:
                    if exc.reason == "cancelled":
                        raise
                    extras.append({"source": extra, "state": "missing" if exc.code == 8 else "failed", "error": exc.to_dict()})
        result["additional_logs"] = extras
        result["partial_failure"] = any(e["state"] == "failed" for e in extras)
        self._annotate(result)
        return result

    def _annotate(self, result):
        atomic_json(Path(result["destination"]) / "collection-manifest.json", result)
        self.store.write("transfers/" + result["id"] + ".json", result)
