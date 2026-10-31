#!/usr/bin/env python3
"""Render the KeLee Stash override landing page (``index.html``).

The page is a *view* over the generated bundle: it takes every piece of dynamic
data (public base, provider count, dependency count, build fingerprint/date,
per-target metadata) from the bundle manifest produced by
``scripts/check_kelee_update.py`` instead of hard-coding hosts or counts.

Design rules (item 12 "unified public base" + item 9 "publish consistency"):

* the install URL base is resolved once (``--public-base`` > ``KELEE_PUBLIC_BASE``
  > ``kelee_config.resolve_public_base()``); this module has **no** built-in
  production host;
* install buttons point at the stable ``<Name>.stoverride`` / legacy
  ``<slug>.js`` names, never at content-addressed asset paths;
* the dependency/provider badge counts are computed from the bundle;
* the page shows what was built (fingerprint, date, target count);
* :func:`validate_page` asserts the invariants before the file is published, so a
  stale default host or a hard-coded dependency count fails the build.

CLI: ``python scripts/generate_kelee_html.py [--out-dir DIR] [--public-base URL]``

Exit codes: 0 = page written (or ``--check`` passed), 1 = failure.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import html
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:  # allow `import kelee_config` when present
    sys.path.insert(0, str(SCRIPTS_DIR))

DEFAULT_OUT_DIR = REPO_ROOT / "stash" / "overrides" / "kelee"
TARGETS_NAME = "targets.json"
PAGE_NAME = "index.html"
PROVIDER_BUNDLE_NAME = "kelee-scripts.stoverride"
MANIFEST_NAME = ".build-manifest.json"

PUBLIC_BASE_ENV = "KELEE_PUBLIC_BASE"
MIRROR_BASE_ENV = "KELEE_MIRROR_BASE"

# Legacy hosts are never hardcoded here; callers may pass explicit legacy_hosts
# when validating a migration.
DEFAULT_LEGACY_PUBLIC_HOSTS = ()

DEFAULT_ICON = (
    "https://raw.githubusercontent.com/luestr/IconResource/main/Other_icon/120px/Default.png"
)
SCHEME = "stash://install-override?url="


class PageError(RuntimeError):
    """Page generation/validation failure (blocking)."""


# ---------------------------------------------------------------------------
# public base resolution
# ---------------------------------------------------------------------------
def normalize_base(value: str, *, name: str = "public base") -> str:
    base = str(value or "").strip().rstrip("/")
    if not base:
        raise PageError(f"{name} is empty; set --public-base or the {PUBLIC_BASE_ENV} environment variable")
    if "://" not in base:
        raise PageError(f"{name} must be an absolute http(s) URL, got {base!r}")
    scheme = base.split("://", 1)[0].lower()
    if scheme not in ("http", "https"):
        raise PageError(f"{name} must use http or https, got {base!r}")
    if any(ch in base for ch in "?#"):
        raise PageError(f"{name} must not contain a query or fragment, got {base!r}")
    return base


def _config_resolver(name: str):
    """Return ``kelee_config.<name>`` when the shared config module is available."""

    try:
        import kelee_config  # type: ignore
    except Exception:  # pragma: no cover - module is optional during bring-up
        return None
    fn = getattr(kelee_config, name, None)
    return fn if callable(fn) else None


def legacy_public_hosts() -> tuple[str, ...]:
    """Hosts that must never appear in a bundle built for a different base."""

    try:
        import kelee_config  # type: ignore

        hosts = getattr(kelee_config, "LEGACY_PUBLIC_HOSTS", None)
        if hosts:
            return tuple(str(h).strip().lower() for h in hosts if str(h).strip())
    except Exception:
        pass
    return DEFAULT_LEGACY_PUBLIC_HOSTS


def resolve_bases(
    explicit_public: str | None = None,
    explicit_mirror: str | None = None,
    *,
    require: bool = True,
) -> tuple[str, str]:
    """Resolve (public base, mirror base) from the shared config in one place."""

    config_mirror = ""
    if explicit_public is not None and str(explicit_public).strip():
        base = normalize_base(explicit_public)
    else:
        resolver = _config_resolver("resolve_public_base")
        if resolver is not None:
            try:
                resolved = resolver(None, require=require)
            except TypeError:  # looser signature fallback
                resolved = resolver(None)
            except Exception as exc:  # kelee_config.PublicBaseError
                raise PageError(str(exc)) from exc
            config_mirror = str(getattr(resolved, "mirror_base", "") or "")
            base = normalize_base(str(getattr(resolved, "base", resolved)))
        else:
            env_value = os.environ.get(PUBLIC_BASE_ENV, "").strip()
            if not env_value:
                raise PageError(
                    f"no public base configured: pass --public-base or set {PUBLIC_BASE_ENV} "
                    "(no default host is compiled into the generator)"
                )
            base = normalize_base(env_value, name=PUBLIC_BASE_ENV)
    if explicit_mirror is not None and str(explicit_mirror).strip():
        mirror = normalize_base(explicit_mirror, name="mirror base")
    elif os.environ.get(MIRROR_BASE_ENV, "").strip():
        mirror = normalize_base(os.environ[MIRROR_BASE_ENV], name=MIRROR_BASE_ENV)
    elif config_mirror and config_mirror.rstrip("/") != base:
        mirror = normalize_base(config_mirror, name="mirror base")
    else:
        mirror = base
    return base, mirror


def resolve_public_base(explicit: str | None = None, *, require: bool = True) -> str:
    """Resolve the one public base used by every published URL.

    Precedence is delegated to ``kelee_config.resolve_public_base`` (explicit
    argument > ``KELEE_PUBLIC_BASE`` > ``STASH_PUBLIC_HOST``). There is
    intentionally no compiled-in default for publish paths: CI and deploys must
    provide one so a fork cannot silently publish install URLs for someone
    else's domain (``require=True`` rejects the shared default).
    """

    return resolve_bases(explicit, None, require=require)[0]


def resolve_mirror_base(
    explicit: str | None = None,
    *,
    public_base: str | None = None,
    require: bool = True,
) -> str:
    """Resolve the optional mirror base; defaults to the public base."""

    return resolve_bases(public_base, explicit, require=require)[1]


# ---------------------------------------------------------------------------
# bundle loading
# ---------------------------------------------------------------------------
def load_targets(out_dir: Path) -> list[dict]:
    path = Path(out_dir) / TARGETS_NAME
    if not path.is_file():
        raise PageError(f"missing {path}; run the KeLee build before rendering the page")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PageError(f"cannot parse {path}: {exc}") from exc
    if not isinstance(data, list) or not data:
        raise PageError(f"{path} must be a non-empty JSON list")
    targets: list[dict] = []
    for item in data:
        if not isinstance(item, dict) or not item.get("filename"):
            raise PageError(f"{path} contains an invalid target entry: {item!r}")
        targets.append(item)
    return targets


def load_manifest(out_dir: Path) -> dict | None:
    path = Path(out_dir) / MANIFEST_NAME
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def load_provider_info(out_dir: Path, manifest: Mapping[str, Any] | None = None) -> dict:
    """Return provider count + whether the bundle declares hashed asset URLs."""

    path = Path(out_dir) / PROVIDER_BUNDLE_NAME
    providers: Mapping[str, Any] = {}
    if path.is_file():
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            providers = data.get("script-providers") or {}
        except (OSError, yaml.YAMLError):
            providers = {}
    if not providers and manifest:
        providers = {
            str(url): rec
            for url, rec in (manifest.get("resources") or {}).items()
            if isinstance(rec, Mapping) and rec.get("kind") == "script" and rec.get("relpath")
        }
    urls = [str((value or {}).get("url", "")) for value in providers.values() if isinstance(value, Mapping)]
    return {
        "count": len(providers),
        "hashed_urls": sum(1 for u in urls if re.search(r"/scripts/[0-9a-f]{8,}-", u)),
        "urls": urls,
    }


def _meta_from_override(out_dir: Path, stoverride: str) -> dict:
    path = Path(out_dir) / stoverride
    meta: dict[str, str] = {}
    if not path.is_file():
        return meta
    text = path.read_text(encoding="utf-8")
    try:
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError:
        data = {}
    if isinstance(data, dict):
        for key in ("name", "desc", "icon"):
            value = data.get(key)
            if value:
                meta[key] = str(value)
    for comment_key, field in (
        ("name", "name"),
        ("desc", "desc"),
        ("icon", "icon"),
        ("author", "author"),
        ("homepage", "homepage"),
        ("date", "date"),
        ("lpx", "lpx"),
    ):
        match = re.search(rf"^# {re.escape(comment_key)}: (.+)$", text, re.M)
        if match and not meta.get(field):
            meta[field] = match.group(1).strip()
    # Legacy header style: first comment line is the display name.
    first = text.splitlines()[0] if text else ""
    if not meta.get("name") and first.startswith("# ") and ":" not in first[:20]:
        meta["name"] = first[2:].strip()
    return meta


def _load_list_json(out_dir: Path) -> dict[str, dict]:
    """Read an optional upstream ``list.json`` snapshot (metadata only)."""

    for candidate in (Path(out_dir) / "list.json", REPO_ROOT / "stash" / "overrides" / "kelee" / "list.json"):
        if not candidate.is_file():
            continue
        try:
            data = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        result: dict[str, dict] = {}
        for item in data.get("lists", []) if isinstance(data, dict) else []:
            if not isinstance(item, dict):
                continue
            match = re.search(r"/([^/]+\.lpx)", str(item.get("url", "")))
            if match:
                result[match.group(1)] = item
        if result:
            return result
    return {}


def _author_text(value: Any) -> str:
    if isinstance(value, list):
        names = [str(item.get("name", "")) for item in value if isinstance(item, dict)]
        return ", ".join(name for name in names if name)
    return str(value or "")


def _clean_desc(value: Any) -> str:
    return str(value or "").replace("\\n", " ").replace("\n", " ").strip()


def _first(items: Iterable[Any]) -> Any:
    for item in items:
        if item:
            return item
    return ""


def build_entries(
    out_dir: Path,
    targets: Sequence[Mapping[str, Any]],
    manifest: Mapping[str, Any] | None = None,
) -> list[dict]:
    """Merge manifest metadata > upstream list.json > override header > targets.json."""

    out_dir = Path(out_dir)
    list_meta = _load_list_json(out_dir)
    manifest_by_file: dict[str, dict] = {}
    for result in (manifest or {}).get("target_results", []) or []:
        if isinstance(result, Mapping) and result.get("filename"):
            manifest_by_file[str(result["filename"])] = dict(result)

    entries: list[dict] = []
    for target in targets:
        filename = str(target["filename"])
        stoverride = filename[:-4] + ".stoverride" if filename.lower().endswith(".lpx") else filename
        result = manifest_by_file.get(filename, {})
        header = _meta_from_override(out_dir, stoverride)
        upstream = list_meta.get(filename, {})
        name = _first([result.get("name"), upstream.get("name"), target.get("name"), header.get("name"), filename])
        desc = _clean_desc(_first([result.get("desc"), upstream.get("desc"), header.get("desc")]))
        icon = _first([result.get("icon"), header.get("icon"), upstream.get("icon"), DEFAULT_ICON])
        author = _first([result.get("author"), _author_text(upstream.get("author")), header.get("author")])
        date = _first([result.get("source_date"), result.get("date"), header.get("date"), upstream.get("date")])
        source_sha = str(result.get("source_sha256", ""))
        outputs = (manifest or {}).get("outputs") or {}
        output_sha = ""
        if isinstance(outputs, Mapping):
            output_sha = str((outputs.get(stoverride) or {}).get("sha256", ""))
        tags = upstream.get("tag") if isinstance(upstream.get("tag"), list) else []
        entries.append(
            {
                "filename": filename,
                "stoverride": stoverride,
                "name": str(name),
                "desc": desc[:500],
                "icon": str(icon),
                "author": str(author),
                "date": str(date),
                "tag": [str(tag) for tag in tags][:3],
                "dependency": bool(target.get("dependency")),
                "source_sha256": source_sha,
                "output_sha256": output_sha,
                "install_url": "",  # filled in by render_page once the base is known
            }
        )
    return entries


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------
_CSS = """
:root{--bg:#0b0f14;--card:#161b22;--muted:#8b949e;--text:#e6edf3;--accent:#1f6feb;--border:#21262d;--dep:#f85149}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:14px/1.6 -apple-system,BlinkMacSystemFont,Segoe UI,Roboto,Helvetica,Arial,PingFang SC,Hiragino Sans GB,Microsoft YaHei,sans-serif;overflow-x:hidden}
a{color:var(--accent);text-decoration:none}
.header{position:sticky;top:0;z-index:10;background:rgba(11,15,20,.85);backdrop-filter:saturate(180%) blur(12px);border-bottom:1px solid var(--border)}
.header-inner{width:100%;max-width:1080px;margin:0 auto;padding:16px 20px}
.h1{font-size:20px;font-weight:700}
.sub{color:var(--muted);font-size:13px;margin-top:4px;overflow-wrap:anywhere}
.toolbar{width:100%;max-width:1080px;margin:14px auto 0;padding:0 20px;display:flex;gap:10px;flex-wrap:wrap;align-items:center}
.search{flex:1;min-width:0;background:var(--card);border:1px solid var(--border);border-radius:10px;padding:10px 12px;color:var(--text);outline:none}
.search:focus{border-color:var(--accent)}
.pill{background:var(--card);border:1px solid var(--border);border-radius:20px;padding:6px 12px;color:var(--muted);cursor:pointer;user-select:none}
.pill.active{background:var(--accent);color:#fff;border-color:var(--accent)}
.grid{width:100%;max-width:1080px;margin:14px auto;padding:0 20px 40px;display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:14px}
.card{min-width:0;background:var(--card);border:1px solid var(--border);border-radius:14px;padding:14px;display:flex;flex-direction:column;gap:12px}
.card-head{display:flex;gap:12px;min-width:0}
.icon{width:56px;height:56px;border-radius:12px;background:#0d1117;object-fit:cover;flex:0 0 56px;border:1px solid var(--border)}
.meta{min-width:0;flex:1}
.title{min-width:0;font-weight:700;font-size:15px;display:flex;flex-wrap:wrap;gap:6px;align-items:center;overflow-wrap:anywhere}
.desc{color:var(--muted);font-size:13px;margin-top:4px;display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden;overflow-wrap:anywhere}
.author{color:var(--muted);font-size:12px;margin-top:4px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.tags{display:flex;gap:6px;flex-wrap:wrap;margin-top:6px}
.tag{background:#0d1117;border:1px solid var(--border);border-radius:20px;padding:2px 8px;font-size:11px;color:var(--muted)}
.tag.dep{color:#fff;background:var(--dep);border-color:var(--dep)}
.date{font-size:11px;color:var(--muted);font-weight:400}
.hash{font-family:ui-monospace,monospace;background:#0d1117;border:1px solid var(--border);border-radius:6px;padding:1px 6px}
.actions{display:flex;gap:8px;flex-wrap:wrap;min-width:0}
.btn{min-width:0;appearance:none;border:1px solid var(--border);background:#0d1117;color:var(--text);border-radius:10px;padding:8px 12px;font-size:13px;cursor:pointer}
.btn.primary{background:var(--accent);border-color:var(--accent);color:#fff}
.btn:active{transform:translateY(1px)}
.footer{max-width:1080px;margin:0 auto;padding:18px 20px;color:var(--muted);font-size:12px;border-top:1px solid var(--border);overflow-wrap:anywhere}
.toast{position:fixed;left:50%;bottom:20px;transform:translateX(-50%);background:#1f2937;color:#fff;padding:8px 14px;border-radius:999px;font-size:13px;opacity:0;pointer-events:none;transition:opacity .2s}
.toast.show{opacity:1}
@media (max-width:600px){
  .header-inner{padding:14px 16px}
  .h1{font-size:18px;line-height:1.35}
  .sub{font-size:12px;line-height:1.5}
  .toolbar{padding:0 16px;gap:8px}
  .search{flex:1 1 100%;width:100%;font-size:14px}
  .pill{flex:1 1 calc(50% - 8px);text-align:center;padding:7px 8px;white-space:nowrap}
  .grid{grid-template-columns:minmax(0,1fr);padding:0 16px 32px;gap:12px}
  .card{padding:12px;gap:10px}
  .icon{width:48px;height:48px;flex-basis:48px}
  .actions{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px}
  .actions .btn{width:100%;padding:8px 6px;text-align:center;white-space:normal}
  .actions .btn:first-child{grid-column:1 / -1}
  .footer{padding:16px}
}
@media (max-width:360px){
  .header-inner,.toolbar,.grid,.footer{padding-left:12px;padding-right:12px}
  .card-head{gap:8px}
  .icon{width:44px;height:44px;flex-basis:44px}
}
"""

_JS = """
const q=document.getElementById('q'),grid=document.getElementById('grid'),pills=document.querySelectorAll('.pill[data-filter]');
let active='all';
function filter(){
  const kw=q.value.trim().toLowerCase();
  for(const c of grid.children){
    const txt=(c.getAttribute('data-name')||'').toLowerCase();
    const isDep=c.getAttribute('data-dependency')==='1';
    const matchKw=!kw||txt.includes(kw);
    const matchFilter=active==='all'||(active==='dep'&&isDep)||(active==='other'&&!isDep);
    c.style.display=(matchKw&&matchFilter)?'':'none';
  }
}
q.addEventListener('input',filter);
for(const p of pills){p.addEventListener('click',()=>{pills.forEach(x=>x.classList.remove('active'));p.classList.add('active');active=p.dataset.filter;filter();});}
function toast(msg){const t=document.getElementById('toast');t.textContent=msg;t.classList.add('show');setTimeout(()=>t.classList.remove('show'),1600);}
function install(raw){const u='stash://install-override?url='+encodeURIComponent(raw);toast('正在调起 Stash…');location.href=u;setTimeout(()=>toast('若未调起，请在 Stash 覆写中粘贴 Raw 链接'),1200);}
"""


def _format_timestamp(value: str) -> str:
    return value or ""


def render_page(
    *,
    entries: Sequence[Mapping[str, Any]],
    provider_info: Mapping[str, Any],
    public_base: str,
    mirror_base: str | None = None,
    fingerprint: str = "",
    generated_at: str = "",
    totals: Mapping[str, Any] | None = None,
    schema: Any = "",
) -> str:
    """Render the landing page HTML for one bundle."""

    base = normalize_base(public_base)
    mirror = normalize_base(mirror_base) if mirror_base else base
    totals = dict(totals or {})
    dependency_count = sum(1 for entry in entries if entry.get("dependency"))
    # One source of truth for every dependency number on the page: the pinned
    # provider-bundle card is itself a dependency, so "cards behind the 依赖
    # filter" = target dependencies + 1. The header and the pills both use this.
    dependency_cards = dependency_count + 1
    total_cards = len(entries) + 1
    other_cards = len(entries) - dependency_count
    provider_count = int(provider_info.get("count") or 0)
    asset_count = int(totals.get("assets") or 0)
    target_count = int(totals.get("targets") or len(entries))

    cards: list[str] = []
    # Pinned provider bundle card (installed together with any http.script override).
    bundle_install = html.escape(base + "/" + PROVIDER_BUNDLE_NAME)
    cards.append(
        f"""
        <div class="card" data-name="{html.escape('脚本 Provider 合集 kelee-scripts dependency')}" data-dependency="1">
          <div class="card-head">
            <img class="icon" loading="lazy" src="{html.escape(DEFAULT_ICON)}" alt="">
            <div class="meta">
              <div class="title">脚本 Provider 合集 <span class="tag dep">依赖</span></div>
              <div class="desc">{html.escape('KeLee 转换后共用的脚本 provider（%d 个）。启用任何含 http.script 的覆写时必须同时启用本文件。' % provider_count)}</div>
              <div class="author">{html.escape('本仓库自动生成 · 内容寻址脚本 %d 个' % asset_count)}</div>
              <div class="tags"><span class="tag">{html.escape(PROVIDER_BUNDLE_NAME)}</span></div>
            </div>
          </div>
          <div class="actions">
            <button class="btn primary" onclick="install('{bundle_install}')">一键安装到 Stash</button>
          </div>
        </div>
        """.rstrip()
    )

    for entry in entries:
        install_url = base + "/" + str(entry["stoverride"])
        badges = ""
        for tag in entry.get("tag") or []:
            badges += f'<span class="tag">{html.escape(str(tag))}</span>'
        dep_badge = '<span class="tag dep">依赖</span>' if entry.get("dependency") else ""
        short_hash = str(entry.get("output_sha256") or entry.get("source_sha256") or "")[:8]
        hash_badge = (
            f'<span class="hash" title="{html.escape(short_hash)}">{html.escape(short_hash)}</span>'
            if short_hash
            else ""
        )
        date = html.escape(_format_timestamp(str(entry.get("date") or "")))
        cards.append(
            f"""
        <div class="card" data-name="{html.escape((str(entry.get('name','')) + ' ' + str(entry.get('desc','')) + ' ' + str(entry.get('author',''))).lower())}" data-dependency="{1 if entry.get('dependency') else 0}">
          <div class="card-head">
            <img class="icon" loading="lazy" src="{html.escape(str(entry.get('icon') or DEFAULT_ICON))}" alt="" onerror="this.src='{html.escape(DEFAULT_ICON)}'">
            <div class="meta">
              <div class="title">{html.escape(str(entry.get('name','')))} {dep_badge} <span class="date">{date} {hash_badge}</span></div>
              <div class="desc">{html.escape(str(entry.get('desc','')))}</div>
              <div class="author">{html.escape(str(entry.get('author','')))}</div>
              <div class="tags">{badges}</div>
            </div>
          </div>
          <div class="actions">
            <button class="btn primary" onclick="install('{html.escape(install_url)}')">一键安装到 Stash</button>
          </div>
        </div>
        """.rstrip()
        )

    fingerprint_display = html.escape(fingerprint[:12]) if fingerprint else "未提供"
    generated_display = html.escape(generated_at or "")
    mirror_line = ""
    if mirror and mirror != base:
        mirror_line = f" · 镜像 <a href=\"{html.escape(mirror)}/\">{html.escape(mirror)}</a>"
    page = f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>KeLee Stash 覆写</title>
<meta name="color-scheme" content="dark">
<style>{_CSS}</style>
</head>
<body>
<div class="header">
  <div class="header-inner">
    <div class="h1">KeLee Stash 覆写</div>
    <div class="sub">共 {target_count} 个目标 · 依赖 {dependency_cards} 个（含 Provider 合集） · provider {provider_count} 个 · 构建指纹 <code>{fingerprint_display}</code> · {generated_display}</div>
  </div>
</div>
<div class="toolbar">
  <input id="q" class="search" placeholder="搜索 名称 / 作者 / 描述，例如 哔哩哔哩 / 拼多多 / Spotify">
  <span class="pill active" data-filter="all">全部 {total_cards}</span>
  <span class="pill" data-filter="dep">依赖 {dependency_cards}</span>
  <span class="pill" data-filter="other">其他 {other_cards}</span>
</div>
<div id="grid" class="grid">
{''.join(cards)}
</div>
<div class="footer">
  数据源 <a href="https://hub.kelee.one" target="_blank" rel="noopener">hub.kelee.one</a> ·
  本站地址 <a href="{html.escape(base)}/">{html.escape(base)}</a>{mirror_line}<br>
  生成物由 <code>scripts/convert_kelee_lpx.py</code> 自动转换、<code>scripts/check_kelee_update.py</code> 统一构建与校验；
  含 <code>http.script</code> 的覆写需与置顶的脚本 Provider 合集一起启用。内容寻址资源 {asset_count} 个；
  构建指纹 <code>{fingerprint_display}</code>；生成时间 {generated_display}；schema {html.escape(str(schema))}。
  <span style="float:right">Generated {generated_display}</span>
</div>
<div id="toast" class="toast"></div>
<script>{_JS}</script>
</body>
</html>
"""
    return page


# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------
def validate_page(
    page: str,
    *,
    public_base: str,
    fingerprint: str = "",
    provider_count: int | None = None,
    entry_count: int | None = None,
    legacy_hosts: Iterable[str] | None = None,
) -> list[str]:
    """Return a list of blocking problems (empty = valid)."""

    problems: list[str] = []
    base = normalize_base(public_base)
    if not page or "<!doctype html>" not in page.lower():
        problems.append("page is empty or not an HTML document")
        return problems
    if base + "/" not in page:
        problems.append(f"page does not contain the public base {base!r}")
    base_host = base.split("://", 1)[1].split("/", 1)[0].lower()
    for host in legacy_hosts if legacy_hosts is not None else legacy_public_hosts():
        host = str(host).strip().lower()
        if not host or host == base_host:
            continue
        if host in page.lower():
            problems.append(f"page still references the stale default host {host!r}")
    if re.search(r"依赖\s*\d+\s*</span>", page) is None:
        # The dependency badge itself is dynamic; a missing badge means the page
        # was rendered from a stale template.
        if "data-filter=\"dep\"" not in page:
            problems.append("page has no dependency filter badge")
    # The header note and the dependency pill must agree on the same dynamic
    # number (target dependencies + the pinned provider bundle card).
    header_deps = re.search(r"依赖 (\d+) 个（含 Provider 合集）", page)
    pill_deps = re.search(r'data-filter="dep">依赖 (\d+)</span>', page)
    if header_deps and pill_deps and header_deps.group(1) != pill_deps.group(1):
        problems.append(
            "page dependency counts disagree: "
            f"header={header_deps.group(1)} pill={pill_deps.group(1)}"
        )
    if entry_count is not None:
        all_pill = re.search(r'data-filter="all">全部 (\d+)</span>', page)
        if all_pill and int(all_pill.group(1)) != entry_count + 1:
            problems.append(
                f"page '全部' count ({all_pill.group(1)}) != targets + provider bundle ({entry_count + 1})"
            )
    if provider_count is not None and provider_count >= 0:
        if f"provider {provider_count} 个" not in page:
            problems.append(f"page does not show the dynamic provider count ({provider_count})")
    if fingerprint and fingerprint[:12] not in page:
        problems.append("page does not show the build fingerprint")
    if SCHEME not in page:
        problems.append("page has no stash:// install links")
    if entry_count is not None and entry_count > 0 and page.count("一键安装到 Stash") < entry_count:
        problems.append("page is missing install buttons for some targets")
    if "{{" in page or "}}" in page:
        problems.append("page contains an unrendered template brace")
    return problems


# ---------------------------------------------------------------------------
# generation entry points
# ---------------------------------------------------------------------------
def generate_page(
    out_dir: Path | str = DEFAULT_OUT_DIR,
    *,
    public_base: str | None = None,
    mirror_base: str | None = None,
    manifest: Mapping[str, Any] | None = None,
    write: bool = True,
    legacy_hosts: Iterable[str] | None = None,
    require_base: bool = True,
) -> dict:
    """Render + validate ``index.html`` for an existing bundle directory."""

    out_dir = Path(out_dir)
    base, mirror = resolve_bases(public_base, mirror_base, require=require_base)
    if manifest is None:
        manifest = load_manifest(out_dir)
    targets = load_targets(out_dir)
    entries = build_entries(out_dir, targets, manifest)
    provider_info = load_provider_info(out_dir, manifest)
    fingerprint = str((manifest or {}).get("fingerprint", ""))
    generated_at = str((manifest or {}).get("generated_at", ""))
    totals = dict((manifest or {}).get("totals") or {})
    if not totals:
        totals = {"targets": len(targets), "assets": int(provider_info.get("count", 0))}
    page = render_page(
        entries=entries,
        provider_info=provider_info,
        public_base=base,
        mirror_base=mirror_base,
        fingerprint=fingerprint,
        generated_at=generated_at,
        totals=totals,
        schema=(manifest or {}).get("schema", ""),
    )
    problems = validate_page(
        page,
        public_base=base,
        fingerprint=fingerprint,
        provider_count=int(provider_info["count"]),
        entry_count=len(entries),
        legacy_hosts=legacy_hosts,
    )
    if problems:
        raise PageError("; ".join(problems))
    if write:
        path = out_dir / PAGE_NAME
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(page, encoding="utf-8")
        os.replace(tmp, path)
        return {
            "path": str(path),
            "bytes": path.stat().st_size,
            "entries": len(entries),
            "providers": int(provider_info["count"]),
            "fingerprint": fingerprint,
        }
    return {"path": None, "bytes": len(page.encode("utf-8")), "entries": len(entries), "providers": int(provider_info["count"]), "fingerprint": fingerprint}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Render the KeLee Stash override landing page")
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR), help="generated bundle directory")
    parser.add_argument("--public-base", default=None, help=f"public URL base (default: ${PUBLIC_BASE_ENV})")
    parser.add_argument("--mirror-base", default=None, help=f"optional mirror base (default: ${MIRROR_BASE_ENV})")
    parser.add_argument("--check", action="store_true", help="render + validate without writing index.html")
    parser.add_argument(
        "--allow-default-base",
        action="store_true",
        help="allow the shared config default host (local preview only; publish paths must not)",
    )
    args = parser.parse_args(argv)
    try:
        info = generate_page(
            args.out_dir,
            public_base=args.public_base,
            mirror_base=args.mirror_base,
            write=not args.check,
            require_base=not args.allow_default_base,
        )
    except PageError as exc:
        print(f"page generation failed: {exc}", file=sys.stderr)
        return 1
    if args.check:
        print(f"page OK: {info['entries']} entries, {info['providers']} providers, {info['bytes']} bytes")
    else:
        print(f"generated {info['path']} ({info['bytes']} bytes) for {info['entries']} entries, providers={info['providers']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
