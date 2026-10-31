#!/usr/bin/env python3
"""Single source of truth for KeLee Loon→Stash conversion and publishing.

Everything that previously hardcoded a production public host or duplicated
Loon/Stash mapping tables lives here:

* public base / mirror base resolution (one env-var contract for converter,
  page generator, offline validator, cloud updater and manual deploy);
* Loon mock content-type → HTTP Content-Type mapping;
* Loon rule policy → Stash policy mapping (explicit, no blind replacement);
* Stash schema field allow-lists used by the validator;
* script argument wire protocols keyed by upstream script URL;
* content-addressed asset naming helpers shared by converter and publisher;
* integration patches (upstream semantics vs. local configuration conflicts)
  with an explicit, testable reason for every deviation.

This module is import-safe (stdlib only) and has no side effects.
"""
from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from typing import Mapping, Optional
from urllib.parse import urlparse

# ---------------------------------------------------------------------------
# Public base address (single source)
# ---------------------------------------------------------------------------

PUBLIC_BASE_ENV = "KELEE_PUBLIC_BASE"
MIRROR_BASE_ENV = "KELEE_MIRROR_BASE"
PUBLIC_HOST_ENV = "STASH_PUBLIC_HOST"

# There is deliberately no built-in production host. Every publish path must
# resolve an explicit address; local one-off conversion must pass one too.
DEFAULT_PUBLIC_HOST = ""
DEFAULT_PUBLIC_BASE = ""
LEGACY_PUBLIC_HOSTS = ()


class PublicBaseError(ValueError):
    """Raised when the public base is missing, malformed or inconsistent."""


@dataclass(frozen=True)
class PublicBase:
    """Resolved public address plus where it came from (for diagnostics)."""

    base: str
    source: str
    mirror_base: str = ""

    @property
    def host(self) -> str:
        return urlparse(self.base).hostname or ""

    def url(self, path: str) -> str:
        return f"{self.base}/{str(path).lstrip('/')}"

    def mirror_url(self, path: str) -> str:
        return f"{(self.mirror_base or self.base)}/{str(path).lstrip('/')}"


def normalize_base(value: str, *, what: str = "public base") -> str:
    raw = (value or "").strip()
    if not raw:
        raise PublicBaseError(f"{what} is empty")
    parsed = urlparse(raw)
    if parsed.scheme not in ("https", "http"):
        raise PublicBaseError(f"{what} must be an absolute http(s) URL: {raw!r}")
    if not parsed.hostname:
        raise PublicBaseError(f"{what} has no host: {raw!r}")
    if parsed.query or parsed.fragment:
        raise PublicBaseError(f"{what} must not contain query/fragment: {raw!r}")
    if parsed.path and parsed.path != "/":
        raise PublicBaseError(f"{what} must not contain a path: {raw!r}")
    return f"{parsed.scheme}://{parsed.netloc}"


def resolve_public_base(
    explicit: Optional[str] = None,
    *,
    env: Optional[Mapping[str, str]] = None,
    require: bool = False,
    allow_default: bool = True,
) -> PublicBase:
    """Resolve the single public base used for every generated URL.

    Precedence: explicit argument > ``KELEE_PUBLIC_BASE`` > ``STASH_PUBLIC_HOST``.
    ``STASH_PUBLIC_HOST`` is the host-only form injected by the deploy/updater
    environment file; when both are present they must agree.
    """

    environ = os.environ if env is None else env
    base_env = (environ.get(PUBLIC_BASE_ENV) or "").strip()
    host_env = (environ.get(PUBLIC_HOST_ENV) or "").strip()
    mirror_env = (environ.get(MIRROR_BASE_ENV) or "").strip()

    if explicit:
        base = normalize_base(explicit, what="public base")
        source = "argument"
    elif base_env:
        base = normalize_base(base_env, what=PUBLIC_BASE_ENV)
        source = PUBLIC_BASE_ENV
    elif host_env:
        base = normalize_base(f"https://{host_env}", what=PUBLIC_HOST_ENV)
        source = PUBLIC_HOST_ENV
    elif allow_default and DEFAULT_PUBLIC_BASE:
        base = DEFAULT_PUBLIC_BASE
        source = "default"
    else:
        raise PublicBaseError(
            f"{PUBLIC_BASE_ENV} is required (or pass --public-base); "
            "refusing to use an implicit production host"
        )

    if require and source == "default":
        raise PublicBaseError(
            f"{PUBLIC_BASE_ENV} is required for publish builds; refusing the built-in default"
        )

    # Cross-check the host-only environment value, if present.
    if host_env:
        explicit_base = normalize_base(f"https://{host_env}", what=PUBLIC_HOST_ENV)
        if explicit_base != base:
            raise PublicBaseError(
                f"inconsistent public host: {PUBLIC_HOST_ENV}={host_env!r} "
                f"vs {PUBLIC_BASE_ENV}/argument={base!r}"
            )

    if mirror_env:
        mirror = normalize_base(mirror_env, what=MIRROR_BASE_ENV)
    else:
        mirror = base
    return PublicBase(base=base, source=source, mirror_base=mirror)


# ---------------------------------------------------------------------------
# Loon mock content type mapping
# ---------------------------------------------------------------------------

# Official Loon docs: response.body.mock(contentType, ...) accepts the names
# below; Stash mocks instead use a literal HTTP Content-Type header.
MOCK_CONTENT_TYPES: dict[str, str] = {
    "json": "application/json",
    "text": "text/plain",
    "css": "text/css",
    "html": "text/html",
    "javascript": "application/javascript",
    "plain": "text/plain",
    "png": "image/png",
    "gif": "image/gif",
    "jpeg": "image/jpeg",
    "jpg": "image/jpeg",
    "tiff": "image/tiff",
    "svg": "image/svg+xml",
    "mp4": "video/mp4",
    "form-data": "multipart/form-data",
}

# HTTP status codes accepted by Loon `reject(...)` / mock status arguments.
HTTP_STATUS_RE = re.compile(r"^[1-5][0-9]{2}$")


def mock_content_type(loon_type: str) -> str:
    key = (loon_type or "").strip().lower()
    try:
        return MOCK_CONTENT_TYPES[key]
    except KeyError as exc:  # pragma: no cover - exercised via diagnostics
        raise KeyError(f"E_MOCK_CONTENT_TYPE_UNKNOWN: {loon_type!r}") from exc


# ---------------------------------------------------------------------------
# Rule policies
# ---------------------------------------------------------------------------

# Loon plugin rules may only use DIRECT/REJECT*/PROXY. Mapping to the current
# Stash main configuration is explicit so a typo can never silently fall back.
POLICY_MAP: dict[str, str] = {
    "DIRECT": "DIRECT",
    "REJECT": "REJECT",
    "REJECT-DROP": "REJECT-DROP",
    "REJECT-TINYGIF": "REJECT",
    # Loon supports per-rule REJECT-DICT/REJECT-IMG/REJECT-ARRAY/REJECT-200
    # policies. Stash rule policies only expose REJECT / REJECT-DROP, so these
    # degrade to REJECT; the converter records this as an explicit limitation
    # instead of silently changing the policy.
    "REJECT-DICT": "REJECT",
    "REJECT-IMG": "REJECT",
    "REJECT-ARRAY": "REJECT",
    "REJECT-200": "REJECT",
    "PROXY": "Proxy",
    "Proxy": "Proxy",
}

LOON_RULE_REJECT_DEGRADED = {
    "REJECT-DICT",
    "REJECT-IMG",
    "REJECT-ARRAY",
    "REJECT-200",
    "REJECT-TINYGIF",
}

# Stash built-in policies that may legitimately appear in generated output.
STASH_BUILTIN_POLICIES = {"DIRECT", "REJECT", "REJECT-DROP"}


def map_policy(policy: str) -> str:
    raw = (policy or "").strip()
    try:
        return POLICY_MAP[raw]
    except KeyError as exc:
        raise KeyError(f"E_POLICY_UNMAPPED: {raw!r} (known: {sorted(POLICY_MAP)})") from exc


# ---------------------------------------------------------------------------
# Stash override schema allow-lists (verified against stash.wiki 2026-10-25)
# ---------------------------------------------------------------------------

MOCK_FIELDS = {"match", "status-code", "headers", "text", "base64"}
SCRIPT_FIELDS = {
    "match",
    "name",
    "type",
    "require-body",
    "timeout",
    "argument",
    "binary-mode",
    "max-size",
    "engine",
}
SCRIPT_PROVIDER_FIELDS = {"url", "interval", "headers", "path", "payload"}
HEADER_ACTIONS = {
    "request-add",
    "request-del",
    "request-replace",
    "request-replace-regex",
    "response-add",
    "response-del",
    "response-replace",
    "response-replace-regex",
}
URL_REJECT_ACTIONS = {
    "reject",
    "reject-200",
    "reject-img",
    "reject-dict",
    "reject-array",
}
URL_REDIRECT_ACTIONS = {"302", "307", "transparent"}
BODY_JQ_ACTIONS = {"request-jq", "response-jq"}
BODY_JSON_ACTIONS = {
    "request-json-add",
    "response-json-add",
    "request-json-del",
    "response-json-del",
    "request-json-replace",
    "response-json-replace",
}
BODY_REGEX_ACTIONS = {"request-replace-regex", "response-replace-regex"}
BODY_ACTIONS = (
    BODY_JQ_ACTIONS | BODY_JSON_ACTIONS | BODY_REGEX_ACTIONS | {"request-replace", "response-replace"}
)

# Fields emitted by the old converter that Stash does not understand. The
# validator rejects them instead of blessing the previous wrong format.
FORBIDDEN_YAML_FIELDS = {
    "data-type",
    "requires-body",
    "binary-body-mode",
    "requires_body",
    "requiresBody",
    "mock-data-is-base64",
    "statusCode",
    "dataType",
}


# ---------------------------------------------------------------------------
# Script argument wire protocols
# ---------------------------------------------------------------------------

# Key: upstream script URL suffix (regex). Value: protocol identifier.
#  - json-object: script does JSON.parse($argument) and expects an object.
#  - amp-key-value: script splits the string on '&' then '=' (WeatherKit).
#  - none: script does not read $argument (argument can be dropped safely).
# Evidence: `.local/upstream-audit/scripts/*` inspected 2026-10-25.
SCRIPT_ARGUMENT_PROTOCOLS: dict[str, str] = {
    r"/Spotify/Spotify_remove_ads\.js$": "json-object",
    r"/Bilibili/Bilibili_proto_request_kokoryh\.js$": "json-object",
    r"/Bilibili/Bilibili_proto_response_kokoryh\.js$": "json-object",
    r"/Bilibili/Bilibili_json_kokoryh\.js$": "json-object",
    r"/WeatherKit/releases/download/[^/]+/response\.bundle\.js$": "amp-key-value",
    r"/Weixin/Weixin_external_links_unlock\.js$": "amp-key-value",
    r"/Spotify/Spotify_(request|response|Translate_response|External_Lyrics_response)\.js$": "amp-key-value",
}

# Nested runtime assets that are served from our own host and must be
# content-addressed as well (PDD's dynamically loaded webpack chunk).
MIRRORED_RUNTIME_ASSETS: dict[str, str] = {
    "https://kelee.one/Resource/JavaScript/PinDuoDuo/9410-b8806e870a26db7d.js":
        "9410-b8806e870a26db7d.js",
}

# Assets mirrored for legacy installed overrides (not referenced by the
# current provider bundle). Kept content-addressed + legacy alias so an old
# override that still points at the old URL keeps working during retention.
LEGACY_STATIC_ASSETS: dict[str, str] = {
    "sub-store-0.min.js": "https://cdn.jsdelivr.net/gh/sub-store-org/Sub-Store@release/sub-store-0.min.js",
    "sub-store-1.min.js": "https://cdn.jsdelivr.net/gh/sub-store-org/Sub-Store@release/sub-store-1.min.js",
}


def script_argument_protocol(script_url: str) -> Optional[str]:
    for pattern, protocol in SCRIPT_ARGUMENT_PROTOCOLS.items():
        if re.search(pattern, script_url or ""):
            return protocol
    return None


def script_consumes_argument(content: str | bytes) -> bool:
    """True when the mirrored script actually reads ``$argument``."""

    text = content.decode("utf-8", "replace") if isinstance(content, bytes) else content
    return "$argument" in text


# ---------------------------------------------------------------------------
# Content-addressed asset naming
# ---------------------------------------------------------------------------

HASH_HEX_LEN = 12


def sha256_hex(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def slugify_asset_name(logical_name: str) -> str:
    """Reduce an upstream URL basename to a filesystem/URL-safe slug."""

    name = (logical_name or "script.js").strip().replace("\\", "/").rsplit("/", 1)[-1]
    stem, _, suffix = name.rpartition(".")
    if not stem:
        stem, suffix = name, ""
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", stem).strip("-._").lower()
    stem = re.sub(r"-{2,}", "-", stem) or "asset"
    suffix = re.sub(r"[^A-Za-z0-9.]+", "", suffix).lower()
    return f"{stem}.{suffix}" if suffix else stem


def hashed_asset_relpath(content: bytes | str, logical_name: str, *, directory: str = "scripts") -> str:
    digest = sha256_hex(content)[:HASH_HEX_LEN]
    return f"{directory}/{digest}-{slugify_asset_name(logical_name)}"


def legacy_asset_relpath(logical_name: str, *, directory: str = "scripts") -> str:
    return f"{directory}/{slugify_asset_name(logical_name)}"


# ---------------------------------------------------------------------------
# Integration patches (conversion → local Stash main config)
# ---------------------------------------------------------------------------

# Upstream rules that conflict with the local Stash main configuration need an
# explicit, documented resolution instead of a silent drop. Each entry records
# the source plugin, the upstream rule/matcher, the replacement rule and why.
# Loon rule types supported by the converter. Unknown types are a blocking
# diagnostic (never silently emitted into the Stash profile).
KNOWN_LOON_RULE_TYPES = {
    "DOMAIN",
    "DOMAIN-SUFFIX",
    "DOMAIN-KEYWORD",
    "DOMAIN-REGEX",
    "DOMAIN-WILDCARD",
    "USER-AGENT",
    "URL-REGEX",
    "IP-CIDR",
    "IP-CIDR6",
    "IP-ASN",
    "GEOIP",
    "DST-PORT",
    "SRC-PORT",
    "SRC-IP",
    "NETWORK",
    "PROTOCOL",
    "PROCESS-NAME",
    "PROCESS-PATH",
    "SCRIPT",
    "AND",
    "OR",
    "NOT",
}


WECHAT_HTTPDNS_DIRECT_EXCEPTIONS = [
    # `stash-ios.yaml` deliberately lets these three WeChat HTTPDNS domains go
    # DIRECT before the ad rules; upstream Block_HTTPDNS rejects them to force
    # Loon's DNS framework, which has no Stash equivalent and would break
    # WeChat if copied verbatim.
    "DOMAIN,dns.weixin.qq.com,DIRECT",
    "DOMAIN,dns.weixin.qq.com.cn,DIRECT",
    "DOMAIN,aedns.weixin.qq.com,DIRECT",
]
WECHAT_HTTPDNS_REJECTED_UPSTREAM = {
    "DOMAIN,dns.weixin.qq.com,REJECT",
    "DOMAIN,dns.weixin.qq.com.cn,REJECT",
    "DOMAIN,aedns.weixin.qq.com,REJECT",
}

# Extra rules that preempt an upstream response script for the same URL must be
# dropped in favour of the upstream fine-grained handling.
UPSTREAM_CONFLICT_PATCHES = [
    {
        "plugin": "Taobao_remove_ads.lpx",
        "kind": "drop-extra-rewrite",
        "match": r"poplayer\.template\.alibaba\.com",
        "reason": "上游 Taobao_remove_ads.js 响应脚本精细清理 poplayer JSON；额外 reject-dict 会先拦截请求使其失效",
    },
]

# Fair conversion vs. deliberate local enhancement: enhancements are recorded
# in the build manifest separately and must not override upstream behaviour.
ENHANCEMENT_IDS = {
    "quic-udp-mirror": "为 DOMAIN REJECT 规则补充 UDP/443 AND 阻断，强制 QUIC 回退 TCP",
    "pinduoduo-force-http-engine": "拼多多绕过系统代理，定向强制 HTTP 引擎处理两个 MITM 域名",
    "extra-ad-domains": "按 anti-AD/Cats-Team/fmz200 交叉核对补齐上游插件漏掉的广告域名/接口",
    "pinduoduo-quic-fallback": "拼多多 QUIC 回退规则（协议嗅探前的 UDP/443 兜底）",
}
