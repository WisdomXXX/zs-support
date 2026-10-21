import copy
import json
import os
import re
from importlib.resources import files
from pathlib import Path

from zs_support.domain import SupportError

DEFAULTS = {"min_free_bytes": 1073741824, "connect_timeout": 5, "command_timeout": 30,
            "transfer_timeout": 3600, "script_timeout": 180, "stop_timeout": 40,
            "retries": 2, "scan_depth": 3, "products": {}}


def config_dir():
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "zs-support"


def merge(base, overlay):
    result = copy.deepcopy(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def read_json(path):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("顶层必须为对象")
        return value
    except (ValueError, OSError) as exc:
        raise SupportError(f"配置读取失败：{path}：{exc}", 3, "invalid_config") from exc


def load(cli=None, system=None, user=None):
    cfg = copy.deepcopy(DEFAULTS)
    resource = files("zs_support").joinpath("resources/products/products.json")
    cfg["products"] = json.loads(resource.read_text(encoding="utf-8"))
    for path in (system or Path("/etc/zs-support/config.json"), user or config_dir() / "config.json"):
        if Path(path).exists():
            cfg = merge(cfg, read_json(path))
    for key in DEFAULTS:
        if key == "products":
            continue
        value = os.environ.get("ZS_SUPPORT_" + key.upper())
        if value is not None:
            try:
                cfg[key] = int(value)
            except ValueError as exc:
                raise SupportError(f"环境变量 ZS_SUPPORT_{key.upper()} 必须为整数", 3, "invalid_config") from exc
    for key in ("product", "board", "host", "output", "ros_setup", "log_script", "interface"):
        if os.environ.get("ZS_SUPPORT_" + key.upper()):
            cfg[key] = os.environ["ZS_SUPPORT_" + key.upper()]
    if cli:
        cfg.update({k: v for k, v in cli.items() if v is not None})
    if cfg.get("board") and cfg["board"] not in ("body", "compute"):
        raise SupportError("board 配置只能为 body 或 compute", 3, "invalid_config")
    for key, default in DEFAULTS.items():
        if isinstance(default, int) and (type(cfg[key]) is not int or cfg[key] < (0 if key in ("retries", "min_free_bytes") else 1)):
            raise SupportError(f"配置 {key} 数值无效", 3, "invalid_config")
    try:
        for product in cfg["products"].values():
            assert isinstance(product["aliases"], list)
            for board in product["boards"].values():
                assert board["user"] and board["password"] and board["hosts"]
                assert isinstance(board["hosts"], list)
                for kind in ("bag", "log", "map"):
                    assert isinstance(board["paths"][kind], list)
    except (AssertionError, KeyError, TypeError, AttributeError) as exc:
        raise SupportError("产品配置结构无效，请检查 boards/hosts/paths", 3, "invalid_config") from exc
    return cfg


def product_key(value, cfg):
    value = value.lower()
    for key, product in cfg["products"].items():
        if value in [key] + [alias.lower() for alias in product["aliases"]]:
            return key
    raise SupportError(f"未知产品：{value}；可选 zsl1、zsl2、zsm1", 2, "invalid_product")


def topics(product, scenes, added=(), removed=()):
    groups = topic_groups(product)
    selected = []
    for scene in scenes:
        if scene not in groups:
            raise SupportError(f"未知场景 {scene}；可选 {', '.join(groups)}", 2, "invalid_scene")
        selected.extend(groups[scene]["topics"])
    selected.extend(added)
    selected = list(dict.fromkeys(t for t in selected if t not in removed))
    if not selected or any(not re.fullmatch(r"/[A-Za-z0-9_/]+", t) for t in selected):
        raise SupportError("Topic 不能为空，且必须是以 / 开头的有效 ROS Topic", 2, "invalid_topics")
    return selected


def topic_groups(product):
    return json.loads(files("zs_support").joinpath(f"resources/topics/{product}.json").read_text())
