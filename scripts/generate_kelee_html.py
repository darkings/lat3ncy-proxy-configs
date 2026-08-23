#!/usr/bin/env python3
"""
Generate stash/overrides/kelee/index.html
Reads targets.json / list.json and existing .stoverride to build a searchable page.
Dependencies at top, one-click install to Stash via stash:// scheme + raw URL.
"""
from __future__ import annotations
import json, re, html, datetime, os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
TARGETS_JSON = REPO_ROOT / "stash/overrides/kelee/targets.json"
LIST_JSON = REPO_ROOT / "stash/overrides/kelee/list.json"
OUT_HTML = REPO_ROOT / "stash/overrides/kelee/index.html"
HASHES_JSON = REPO_ROOT / "stash/overrides/kelee/.hashes.json"

# Public base for Stash install; override with KELEE_PUBLIC_BASE when needed.
# Default is self-hosted stash.ponyo.fun (Keli upstream → convert → deploy), GitHub Raw is fallback via env.
PUBLIC_BASE = os.environ.get("KELEE_PUBLIC_BASE", "https://stash.ponyo.fun").rstrip("/")
RAW_BASE = PUBLIC_BASE
JSD_BASE = os.environ.get("KELEE_MIRROR_BASE", "https://stash.ponyo.fun").rstrip("/")

def load_meta():
    # Load list.json for full metadata (icon, desc, author, date, tag)
    list_data = {}
    if LIST_JSON.exists():
        try:
            j = json.loads(LIST_JSON.read_text(encoding="utf-8"))
            for item in j.get("lists", []):
                url = item.get("url","")
                m = re.search(r"/([^/]+\.lpx)", url)
                if m:
                    fname = m.group(1)
                    list_data[fname] = item
        except Exception as e:
            print(f"list.json load failed: {e}")
    targets = json.loads(TARGETS_JSON.read_text(encoding="utf-8"))
    hashes = {}
    if HASHES_JSON.exists():
        try:
            hashes = json.loads(HASHES_JSON.read_text(encoding="utf-8"))
        except:
            hashes = {}
    # For each target, try to read converted stoverride for local date/name fallback
    enriched = []
    for t in targets:
        fname = t["filename"]
        meta = list_data.get(fname, {})
        # stoverride local parsed
        stoverride_path = REPO_ROOT / f"stash/overrides/kelee/{fname.replace('.lpx','.stoverride')}"
        local = {}
        if stoverride_path.exists():
            try:
                import yaml
                data = yaml.safe_load(stoverride_path.read_text(encoding="utf-8"))
                local["local_name"] = data.get("name","")
                local["local_desc"] = data.get("desc","")
                # date from header comment?
                text = stoverride_path.read_text(encoding="utf-8")
                m = re.search(r"^# date: (.+)$", text, re.M)
                if m:
                    local["local_date"] = m.group(1).strip()
            except:
                pass
        # Merge
        entry = {
            "filename": fname,
            "stoverride": fname.replace(".lpx",".stoverride"),
            "name": meta.get("name") or t.get("name") or local.get("local_name",""),
            "desc": meta.get("desc","") or local.get("local_desc",""),
            "icon": meta.get("icon","") or "https://raw.githubusercontent.com/luestr/IconResource/main/Other_icon/120px/Default.png",
            "author": ", ".join([a.get("name","") for a in meta.get("author", [])]) if isinstance(meta.get("author"), list) else str(meta.get("author","")),
            "date": local.get("local_date") or meta.get("date","") or "",
            "tag": meta.get("tag", []),
            "url": f"https://kelee.one/Tool/Loon/Lpx/{fname}",
            "raw": f"{RAW_BASE}/{fname.replace('.lpx','.stoverride')}",
            "jsd": f"{JSD_BASE}/{fname.replace('.lpx','.stoverride')}",
            "dependency": t.get("dependency", False),
            "hash": hashes.get(fname, "")[:8] if hashes.get(fname) else "",
        }
        # Clean desc
        entry["desc"] = entry["desc"].replace("\\n"," ").strip()
        enriched.append(entry)
    # sort: dependency first, then original targets order (already)
    return enriched

def main():
    entries = load_meta()
    # keep order as in targets.json (which already has dependency first)
    now = datetime.datetime.now(datetime.timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M %Z")
    # Generate HTML
    html_entries = []
    for e in entries:
        name = html.escape(e["name"])
        desc = html.escape(e["desc"])
        author = html.escape(e["author"])
        date = html.escape(e["date"])
        icon = html.escape(e["icon"])
        raw = html.escape(e["raw"])
        jsd = html.escape(e["jsd"])
        # stash install scheme: try both stash://install-override and stash://add-override
        # Common working scheme is `stash://install-config?url=` for config, but for override Stash supports `stash://install-override?url=`
        # We provide `stash://add-override?url=` as fallback via JS
        stash_url = f"stash://install-override?url={raw}"
        # For display, tag badges
        tags = ""
        for tag in e["tag"][:3]:
            tags += f'<span class="tag">{html.escape(tag)}</span>'
        dep_badge = '<span class="tag dep">依赖</span>' if e["dependency"] else ""
        # hash badge
        hash_badge = f'<span class="hash" title="{html.escape(e["hash"])}">{html.escape(e["hash"])}</span>' if e["hash"] else ""
        html_entries.append(f"""
        <div class="card" data-name="{name.lower()} {desc.lower()} {author.lower()}">
          <div class="card-head">
            <img class="icon" loading="lazy" src="{icon}" alt="" onerror="this.src='https://raw.githubusercontent.com/luestr/IconResource/main/Other_icon/120px/Default.png'">
            <div class="meta">
              <div class="title">{name} {dep_badge} <span class="date">{date} {hash_badge}</span></div>
              <div class="desc">{desc}</div>
              <div class="author">{author}</div>
              <div class="tags">{tags}</div>
            </div>
          </div>
          <div class="actions">
            <button class="btn primary" onclick="install('{raw}')">一键安装到 Stash</button>
          </div>
        </div>
        """)

    html_content = f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>自用收集</title>
<meta name="color-scheme" content="dark">
<style>
:root{{--bg:#0b0f14;--card:#161b22;--muted:#8b949e;--text:#e6edf3;--accent:#1f6feb;--border:#21262d;--dep:#f85149}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--text);font:14px/1.6 -apple-system,BlinkMacSystemFont,Segoe UI,Roboto,Helvetica,Arial,PingFang SC,Hiragino Sans GB,Microsoft YaHei,sans-serif;overflow-x:hidden}}
a{{color:var(--accent);text-decoration:none}}
.header{{position:sticky;top:0;z-index:10;background:rgba(11,15,20,.85);backdrop-filter:saturate(180%) blur(12px);border-bottom:1px solid var(--border)}}
.header-inner{{width:100%;max-width:1080px;margin:0 auto;padding:16px 20px}}
.h1{{font-size:20px;font-weight:700}} .sub{{color:var(--muted);font-size:13px;margin-top:4px}}
.toolbar{{width:100%;max-width:1080px;margin:14px auto 0;padding:0 20px;display:flex;gap:10px;flex-wrap:wrap;align-items:center}}
.search{{flex:1;min-width:0;background:var(--card);border:1px solid var(--border);border-radius:10px;padding:10px 12px;color:var(--text);outline:none}}
.search:focus{{border-color:var(--accent)}}
.pill{{background:var(--card);border:1px solid var(--border);border-radius:20px;padding:6px 12px;color:var(--muted);cursor:pointer;user-select:none}} .pill.active{{background:var(--accent);color:#fff;border-color:var(--accent)}}
.grid{{width:100%;max-width:1080px;margin:14px auto;padding:0 20px 40px;display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:14px}}
.card{{min-width:0;background:var(--card);border:1px solid var(--border);border-radius:14px;padding:14px;display:flex;flex-direction:column;gap:12px}}
.card-head{{display:flex;gap:12px;min-width:0}}
.icon{{width:56px;height:56px;border-radius:12px;background:#0d1117;object-fit:cover;flex:0 0 56px;border:1px solid var(--border)}}
.meta{{min-width:0;flex:1}}
.title{{min-width:0;font-weight:700;font-size:15px;display:flex;flex-wrap:wrap;gap:6px;align-items:center;overflow-wrap:anywhere}}
.desc{{color:var(--muted);font-size:13px;margin-top:4px;display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden;overflow-wrap:anywhere}}
.author{{color:var(--muted);font-size:12px;margin-top:4px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
.tags{{display:flex;gap:6px;flex-wrap:wrap;margin-top:6px}} .tag{{background:#0d1117;border:1px solid var(--border);border-radius:20px;padding:2px 8px;font-size:11px;color:var(--muted)}} .tag.dep{{color:#fff;background:var(--dep);border-color:var(--dep)}}
.date{{font-size:11px;color:var(--muted);font-weight:400}} .hash{{font-family:ui-monospace,monospace;background:#0d1117;border:1px solid var(--border);border-radius:6px;padding:1px 6px}}
.actions{{display:flex;gap:8px;flex-wrap:wrap;min-width:0}}
.btn{{min-width:0;appearance:none;border:1px solid var(--border);background:#0d1117;color:var(--text);border-radius:10px;padding:8px 12px;font-size:13px;cursor:pointer}} .btn.primary{{background:var(--accent);border-color:var(--accent);color:#fff}} .btn.ghost{{background:transparent}}
.btn:active{{transform:translateY(1px)}}
.footer{{max-width:1080px;margin:0 auto;padding:18px 20px;color:var(--muted);font-size:12px;border-top:1px solid var(--border)}}
.toast{{position:fixed;left:50%;bottom:20px;transform:translateX(-50%);background:#1f2937;color:#fff;padding:8px 14px;border-radius:999px;font-size:13px;opacity:0;pointer-events:none;transition:opacity .2s}} .toast.show{{opacity:1}}
@media (max-width:600px){{
  .header-inner{{padding:14px 16px}}
  .h1{{font-size:18px;line-height:1.35}}
  .sub{{font-size:12px;line-height:1.5;overflow-wrap:anywhere}}
  .toolbar{{padding:0 16px;gap:8px}}
  .search{{flex:1 1 100%;width:100%;font-size:14px}}
  .pill{{flex:1 1 calc(50% - 8px);text-align:center;padding:7px 8px;white-space:nowrap}}
  .grid{{grid-template-columns:minmax(0,1fr);padding:0 16px 32px;gap:12px}}
  .card{{padding:12px;gap:10px}}
  .icon{{width:48px;height:48px;flex-basis:48px}}
  .actions{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px}}
  .actions .btn{{width:100%;padding:8px 6px;text-align:center;white-space:normal}}
  .actions .btn:first-child{{grid-column:1 / -1}}
  .footer{{padding:16px}}
  .footer span[style*="float:right"]{{float:none !important;display:block;margin-top:8px}}
}}
@media (max-width:360px){{
  .header-inner,.toolbar,.grid,.footer{{padding-left:12px;padding-right:12px}}
  .card-head{{gap:8px}}
  .icon{{width:44px;height:44px;flex-basis:44px}}
}}
</style>
</head>
<body>
<div class="header">
  <div class="header-inner">
    <div class="h1">自用收集</div>
    <div class="sub">依赖置顶 · 每天 02:00 自动同步（{now} 更新）· 点击“一键安装”将调用 <code>stash://install-override</code>，需在 iOS 上用 Stash 打开</div>
  </div>
</div>
<div class="toolbar">
  <input id="q" class="search" placeholder="搜索 名称 / 作者 / 描述，例如 哔哩哔哩 / 拼多多 / Spotify">
  <span class="pill active" data-filter="all">全部 19</span>
  <span class="pill" data-filter="dep">依赖 2</span>
  <span class="pill" data-filter="ad">去广告</span>
</div>
<div id="grid" class="grid">
{''.join(html_entries)}
</div>
<div class="footer">
  数据源 <a href="https://hub.kelee.one" target="_blank">hub.kelee.one</a>（<a href="https://hub.kelee.one/list.json" target="_blank">list.json</a>）· 原始 Lpx 需 <code>User-Agent: Loon/...</code> 才能通过 Cloudflare · 本站由 <code>scripts/convert_kelee_lpx.py</code> 自动转换，依赖 <code>Block_HTTPDNS</code> / <code>BlockAdvertisers</code> 已置顶。<br>
  <span style="float:right">Generated {now}</span>
</div>
<div id="toast" class="toast"></div>
<script>
const q=document.getElementById('q'), grid=document.getElementById('grid'), pills=document.querySelectorAll('.pill[data-filter]');
let active='all';
function filter(){{
  const kw=q.value.trim().toLowerCase();
  for(const c of grid.children){{
    const txt=c.getAttribute('data-name')||'';
    const isDep=c.innerHTML.includes('tag dep');
    const matchKw=!kw||txt.includes(kw);
    const matchFilter=active==='all'||(active==='dep'&&isDep)||(active==='ad'&&!isDep);
    c.style.display=(matchKw&&matchFilter)?'':'none';
  }}
}}
q.addEventListener('input',filter);
for(const p of pills){{p.addEventListener('click',()=>{{pills.forEach(x=>x.classList.remove('active'));p.classList.add('active');active=p.dataset.filter;filter();}});}}
function toast(msg){{const t=document.getElementById('toast');t.textContent=msg;t.classList.add('show');setTimeout(()=>t.classList.remove('show'),1600);}}
function install(raw){{const u='stash://install-override?url='+encodeURIComponent(raw);toast('正在调起 Stash…');location.href=u; setTimeout(()=>toast('若未调起，请在 Stash 覆写中粘贴 Raw'),1200);}}
</script>
</body>
</html>
"""
    OUT_HTML.write_text(html_content, encoding="utf-8")
    print(f"generated {OUT_HTML} ({OUT_HTML.stat().st_size} bytes) for {len(entries)} entries")

if __name__ == "__main__":
    main()
