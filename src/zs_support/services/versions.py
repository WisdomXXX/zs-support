import re
import yaml
from zs_support.domain import VersionEvidence


class UniqueLoader(yaml.SafeLoader):
    pass


def _mapping(loader, node, deep=False):
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in result:
            raise ValueError(f"重复字段：{key}")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)


def parse_version(text, source="/opt/release/version.yaml"):
    try:
        data = yaml.load(text, Loader=UniqueLoader)
        if not isinstance(data, dict) or not isinstance(data.get("version"), str) or not data["version"].strip():
            raise ValueError("缺少有效的顶层 version 字符串")
        return VersionEvidence(source, "ok", data["version"].strip())
    except (yaml.YAMLError, ValueError, TypeError) as exc:
        return VersionEvidence(source, "invalid", error=str(exc))


def release_versions(names, prefixes=("602", "622")):
    return sorted({re.sub(r"\.ya?ml$", "", name, flags=re.I) for name in names
                   if any(name.startswith(prefix) for prefix in prefixes)
                   and re.fullmatch(r"[A-Za-z0-9_.-]+", name)})


def classify(compute, body_only=False, override=None):
    value = compute.get("version", {}).get("value", "") if compute else ""
    if compute and compute.get("status") == "connected" and compute.get("version", {}).get("status") == "ok":
        detected = "navigation" if re.match(r"^hermes", value, re.I) else (
            "standard" if re.fullmatch(r"nx\s*v?0\.\d+\.\d+(?:[-+][\w.]+)?", value, re.I) else "unknown")
        reason = "算力板顶层 version 规则" if detected != "unknown" else "算力板版本前缀未知"
    else:
        detected, reason = "unknown", "算力板不可达或版本读取失败；不能视为无算力板"
    if body_only and compute and compute.get("status") == "connected":
        detected, reason = "unknown", "仅本体声明与已连接算力板冲突"
    elif body_only:
        detected, reason = "standard", "用户明确指定仅本体配置"
    return {"type": override or detected, "detected": detected, "source": "人工指定" if override else reason,
            "reason": reason}


def numeric_version(value):
    match = re.fullmatch(r"[vV]?(\d+)\.(\d+)\.(\d+)", value.strip())
    return tuple(map(int, match.groups())) if match else None
