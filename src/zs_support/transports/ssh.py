import base64
import json
import logging
import os
import re
import shlex
import shutil
import signal
import subprocess
import tempfile
import time
from importlib.resources import files
from pathlib import Path

from zs_support.domain import CommandResult, SupportError, Progress, TaskControl
from zs_support.config import config_dir
from zs_support.transports.network import select_interface, route_details


def dependency(name):
    if not shutil.which(name):
        raise SupportError(f"缺少依赖 {name}；请安装后重试（zs-support doctor）", 6, "missing_dependency")


def ssh_error(code, message):
    lower = message.lower()
    if "host key verification failed" in lower or "remote host identification has changed" in lower:
        return SupportError("SSH 主机身份校验失败；请核验设备后处理 known_hosts", 5, "host_key_changed")
    if code == 5 or "permission denied" in lower or "authentication" in lower:
        return SupportError("设备认证失败或权限不足，请核对产品与板卡选择", 5, "permission_denied")
    return SupportError("SSH 连接失败：" + message.strip()[-500:], 4, "connection_failed")


class SSHTransport:
    def __init__(self, endpoint, config):
        self.endpoint, self.config = endpoint, config
        self.control = TaskControl()
        if not re.fullmatch(r"[A-Za-z0-9_.:-]+", endpoint.host) or endpoint.host.startswith("-"):
            raise SupportError("设备地址无效", 2, "invalid_host")
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", endpoint.user):
            raise SupportError("账号配置无效", 3, "invalid_config")
        self.interface = select_interface(endpoint.host, config.get("interface"))

    def ssh_args(self):
        known = config_dir() / "known_hosts"
        known.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        return ["ssh", *(["-o", "BindInterface=" + self.interface] if self.interface else []), "-p", str(self.endpoint.port), "-o", "StrictHostKeyChecking=accept-new",
                "-o", "UserKnownHostsFile=" + str(known), "-o", "GlobalKnownHostsFile=/dev/null",
                "-o", "ConnectTimeout=" + str(self.config["connect_timeout"]),
                "-o", "ServerAliveInterval=10", "-o", "ServerAliveCountMax=2",
                "-o", "NumberOfPasswordPrompts=1", "-o", "LogLevel=ERROR"]

    def _spawn(self, args, **kwargs):
        dependency("sshpass")
        dependency("ssh")
        read_fd, write_fd = os.pipe()
        try:
            os.write(write_fd, (self.endpoint.password + "\n").encode())
            os.close(write_fd)
            write_fd = -1
            return subprocess.Popen(["sshpass", "-d", str(read_fd), *args], pass_fds=(read_fd,),
                                    start_new_session=True, **kwargs)
        finally:
            os.close(read_fd)
            if write_fd != -1:
                os.close(write_fd)

    @staticmethod
    def _terminate(proc):
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGTERM)
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()

    def run(self, command, stdin=b"", timeout=30):
        # A request can exceed the pipe buffer. Retrying communicate(None) after
        # a short timeout can stop forwarding unwritten input on Python 3.10.
        # A seekable private file gives SSH the complete request and a real EOF.
        with tempfile.TemporaryFile() as request_input:
            request_input.write(stdin)
            request_input.seek(0)
            proc = self._spawn([*self.ssh_args(), f"{self.endpoint.user}@{self.endpoint.host}", command],
                               stdin=request_input, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + timeout
            started = time.monotonic()
            heartbeat = started + 5
            while True:
                self.control.check()
                if time.monotonic() >= heartbeat:
                    elapsed = int(time.monotonic() - started)
                    label = {"log_archive": "设备日志打包与校验", "manifest": "文件清单与校验",
                             "ros": "ROS 环境与话题探测", "preflight": "录制预检", "record_start": "录制启动",
                             "record_stop": "停止并等待 Bag 收尾"}.get(getattr(self, "action", ""), "远程操作")
                    self.control.progress(Progress("waiting", message=f"{label}进行中，已等待 {elapsed} 秒…"))
                    heartbeat = time.monotonic() + 5
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(command, timeout)
                try:
                    out, err = proc.communicate(timeout=min(.25, remaining))
                    break
                except subprocess.TimeoutExpired:
                    pass
        except subprocess.TimeoutExpired as exc:
            self._terminate(proc)
            proc.communicate()
            raise SupportError("远程命令超时，任务状态需要重新核验", 7, "timeout") from exc
        except BaseException:
            self._terminate(proc)
            proc.communicate()
            raise
        result = CommandResult(proc.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace"))
        if result.code in (5, 6, 7, 255):
            error = ssh_error(result.code, result.stderr)
            if error.code == 4:
                error.details.update(route_details(self.endpoint.host, self.interface))
            raise error
        return result

    def call(self, action, **kwargs):
        self.action = action
        logging.getLogger("zs_support").debug("remote action=%s board=%s host=%s", action, self.endpoint.board, self.endpoint.host)
        timeout = kwargs.pop("_timeout", self.config["command_timeout"])
        helper = files("zs_support.transports").joinpath("remote_agent.py").read_bytes()
        code = "import base64;exec(compile(base64.b64decode(" + repr(base64.b64encode(helper).decode()) + "),'<zs-support>','exec'))"
        request = {"action": action, **kwargs}
        if action == "log_archive":
            request["password"] = self.endpoint.password
        result = self.run("python3 -c " + shlex.quote(code), json.dumps(request).encode(), timeout)
        if result.code == 127:
            raise SupportError("设备缺少 python3，无法运行安全会话与文件探测", 6, "remote_dependency")
        try:
            payload = json.loads(result.stdout)
        except ValueError as exc:
            raise SupportError("远端响应格式异常：" + result.stderr[-300:], 7, "invalid_response") from exc
        if not payload.get("ok"):
            error = payload["error"]
            raise SupportError(error["message"], error["code"], error["reason"], error.get("details"))
        return payload["data"]

    def download(self, source, destination, is_dir, control=None):
        control = control or TaskControl()
        control.check()
        dependency("rsync")
        destination = Path(destination)
        destination.mkdir(parents=True, exist_ok=True)
        # -s protects remote paths; no local shell and no password in argv/environment.
        host = "[" + self.endpoint.host + "]" if ":" in self.endpoint.host else self.endpoint.host
        remote = f"{self.endpoint.user}@{host}:{source}" + ("/" if is_dir else "")
        args = ["rsync", "-rlt", "--protect-args", "--partial", "--partial-dir=.rsync-partial",
                "--no-links", "--info=progress2", "--outbuf=L", "--timeout=60",
                "-e", shlex.join(self.ssh_args()), "--", remote, str(destination) + "/"]
        import tempfile
        with tempfile.TemporaryFile() as output:
            proc = self._spawn(args, stdin=subprocess.DEVNULL, stdout=output, stderr=output)
            start, offset = time.monotonic(), 0
            done = 0
            tail = b""
            try:
                while proc.poll() is None:
                    control.check()
                    if time.monotonic() - start > self.config["transfer_timeout"]:
                        raise SupportError("传输超时，重新执行可续传", 7, "timeout")
                    chunk = os.pread(output.fileno(), 65536, offset)
                    offset += len(chunk)
                    tail = (tail + chunk)[-4096:]
                    matches = re.findall(rb"([\d,]+)\s+\d+%", chunk)
                    if matches:
                        done = int(matches[-1].replace(b",", b""))
                    control.progress(Progress("transfer", done, bytes_per_second=done / max(time.monotonic()-start, .01),
                                              destination=str(destination)))
                    time.sleep(.2)
            except BaseException:
                self._terminate(proc)
                raise
            tail = (tail + os.pread(output.fileno(), max(0, os.fstat(output.fileno()).st_size - offset), offset))[-4096:]
            final_matches = re.findall(rb"([\d,]+)\s+\d+%", tail)
            if final_matches:
                done = int(final_matches[-1].replace(b",", b""))
            control.progress(Progress("transfer", done, bytes_per_second=done / max(time.monotonic()-start, .01),
                                      destination=str(destination)))
            if proc.returncode:
                message = tail.decode("utf-8", "replace")
                if proc.returncode == 255 or "Permission denied" in message or "Host key" in message:
                    raise ssh_error(proc.returncode, message)
                reason = "local_disk_full" if "No space left" in message else "transfer_failed"
                raise SupportError("传输未完成，已保留断点：" + message[-600:], 1, reason)
