import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path

from zs_support.domain import SupportError


def state_dir():
    return Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "zs-support"


def atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class Store:
    def __init__(self, root=None):
        self.root = Path(root) if root else state_dir()

    def read(self, name, default=None):
        path = self.root / name
        if not path.exists():
            return default
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError) as exc:
            raise SupportError(f"状态文件无法读取：{path}", 3, "invalid_state") from exc

    def write(self, name, value):
        atomic_json(self.root / name, value)

    @contextmanager
    def lock(self, name):
        import fcntl
        path = self.root / (name + ".lock")
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with path.open("a") as stream:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise SupportError("该设备已有任务运行，请稍后重试", 7, "busy") from exc
            yield

    def sessions(self, device=None):
        items = [self.read(str(p.relative_to(self.root))) for p in (self.root / "sessions").glob("*.json")]
        return sorted([s for s in items if device is None or s["device_id"] == device],
                      key=lambda s: s["started_at"], reverse=True)
