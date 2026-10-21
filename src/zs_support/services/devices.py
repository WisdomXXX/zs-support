from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
from zs_support.config import product_key
from zs_support.domain import Endpoint, SupportError, VersionEvidence
from zs_support.services.versions import parse_version, release_versions, classify
from zs_support.transports.ssh import SSHTransport
from zs_support.transports.network import prefer_direct_hosts


def now():
    return datetime.now(timezone.utc).isoformat()


class Devices:
    def __init__(self, config, store, factory=SSHTransport):
        self.config, self.store, self.factory = config, store, factory

    def endpoint(self, product, board, host):
        spec = self.config["products"][product]["boards"][board]
        return Endpoint(product, board, host, spec["user"], spec["password"], spec.get("port", 22))

    def probe_board(self, product, board, host=None, expected=None, fast=False):
        spec = self.config["products"][product]["boards"][board]
        hosts = [host] if host else prefer_direct_hosts(list(spec["hosts"]))
        if fast:
            if not spec.get("fast_host"):
                raise SupportError("此板卡没有配置高速通道", 2, "no_fast_channel")
            hosts = [spec["fast_host"]]
        failures = []
        for address in hosts:
            transport = self.factory(self.endpoint(product, board, address), self.config)
            try:
                data = transport.call("probe", version_path=spec["version_path"], release=product == "zsl1" and board == "body",
                                      device_path=spec.get("device_path", "/userdata/config/device.json") if board == "body" else None)
                if expected and data["id"] != expected:
                    raise SupportError("入口指向另一块板卡，拒绝复用原设备会话", 5, "identity_mismatch")
                evidence = parse_version(data.get("version_text", ""), spec["version_path"]) if data["version_status"] == "ok" else VersionEvidence(spec["version_path"], data["version_status"], error="版本文件读取失败")
                result = {"status": "connected", "host": address, "id": data["id"], "boot_id": data["boot_id"],
                          "home": data["home"], "hardware": data["hardware"], "hardware_evidence": data.get("hardware_evidence", ""),
                          "version": evidence.to_dict(), "checked_at": now(), "attempts": failures}
                result["interface"] = getattr(transport, "interface", None)
                if board == "body":
                    result["device"] = data.get("device", {"status": "missing", "source": "/userdata/config/device.json"})
                if product == "zsl1" and board == "body":
                    result["product_versions"] = release_versions(data.get("release_names", []), self.config["products"][product]["release_prefixes"])
                    result["release_source"] = "/etc/release"
                    result["release_error"] = data.get("release_error")
                return result, transport
            except SupportError as exc:
                failures.append({"host": address, "error": exc.to_dict()})
                if exc.code != 4:
                    raise
        raise SupportError("候选入口均无法连接", 4, "connection_failed", {"attempts": failures})

    def connect(self, product, hosts=None, body_only=False, algorithm=None, fast=False, refresh=False):
        product = product_key(product, self.config)
        previous = self.store.read("context.json", {}) if refresh else {}
        boards = {}
        hosts = hosts or {}
        with ThreadPoolExecutor(max_workers=2) as pool:
            pending = {}
            for board in ("body", "compute"):
                if body_only and board == "compute":
                    boards[board] = {"status": "declared_absent", "source": "用户明确选择仅本体"}
                    continue
                old = previous.get("boards", {}).get(board, {})
                pending[board] = pool.submit(self.probe_board, product, board, hosts.get(board) or old.get("host"),
                                             old.get("id"), fast and board == "compute")
            for board, future in pending.items():
                try:
                    boards[board], _ = future.result()
                except SupportError as exc:
                    if exc.reason in ("identity_mismatch", "host_key_changed"):
                        raise
                    boards[board] = {"status": "unavailable", "error": exc.to_dict(), "checked_at": now()}
        boards = {board: boards[board] for board in ("body", "compute")}
        connected = [v for v in boards.values() if v["status"] == "connected"]
        if not connected:
            errors = [v["error"] for v in boards.values() if "error" in v]
            raise SupportError("设备连接失败，请检查产品、接线与网络", errors[0]["code"], "connection_failed", {"boards": boards})
        # Refreshing availability must not silently change the established context key.
        device_id = previous.get("device_id") or boards["body"].get("id") or boards["compute"]["id"]
        context = {"product": product, "product_source": "用户选择", "device_id": device_id,
                   "boards": boards, "algorithm": classify(boards["compute"], body_only, algorithm),
                   "body_only": body_only, "algorithm_override": algorithm, "connected_at": now()}
        context["device"] = boards["body"].get("device", {"status": "unavailable"})
        context["interface"] = self.config.get("interface")
        self.store.write("context.json", context)
        return context

    def current(self):
        context = self.store.read("context.json")
        if not context:
            raise SupportError("尚未选择设备，请先 zs-support connect --product zsm1", 3, "no_device")
        if context.get("interface") and not self.config.get("interface"):
            self.config["interface"] = context["interface"]
        return context

    def open(self, board, host=None, fast=False):
        context = self.current()
        host = host or self.config.get("host")
        saved = context["boards"].get(board, {})
        if saved.get("status") != "connected":
            raise SupportError("该板卡未连接，请先 connect 刷新连接", 4, "board_unavailable")
        try:
            current, transport = self.probe_board(context["product"], board, host or saved["host"], saved["id"], fast)
        except SupportError as exc:
            if exc.code != 4 or host or fast:
                raise
            current, transport = self.probe_board(context["product"], board, expected=saved["id"])
        context["boards"][board] = current
        if board == "body":
            context["device"] = current.get("device", {"status": "missing"})
        context["algorithm"] = classify(context["boards"].get("compute"), context.get("body_only"), context.get("algorithm_override"))
        self.store.write("context.json", context)
        return context, current, transport
