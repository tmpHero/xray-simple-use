"""
Parse VLESS share link and generate Xray-core client JSON config.
"""

import base64
import os
import re
from dataclasses import dataclass, field
from urllib.parse import urlparse, parse_qs, unquote
from typing import Optional
import json


def parse_share_link(url: str) -> "VLESSConfig":
    """
    Auto-detect share link type (vless:// or vmess://) and parse it.

    Args:
        url: A vless:// or vmess:// share link.

    Returns:
        VLESSConfig with all parsed parameters.

    Raises:
        ValueError: If the link is not recognized.
    """
    if url.startswith("vless://"):
        return parse_vless_link(url)
    if url.startswith("vmess://"):
        return parse_vmess_link(url)
    raise ValueError("Unsupported share link type. Expected vless:// or vmess://")


@dataclass
class VLESSConfig:
    """Parsed VLESS/VMESS share link parameters."""
    uuid: str
    address: str
    port: int
    protocol: str = "vless"   # "vless" or "vmess"
    encryption: str = "none"
    security: str = "none"
    flow: str = ""
    sni: str = ""
    fp: str = "chrome"
    pbk: str = ""
    sid: str = ""
    spx: str = ""
    network: str = "tcp"
    path: str = ""
    host: str = ""
    service_name: str = ""
    mode: str = ""
    remark: str = ""
    authority: str = ""
    origin_address: str = ""  # Original hostname from the link, preserved for SNI
    alter_id: int = 0         # VMESS alterId

    def to_dict(self) -> dict:
        """Return a dict summary of non-empty fields."""
        return {k: v for k, v in self.__dict__.items() if v}

    def to_dict_safe(self) -> dict:
        """Return a dict summary with credentials masked for logging."""
        safe: dict = {}
        for k, v in self.__dict__.items():
            if not v:
                continue
            if k in ("uuid", "sid", "pbk"):
                safe[k] = _mask(v)
            else:
                safe[k] = v
        return safe


def _mask(s: str, show: int = 4) -> str:
    """Mask a string, showing only first and last 'show' characters."""
    if len(s) <= show * 2:
        return "*" * len(s)
    return s[:show] + "*" * (len(s) - show * 2) + s[-show:]


def parse_vless_link(url: str) -> VLESSConfig:
    """
    Parse a vless:// share link into a VLESSConfig.

    Format: vless://uuid@host:port?params...#remark

    Args:
        url: Full vless:// URL string.

    Returns:
        VLESSConfig with all parsed parameters.

    Raises:
        ValueError: If the link is not a valid vless:// URL.
    """
    if not url.startswith("vless://"):
        raise ValueError("Not a vless:// link")

    parsed = urlparse(url)
    uuid = parsed.username or ""
    address = parsed.hostname or ""
    port = parsed.port or 443
    remark = unquote(parsed.fragment) if parsed.fragment else ""

    if not uuid or not address:
        raise ValueError("Missing uuid or address in vless link")

    params = parse_qs(parsed.query)

    def _get(qs: dict, key: str, default: str = "") -> str:
        vals = qs.get(key, [])
        return vals[0] if vals else default

    security = _get(params, "security", "none")
    enc = _get(params, "encryption", "none")
    flow = _get(params, "flow", "")
    sni = _get(params, "sni", "")
    fp = _get(params, "fp", "chrome")
    pbk = _get(params, "pbk", "")
    sid = _get(params, "sid", "")
    spx = _get(params, "spx", "")
    network = _get(params, "type", "tcp")
    path_val = _get(params, "path", "")
    host_val = _get(params, "host", "")
    service_name = _get(params, "serviceName", "")
    mode = _get(params, "mode", "")
    authority = _get(params, "authority", "")

    # Determine origin_address: use sni if it's a domain, otherwise use address
    # This is the original hostname preserved for SNI fallback
    origin = sni if sni else address

    return VLESSConfig(
        uuid=uuid,
        address=address,
        port=port,
        encryption=enc,
        security=security,
        flow=flow,
        sni=sni,
        fp=fp,
        pbk=pbk,
        sid=sid,
        spx=spx,
        network=network,
        path=path_val,
        host=host_val,
        service_name=service_name,
        mode=mode,
        remark=remark,
        authority=authority,
        origin_address=origin,
    )


def parse_vmess_link(url: str) -> VLESSConfig:
    """
    Parse a vmess:// share link into a VLESSConfig.

    Format: vmess://base64(json) where JSON contains:
        v, ps, add, port, id, aid, net, type, host, path, tls, sni, fp

    Args:
        url: Full vmess:// URL string.

    Returns:
        VLESSConfig with protocol="vmess".

    Raises:
        ValueError: If the link is not valid.
    """
    if not url.startswith("vmess://"):
        raise ValueError("Not a vmess:// link")

    b64 = url[len("vmess://"):]
    # Handle padding
    padding = 4 - len(b64) % 4
    if padding != 4:
        b64 += "=" * padding

    try:
        raw = base64.b64decode(b64).decode("utf-8")
        data = json.loads(raw)
    except Exception as e:
        raise ValueError(f"Invalid vmess:// link (bad base64 or JSON): {e}") from e

    uuid = data.get("id", "")
    address = data.get("add", "")
    try:
        port = int(data.get("port", "443"))
    except (ValueError, TypeError):
        port = 443
    remark = data.get("ps", "")

    if not uuid or not address:
        raise ValueError("Missing id or add in vmess link")

    network = data.get("net", "tcp")
    security = data.get("tls", "")
    # vmess "tls" field is either "tls" or "reality" or empty
    if security in ("none", ""):
        security = "none"

    try:
        alter_id = int(data.get("aid", 0))
    except (ValueError, TypeError):
        alter_id = 0

    # Map encryption/security for Xray compatibility
    enc = "none"  # vmess uses "security" for encryption in Xray
    scy = data.get("scy", "auto")

    return VLESSConfig(
        protocol="vmess",
        uuid=uuid,
        address=address,
        port=port,
        encryption="auto" if scy == "auto" else scy,
        security=security,
        sni=data.get("sni", ""),
        fp=data.get("fp", "chrome"),
        network=network,
        path=data.get("path", ""),
        host=data.get("host", ""),
        remark=remark,
        origin_address=sni_or_address(data),
        alter_id=alter_id,
    )


def sni_or_address(data: dict) -> str:
    """Get origin address: SNI if domain, otherwise add field."""
    sni = data.get("sni", "")
    if sni:
        return sni
    add = data.get("add", "")
    try:
        from ipaddress import ip_address
        ip_address(add)
        return ""  # IP, no domain SNI
    except ValueError:
        return add


def generate_client_config(cfg: VLESSConfig, socks_port: int = 10808, http_port: int = 10809) -> dict:
    """
    Generate an Xray-core client JSON config from a VLESSConfig.

    Args:
        cfg: Parsed VLESS configuration.
        socks_port: Local SOCKS5 proxy port.
        http_port: Local HTTP proxy port.

    Returns:
        dict suitable for json.dump as Xray-core config.
    """
    outbound = _build_outbound(cfg, cfg.address, "proxy")

    config = {
        "log": {"loglevel": "warning"},
        "inbounds": [
            {
                "tag": "socks",
                "port": socks_port,
                "listen": "127.0.0.1",
                "protocol": "socks",
                "settings": {"udp": True},
            },
            {
                "tag": "http",
                "port": http_port,
                "listen": "127.0.0.1",
                "protocol": "http",
                "settings": {},
            },
        ],
        "outbounds": [
            outbound,
            {"protocol": "freedom", "tag": "direct", "settings": {}},
        ],
        "routing": {
            "domainStrategy": "AsIs",
            "rules": [
                {
                    "type": "field",
                    "inboundTag": ["socks", "http"],
                    "outboundTag": "proxy",
                },
            ],
        },
    }

    return config


def _effective_sni(cfg: VLESSConfig) -> str:
    """Get effective SNI: explicit sni, or origin_address if it's a domain."""
    if cfg.sni:
        return cfg.sni
    # If origin_address looks like a domain (not an IP), use it
    if cfg.origin_address:
        try:
            from ipaddress import ip_address
            ip_address(cfg.origin_address)
            return ""  # It's an IP, no SNI possible
        except ValueError:
            return cfg.origin_address
    return ""


def _build_outbound(cfg: VLESSConfig, address: str, tag: str) -> dict:
    """
    Build a single VLESS outbound entry with a specific address and tag.

    Ensures SNI is preserved: uses cfg.sni or cfg.origin_address as fallback.

    Args:
        cfg: Base VLESS configuration.
        address: Server address for this outbound.
        tag: Outbound tag name.

    Returns:
        Outbound dict for Xray config.
    """
    stream_settings = _build_stream_settings(cfg)
    vnext_user: dict = {"id": cfg.uuid}
    if cfg.protocol == "vless":
        vnext_user["encryption"] = cfg.encryption
    else:
        vnext_user["security"] = cfg.encryption  # vmess uses "security"
        vnext_user["alterId"] = cfg.alter_id
    if cfg.flow:
        vnext_user["flow"] = cfg.flow

    return {
        "protocol": cfg.protocol,
        "settings": {
            "vnext": [{
                "address": address,
                "port": cfg.port,
                "users": [vnext_user],
            }]
        },
        "streamSettings": stream_settings,
        "tag": tag,
    }


def _build_stream_settings(cfg: VLESSConfig) -> dict:
    """Build streamSettings section from VLESSConfig."""
    settings: dict = {"network": cfg.network}
    eff_sni = _effective_sni(cfg)

    if cfg.security not in ("none", ""):
        settings["security"] = cfg.security

    if cfg.security == "reality":
        reality: dict = {}
        if eff_sni:
            reality["serverName"] = eff_sni
        if cfg.fp:
            reality["fingerprint"] = cfg.fp
        if cfg.pbk:
            reality["publicKey"] = cfg.pbk
        if cfg.sid:
            reality["shortId"] = cfg.sid
        if cfg.spx:
            reality["spiderX"] = cfg.spx
        settings["realitySettings"] = reality

    elif cfg.security == "tls":
        tls: dict = {}
        if eff_sni:
            tls["serverName"] = eff_sni
        if cfg.fp:
            tls["fingerprint"] = cfg.fp
        settings["tlsSettings"] = tls

    if cfg.network == "ws":
        ws: dict = {}
        if cfg.path:
            ws["path"] = cfg.path
        if cfg.host:
            ws["headers"] = {"Host": cfg.host}
        settings["wsSettings"] = ws

    elif cfg.network == "grpc":
        grpc: dict = {}
        if cfg.service_name:
            grpc["serviceName"] = cfg.service_name
        if cfg.mode:
            grpc["multiMode"] = cfg.mode == "multi"
        if cfg.authority:
            grpc["authority"] = cfg.authority
        settings["grpcSettings"] = grpc

    elif cfg.network in ("h2", "http"):
        if cfg.host:
            settings["httpSettings"] = {"host": [cfg.host]}
        if cfg.path:
            settings.setdefault("httpSettings", {})["path"] = cfg.path

    elif cfg.network == "tcp":
        tcp: dict = {}
        if cfg.host:
            tcp["header"] = {"type": "http", "request": {"headers": {"Host": [cfg.host]}}}
        settings["tcpSettings"] = tcp if tcp else {}

    return settings


def _build_test_socks_inbound(port: int, outbound_tag: str) -> dict:
    """Build a SOCKS5 inbound that routes to a specific outbound."""
    return {
        "tag": f"test-{outbound_tag}",
        "port": port,
        "listen": "127.0.0.1",
        "protocol": "socks",
        "settings": {"udp": True},
    }


def generate_test_config(
    cfg: VLESSConfig,
    ips: list[str],
    active_ip: str = "",
    test_base_port: int = 11001,
) -> dict:
    """
    Generate a test-only Xray config for concurrent candidate testing.

    Contains ONLY test SOCKS inbounds and candidate outbounds —
    does NOT include 10808/10809 or a proxy outbound (those belong
    to the production xray instance).

    Args:
        cfg: Base VLESS configuration.
        ips: Candidate IP addresses.
        active_ip: Not used in test config (production only).
        test_base_port: Starting port for test SOCKS inbounds.

    Returns:
        Xray config dict for a temporary test instance.
    """
    inbounds: list[dict] = []
    outbounds: list[dict] = []
    routing_rules: list[dict] = []

    for i, ip in enumerate(ips):
        tag = f"candidate-{i + 1}-out"
        test_tag = f"test-candidate-{i + 1}"
        test_port = test_base_port + i

        inbounds.append(_build_test_socks_inbound(test_port, tag))
        outbounds.append(_build_outbound(cfg, ip, tag))
        routing_rules.append({
            "type": "field",
            "inboundTag": [f"test-{tag}"],
            "outboundTag": tag,
        })

    outbounds.append({
        "protocol": "freedom",
        "tag": "direct",
        "settings": {},
    })

    return {
        "log": {"loglevel": "warning"},
        "inbounds": inbounds,
        "outbounds": outbounds,
        "routing": {
            "domainStrategy": "AsIs",
            "rules": routing_rules,
        },
    }


def ensure_server_name(config: dict) -> str:
    """
    Ensure TLS/serverName is set if address is a domain.

    Args:
        config: Xray config dict.

    Returns:
        The current address from the outbound config.
    """
    from ipaddress import ip_address

    outbound = config["outbounds"][0]
    stream = outbound["streamSettings"]
    address = outbound["settings"]["vnext"][0]["address"]
    security = stream.get("security", "")

    try:
        ip_address(address)
        return address
    except ValueError:
        pass

    if security == "reality":
        reality = stream.get("realitySettings", {})
        if not reality.get("serverName"):
            reality["serverName"] = address
            stream["realitySettings"] = reality
    elif security == "tls":
        tls = stream.get("tlsSettings", {})
        if not tls.get("serverName"):
            tls["serverName"] = address
            stream["tlsSettings"] = tls

    return address


def save_config(config: dict, filepath: str) -> None:
    """
    Save config dict as JSON file atomically, with 0600 permissions.

    Writes to tmp file, fsyncs, then atomically renames.
    Prevents half-written config.json on crash or concurrent write.

    Args:
        config: Xray config dict.
        filepath: Output file path.
    """
    tmp_path = filepath + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, filepath)
    try:
        os.chmod(filepath, 0o600)
    except OSError:
        pass


def load_config(filepath: str) -> dict:
    """
    Load config dict from JSON file.

    Args:
        filepath: Path to the config JSON file.

    Returns:
        Parsed config dict.
    """
    with open(filepath, "r", encoding="utf-8") as f:
        return json.load(f)
