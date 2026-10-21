"""Read-only Linux route inspection for robot links on multi-NIC clients."""
import ipaddress
import json
import re
import shlex
import subprocess

from zs_support.domain import SupportError


def ip_json(*args):
    try:
        process = subprocess.run(["ip", "-j", *args], capture_output=True, text=True, timeout=2)
        return json.loads(process.stdout) if process.returncode == 0 else []
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return []


def interfaces_for(host, addresses):
    try:
        target = ipaddress.ip_address(host)
    except ValueError:
        return [], []
    direct, ap = [], []
    for interface in addresses:
        name = interface.get("ifname", "")
        if not re.fullmatch(r"[A-Za-z0-9_.:-]+", name) or name == "lo" or "UP" not in interface.get("flags", []):
            continue
        for address in interface.get("addr_info", []):
            if address.get("family") != "inet":
                continue
            local = ipaddress.ip_interface(address["local"] + "/" + str(address["prefixlen"]))
            if target in local.network and name not in direct:
                direct.append(name)
            if local.ip in ipaddress.ip_network("192.168.234.0/24") and name not in ap:
                ap.append(name)
    return direct, ap


def select_interface(host, explicit=None):
    if explicit:
        if not re.fullmatch(r"[A-Za-z0-9_.:-]+", explicit) or not ip_json("link", "show", "dev", explicit):
            raise SupportError("指定网卡不存在：" + explicit, 3, "invalid_interface")
        return explicit
    try:
        target = ipaddress.ip_address(host)
    except ValueError:
        return None
    if target.is_loopback:
        return None
    addresses = ip_json("address", "show")
    direct, ap = interfaces_for(host, addresses)
    route = ip_json("route", "get", host)
    current = route[0].get("dev") if route else None
    if current in direct:
        return current
    if len(direct) == 1:
        return direct[0]
    if target in ipaddress.ip_network("192.168.168.0/24") and len(ap) == 1:
        # Binding can select the AP's existing default route without modifying
        # the system route table or touching corporate/VPN connections.
        bound = ip_json("route", "get", host, "oif", ap[0])
        if bound and bound[0].get("dev") == ap[0] and bound[0].get("gateway") == "192.168.234.1":
            return ap[0]
    return None


def prefer_direct_hosts(hosts):
    """Try a directly attached AP/LAN before an address requiring a gateway."""
    addresses = ip_json("address", "show")
    return sorted(hosts, key=lambda host: not bool(interfaces_for(host, addresses)[0]))


def route_details(host, interface=None):
    route = ip_json("route", "get", host)
    direct, ap = interfaces_for(host, ip_json("address", "show"))
    details = {"host": host, "route": route, "bound_interface": interface}
    if host.startswith("192.168.168."):
        chosen = interface or (ap[0] if len(ap) == 1 else None)
        suffix = " dev " + shlex.quote(chosen) if chosen else " dev <机器人网卡>"
        details["hint"] = "连接机器人 AP 后，检查到内部板卡的路由；多网卡可使用 connect --interface <机器人网卡>。"
        details["route_command"] = "sudo ip route add 192.168.168.0/24 via 192.168.234.1" + suffix
        if ap:
            details["robot_ap_interfaces"] = ap
    elif host.startswith("192.168.234.") and not direct:
        details["hint"] = "当前网卡没有机器人 AP 网段地址，请检查是否已连接机器人 Wi-Fi。"
    return details
