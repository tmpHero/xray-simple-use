"""
Parse VLESS share link and generate Xray-core client JSON config.
"""

from dataclasses import dataclass, field
from urllib.parse import urlparse, parse_qs, unquote
from typing import Optional
import json


@dataclass
class VLESSConfig:
    """Parsed VLESS link parameters."""
    uuid: str
    address: str
    port: int
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

    def to_dict(self) -> dict:
        """Return a dict summary of non-empty fields."""
        return {k: v for k, v in self.__dict__.items() if v}


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
    )


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
    stream_settings = _build_stream_settings(cfg)
    vnext_user = {
        "id": cfg.uuid,
        "encryption": cfg.encryption,
    }
    if cfg.flow:
        vnext_user["flow"] = cfg.flow

    outbound = {
        "protocol": "vless",
        "settings": {
            "vnext": [{
                "address": cfg.address,
                "port": cfg.port,
                "users": [vnext_user],
            }]
        },
        "streamSettings": stream_settings,
        "tag": "proxy",
    }

    config = {
        "log": {
            "loglevel": "warning",
        },
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
            {
                "protocol": "freedom",
                "tag": "direct",
                "settings": {},
            },
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


def _build_stream_settings(cfg: VLESSConfig) -> dict:
    """Build streamSettings section from VLESSConfig."""
    settings: dict = {
        "network": cfg.network,
    }

    if cfg.security not in ("none", ""):
        settings["security"] = cfg.security

    # Reality settings
    if cfg.security == "reality":
        reality: dict = {}
        if cfg.sni:
            reality["serverName"] = cfg.sni
        if cfg.fp:
            reality["fingerprint"] = cfg.fp
        if cfg.pbk:
            reality["publicKey"] = cfg.pbk
        if cfg.sid:
            reality["shortId"] = cfg.sid
        if cfg.spx:
            reality["spiderX"] = cfg.spx
        settings["realitySettings"] = reality

    # TLS settings
    if cfg.security == "tls":
        tls: dict = {}
        if cfg.sni:
            tls["serverName"] = cfg.sni
        if cfg.fp:
            tls["fingerprint"] = cfg.fp
        settings["tlsSettings"] = tls

    # Transport-specific settings
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
            if "httpSettings" not in settings:
                settings["httpSettings"] = {}
            settings["httpSettings"]["path"] = cfg.path

    elif cfg.network == "tcp":
        tcp: dict = {}
        if cfg.host:
            tcp["header"] = {"type": "http", "request": {"headers": {"Host": [cfg.host]}}}
        settings["tcpSettings"] = tcp if tcp else {}

    return settings


def save_config(config: dict, filepath: str) -> None:
    """Save config dict as JSON file.

    Args:
        config: Xray config dict.
        filepath: Output file path.
    """
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)


def load_config(filepath: str) -> dict:
    """Load config dict from JSON file.

    Args:
        filepath: Path to the config JSON file.

    Returns:
        Parsed config dict.
    """
    with open(filepath, "r", encoding="utf-8") as f:
        return json.load(f)
