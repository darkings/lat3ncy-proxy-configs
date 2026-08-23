# KeLee Loon 插件 → Stash 覆写（自动转换）

> 来源 `https://hub.kelee.one` 列表 `https://hub.kelee.one/list.json`，批量抓取 `https://kelee.one/Tool/Loon/Lpx/*.lpx` 并转换为 Stash `.stoverride`。
> 仅供本地私有使用；仓库 `README` 已说明“不要把第三方 Loon 插件批量转换后公开分发”，请勿在境内平台公开传播。

## 目录结构

```
stash/overrides/kelee/
  ├─ list.json                # hub.kelee.one 原始清单（265 项）
  ├─ *.stoverride             # 265 个转换后覆写，按 kelee.one 文件名命名
  └─ README.md                # 本文件
scripts/convert_kelee_lpx.py  # 抓取 + 转换脚本
```

## 关键发现

- `hub.kelee.one` 页面本身不含 `.lpx` 直链，`list.json` 才是数据源，字段 `lists[].url = "loon://import?plugin=https://kelee.one/Tool/Loon/Lpx/xxx.lpx"`。
- `kelee.one` 受 Cloudflare WAF 保护，普通 `curl` / `fetch` 会返回 `403 Attention Required`。实测必须使用 Loon UA 才能拿到 `200`：

```
User-Agent: Loon/764 CFNetwork/1498.700.1 Darwin/23.6.0 iPhone/17.6.1
```

  用该 UA 配合 `Accept: */*` 即可稳定抓取 `.lpx`、`.js`、`.jq`。
- Stash 覆写与 Loon 插件的映射（已在转换脚本中实现，参考本仓库手写示例 `stash/overrides/pinduoduo-cleanup.stoverride`）：

| Loon | Stash |
|---|---|
| `[Rule]` → `DOMAIN, ... ,REJECT` 等 | `rules: - DOMAIN,...`（逗号去空格，保留 `AND/OR/PROTOCOL/USER-AGENT/IP-ASN/URL-REGEX` 等复杂类型） |
| `hostname = a, b` | `http: mitm: - a` |
| `^pattern reject(-dict/-array/-200/-img)` | `http: url-rewrite: - ^pattern - reject(-dict...)` |
| `^pattern 307 https://target` | `http: url-rewrite: - ^pattern 307 https://target` |
| `mock-response-body data-type=... status-code=... data="..."` | `http: url-rewrite: - ^pattern - mock data-type=...`（Stash 若不支持 `mock` 需手动改为脚本） |
| `response-body-json-jq 'del(...)'` | `http: body-rewrite: - ^pattern response-jq del(...)` |
| `response-body-json-del a b c` | `→ response-jq del(.a, .b, .c)` |
| `response-body-json-replace k v ...` | `→ response-jq .k = v \| ...` |
| `jq-path="https://kelee.one/Resource/JQLang/... .jq"` | 抓取并内联为 `response-jq <jq内容>`（>3000 字符自动截断并注释） |
| `http-response/request ^pattern script-path=URL ... tag=xxx` | `http: script: - match: ^pattern, name: <tag或basename>, type: response/request, require-body, binary-body-mode, timeout, argument` + `script-providers: name: url, interval:86400` |
| `[Argument] key=type, default, ...` + `argument=[{key},...]` / `cron {cron}` | 解析默认值并替换占位符 `{key}` |

## 批量转换用法

### 1. 刷新清单

```bash
# 需带 Loon UA，否则 403
curl -H "User-Agent: Loon/764 CFNetwork/1498.700.1 Darwin/23.6.0" https://hub.kelee.one/list.json -o list.json
# 或
python -c "import urllib.request; req=urllib.request.Request('https://hub.kelee.one/list.json', headers={'User-Agent':'Loon/764 CFNetwork/1498.700.1 Darwin/23.6.0'}); open('list.json','wb').write(urllib.request.urlopen(req).read())"
```

已在 `stash/overrides/kelee/list.json` 保存一份 2026-08-23 快照（265 项）。

### 2. 单个转换

```bash
# 抓取并转换单个
python scripts/convert_kelee_lpx.py --lpx-url https://kelee.one/Tool/Loon/Lpx/PinDuoDuo_remove_ads.lpx --out stash/overrides/kelee/PinDuoDuo_remove_ads.stoverride

# 本地文件转换
python scripts/convert_kelee_lpx.py --lpx-file ./MyPlugin.lpx --out ./MyPlugin.stoverride
```

### 3. 批量

```bash
python scripts/convert_kelee_lpx.py --batch-list stash/overrides/kelee/list.json --batch-out-dir stash/overrides/kelee
# 已转换 265 个，跳过已存在文件可重复执行
```

脚本参数：`--batch-list` 指向 `list.json`，`--batch-out-dir` 默认 `stash/overrides/kelee`，`skip_existing` 默认开启，`fetch_script_fallback` 默认关闭以提速（若需要自动将 `kelee.one/*.js` 替换为其内 `raw.githubusercontent.com` 镜像，可改代码开启）。

## 在 Stash 中使用

1. 将仓库推送后，覆写的 Raw URL 为：

```
https://raw.githubusercontent.com/darkings/lat3ncy-proxy-configs/main/stash/overrides/kelee/<文件名>.stoverride
# 例如拼多多
https://raw.githubusercontent.com/darkings/lat3ncy-proxy-configs/main/stash/overrides/kelee/PinDuoDuo_remove_ads.stoverride
# jsDelivr 镜像
https://cdn.jsdelivr.net/gh/darkings/lat3ncy-proxy-configs@main/stash/overrides/kelee/<文件名>.stoverride
```

2. Stash → 设置 → 覆写 → 右上角 `+` → 粘贴 Raw 链接 → 下载 → 启用。
3. 若覆写含 `mitm`，首次需安装并信任 Stash 的 MITM 证书，`http: mitm` 列表由脚本自动生成。
4. 含脚本的覆写会自动下载 `script-providers`；若下载失败（`kelee.one` 对非 Loon UA 返回 403），可在覆写中为对应 provider 增加：

```yaml
script-providers:
  xxx:
    url: https://kelee.one/Resource/JavaScript/...
    interval: 86400
    headers:
      User-Agent: Loon/764 CFNetwork/1498.700.1 Darwin/23.6.0 iPhone/17.6.1
```

脚本已对 `kelee.one/*.js` 自动尝试解析其首行的 `raw.githubusercontent.com` 真实地址并优先使用（见 `scripts/convert_kelee_lpx.py` 的 `fetch_text` 回退逻辑），GitHub 直链无需伪装 UA。

## 与本仓库手写版的差异

- `stash/overrides/pinduoduo-cleanup.stoverride` 是本仓库精细维护的手写版：合并了 `sdk.1rtb.net` 等追踪域、`volantis3-open` 组件屏蔽、多处 `response-jq del(...)` 以及两个外置 JS（`pinduoduo-homepage-cleanup.js` / `pinduoduo-scan-cleanup.js`）对底栏的兜底清理，MITM 更完整，`script-providers` 指向 `cdn.jsdelivr.net` 的本仓库镜像，无需 Loon UA。
- 自动转换版 `kelee/PinDuoDuo_remove_ads.stoverride` 仅忠实翻译 KeLee 原版（作者 ZenmoFeiShi / 可莉），未包含本仓库的额外底栏 `buffer_bottom_tabs` 过滤和 Vendor Chunk 替换逻辑。若需最干净效果，建议继续使用手写版。

## 已知限制

- `mock-response-body` 在 Stash 中无完全等价语义，脚本暂译为 `url-rewrite: - mock ...`，Stash 若不支持需手动改写为 `reject-dict` 或脚本。
- `USER-AGENT`, `URL-REGEX`, `AND/OR` 等复杂规则在 Stash/Mihomo 中兼容性取决于内核版本，部分老版本可能忽略 `PROTOCOL, QUIC`。
- `Dlabel`, `360` 等含 `header` 重写的插件，Stash 的 `header-rewrite` 语法与 Loon 略有差异，转换后需实测。
- `generic` / `cron` 类型脚本在 Stash 中的 `type` 映射为 `generic` / `cron`，`cron` 字段已用 `Argument` 默认值 `55 23 * * *` 填充。

## 清理与更新

```bash
# 清理自动生成目录（保留手写版）
rm -rf stash/overrides/kelee/*.stoverride

# 重新生成
python scripts/convert_kelee_lpx.py --batch-list stash/overrides/kelee/list.json --batch-out-dir stash/overrides/kelee
```

每次 `hub.kelee.one` 上游更新后，重新抓取 `list.json` 再执行批量即可。
