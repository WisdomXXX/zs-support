import re
import uuid
import time
from datetime import datetime, timezone
from zs_support.config import topics, topic_groups
from zs_support.domain import SupportError, Progress, TaskControl
from zs_support.services.devices import now


class Recording:
    def __init__(self, devices, store, config, control=None):
        self.devices, self.store, self.config = devices, store, config
        self.control = control or TaskControl()
        self.pending_session = None
        self._preview_cache = None

    def _save(self, session):
        self.store.write("sessions/" + session["id"] + ".json", session)
        return session

    def preview(self, board):
        self.control.check()
        context, identity, transport = self.devices.open(board)
        transport.control = self.control
        spec = self.config["products"][context["product"]]["boards"][board]
        result = transport.call("ros", expected_id=identity["id"],
                                ros_setups=[self.config["ros_setup"]] if self.config.get("ros_setup") else spec["ros_setups"],
                                ros_overlays=spec.get("ros_overlays", []), _timeout=100)
        self._preview_cache = (identity["id"], time.monotonic(), result)
        visible = set(result["visible_topics"])
        unsupported = result.get("unsupported_topics", {})
        groups = topic_groups(context["product"])
        order = [key for key in ("navigation", "perception", "localization", "slam") if key in groups]
        order += [key for key in groups if key not in order and key != "custom"]
        scenes = []
        for key in order:
            values = list(dict.fromkeys(groups[key]["topics"]))
            scenes.append({"key": key, "name": groups[key]["name"], "topic_count": len(values),
                           "visible_count": sum(topic in visible for topic in values),
                           "recordable_count": sum(topic in visible and topic not in unsupported for topic in values),
                           "unsupported_count": sum(topic in unsupported for topic in values),
                           "missing_count": sum(topic not in visible for topic in values)})
        return {"product": context["product"], "algorithm": context["algorithm"], "board": board,
                "version": identity["version"], "scenes": scenes, "visible_topic_count": len(visible),
                "unsupported_topics": unsupported,
                "environment": result.get("environment", {}), "setups": result["setups"]}

    def start(self, board, scenes, added=(), removed=(), remote_path=None, storage=None, qos=None):
        self.control.check()
        context, identity, transport = self.devices.open(board)
        transport.control = self.control
        spec = self.config["products"][context["product"]]["boards"][board]
        selected = topics(context["product"], scenes, added, removed)
        sid = uuid.uuid4().hex
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        scene_name = "-".join(scenes) or "custom"
        name = "_".join([context["product"], context["algorithm"]["type"], board, scene_name, timestamp, sid[:8]])
        options = {"min_free_bytes": self.config["min_free_bytes"],
                   "ros_setups": [self.config["ros_setup"]] if self.config.get("ros_setup") else spec["ros_setups"],
                   "ros_overlays": spec.get("ros_overlays", []), "expected_id": identity["id"]}
        root = remote_path or identity["home"] + "/Downloads/zs-support/bags"
        warnings = []
        try:
            cached = self._preview_cache
            reuse = cached and cached[0] == identity["id"] and time.monotonic() - cached[1] < 30
            pre = transport.call("preflight", path=root, check_ros=not reuse, **options, _timeout=100)
            if reuse:
                pre = {**cached[2], **pre}
        except SupportError as exc:
            if remote_path or exc.reason not in ("permission_denied", "missing", "invalid_path"):
                raise
            root = "/var/tmp/zs-support/" + spec["user"] + "/bags"
            warnings.append("默认录制目录不可用，使用临时目录 " + root + "，可能被系统清理")
            pre = transport.call("preflight", path=root, **options, _timeout=100)
        self.control.progress(Progress("preflight", message=f"保存目录 {pre['path']}，可用空间 {pre['free_bytes'] / 1073741824:.2f} GiB，可写"))
        unsupported = pre.get("unsupported_topics", {})
        excluded = {topic: unsupported[topic] for topic in selected if topic in unsupported}
        if any(topic in excluded for topic in added):
            raise SupportError("自定义话题的消息类型库不可用，请先安装/加载对应环境", 7, "unsupported_topic_types", excluded)
        selected = [topic for topic in selected if topic not in excluded]
        if not selected:
            raise SupportError("此场景没有类型库可用的话题", 7, "unsupported_topic_types", excluded)
        if excluded:
            self.control.progress(Progress("topics", message=f"当前版本无法加载 {len(excluded)} 个话题的类型库，本次跳过：" + ", ".join(excluded)))
        missing = [t for t in selected if t not in pre["visible_topics"]]
        self.control.progress(Progress("topics", message=f"本次录制 {len(selected)} 个话题，当前可见 {len(selected) - len(missing)} 个，待发布 {len(missing)} 个。"))
        if missing:
            self.control.progress(Progress("topics", message="待发布话题示例（允许稍后出现）：" + ", ".join(missing[:5])))
        session = {"id": sid, "device_id": context["device_id"], "board_id": identity["id"],
                   "product": context["product"], "board": board, "host": identity["host"],
                   "algorithm": context["algorithm"], "path": pre["path"] + "/" + name,
                   "topics": selected, "scenes": scenes, "storage": storage or "device_default",
                   "started_at": now(), "state": "starting", "warnings": warnings}
        session["excluded_topics"] = excluded
        self._save(session)  # Persist BEFORE launch, including timeout/disconnect ambiguity.
        self.pending_session = sid
        self.control.check()
        try:
            result = transport.call("record_start", id=sid, path=session["path"], topics=selected,
                                    storage=storage, qos=qos, prepared_ros=pre, **options, _timeout=110)
            session.update({"remote": result, "state": result["state"], "missing_topics": missing})
            if result["state"] == "recording":
                session["recording_started_at"] = now()
        except SupportError as exc:
            rejected = exc.reason in ("already_recording", "busy", "output_exists", "unsupported_storage",
                                      "unsupported_qos", "unsupported_topic_types", "invalid_topics", "disk_full")
            uncertain = not rejected
            session.update({"state": "unknown" if uncertain else "failed", "error": exc.to_dict()})
            self._save(session)
            raise SupportError(str(exc), exc.code, exc.reason, {**exc.details, "session": sid}) from exc
        self._save(session)
        if session["state"] == "abnormal":
            raise SupportError("录制启动失败，详情与日志保存在会话中", 7, "record_failed", {"session": session})
        return session

    def active(self):
        context = self.devices.current()
        sessions = self.store.sessions(context["device_id"])
        active = []
        for session in sessions:
            if session.get("product") != context["product"] or session["state"] not in ("starting", "recording", "unknown", "stop_timeout"):
                continue
            checked = self.refresh(session["id"])
            if checked["state"] in ("starting", "recording", "unknown", "stop_timeout"):
                active.append(checked)
        return active

    def get(self, sid):
        if not re.fullmatch(r"[0-9a-f]{32}", sid):
            raise SupportError("会话 ID 无效", 2, "invalid_session")
        session = self.store.read("sessions/" + sid + ".json")
        if not session:
            raise SupportError("未找到会话", 8, "session_missing")
        context = self.devices.current()
        if context["device_id"] != session["device_id"] or context["product"] != session["product"]:
            raise SupportError("会话属于另一设备，请先连接对应设备", 5, "identity_mismatch")
        return session

    def refresh(self, sid, stop=False):
        self.control.check()
        session = self.get(sid)
        if session["state"] == "failed":
            return session
        try:
            context = self.devices.current()
            board = context["boards"].get(session["board"], {})
            if board.get("id") != session["board_id"]:
                raise SupportError("会话板卡身份不匹配，请连接原设备", 5, "identity_mismatch")
            transport = self.devices.factory(self.devices.endpoint(context["product"], session["board"], board["host"]), self.config)
            transport.control = self.control
            params = {"id": sid, "expected_id": session["board_id"], "stop_timeout": self.config["stop_timeout"],
                      "_timeout": self.config["stop_timeout"] + 15}
            try:
                result = transport.call("record_stop" if stop else "record_status", **params)
            except SupportError as exc:
                if exc.code != 4:
                    raise
                _, _, transport = self.devices.open(session["board"])
                transport.control = self.control
                result = transport.call("record_stop" if stop else "record_status", **params)
            if result["state"] == "unknown" and result.get("reason") == "远端会话标识不存在":
                age = (datetime.now(timezone.utc) - datetime.fromisoformat(session["started_at"])).total_seconds()
                if age > 120:
                    result = {**result, "state": "failed", "reason": "该次启动未创建远端录制任务"}
            session.update({"state": result["state"], "remote": result, "checked_at": now()})
            session.pop("error", None)
        except SupportError as exc:
            session.update({"state": "unknown", "error": exc.to_dict(), "checked_at": now()})
            self._save(session)
            raise
        return self._save(session)
