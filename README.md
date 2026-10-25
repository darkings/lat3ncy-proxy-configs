# 自用代理配置

这个仓库保存我当前自用的 Loon iOS、Loon macOS、Stash iOS 和 Stelliberty Windows 配置。配置按自己的设备、常用应用和 Tailscale 网络维护，不作为适合所有环境的通用模板。公开内容不包含私人节点订阅、MITM 证书、密码或 Token。

## 配置下载

Loon iOS：

```text
https://raw.githubusercontent.com/darkings/lat3ncy-proxy-configs/main/loon-ios.lcf
```

Loon macOS：

```text
https://raw.githubusercontent.com/darkings/lat3ncy-proxy-configs/main/loon-macos.lcf
```

Stash iOS：

```text
https://raw.githubusercontent.com/darkings/lat3ncy-proxy-configs/main/stash-ios.yaml
```

Stash iOS 直连镜像（jsDelivr）：

```text
https://cdn.jsdelivr.net/gh/darkings/lat3ncy-proxy-configs@main/stash-ios.yaml
```

Stash 应用覆写（KeLee Loon 插件自动转换）：按需在本地自托管的覆写列表页安装，含 `http.script` 的覆写需与同页置顶的脚本 provider 合集一起启用；转换生成物不发布到本仓库。

KeLee 的 Loon→Stash 转换器和目标清单提交在本仓库；`.stoverride`、转换后的 JavaScript、索引页、构建清单和哈希文件属于生成物，不回写公有 Git 历史，由本机 `deploy-stash.ps1` 或云端 systemd timer 按需生成并发布。转换器按当前 Stash 官方文档映射语义：Mock 使用 `http.mock` 的 `text`/`base64`、`status-code`、`headers` 字段；Loon 脚本镜像为自托管的 `scripts/<内容哈希>-<名称>.js`，provider URL 随内容变化，历史无哈希 URL 保留为需要重新验证的兼容别名；`url.replace`/捕获组 `redirect` 生成最小 request 脚本以保留捕获和 307 语义；远程 jq 先按行去注释再压平并内联。Loon `[Argument]` 的默认值与类型在**构建时固化**（例如 Spotify/Bilibili 脚本收到 JSON 字符串，WeatherKit 收到 `&` 分隔的键值串），Stash 覆写 Beta 的可编辑参数尚未使用，因此用户在 Loon 中改过的非默认值不会保留。脚本 provider 的更新检测同时覆盖 LPX、引用的 JS、远程 jq、拼多多动态 runtime chunk 与转换器源码，不只比较 LPX 哈希。

Stelliberty Windows：

```text
https://raw.githubusercontent.com/darkings/lat3ncy-proxy-configs/main/stelliberty-override.yaml
```

Loon 配置使用对应 Raw 链接导入，导入后需在本地添加节点订阅并生成、安装 MITM 证书。Stash 配置不会保存私人机场 URL，而是从设备本机名为 `ios` 的 Sub-Store 订阅读取节点；Tailscale 使用配置内的原生节点单独完成交互认证，不需要把 `auth-key` 写入文件。Windows YAML 不包含节点，不能作为普通订阅单独激活，需要在 Stelliberty 中作为远程 YAML 覆写绑定到节点订阅或内置 Sub-Store 的 ClashMeta 输出。

## Loon iOS 与 macOS

两份配置基于 iKeLee 的 Auto Select 模板定制，并保留原作者和授权说明。公开文件中的节点、远程订阅和证书字段保持为空。

两端均启用 IPv6 和系统 DNS，禁用 STUN 以减少直连 IP 泄漏，并在策略切换时重建现有连接。Tailscale 的 `100.64.0.0/10`、`fd7a:115c:a1e0::/48`、`*.ts.net` 与 `*.tailscale.com` 优先直连。iOS 继续绕过代理和 TUN；macOS 还将 `100.100.100.100/32` 加入 `skip-proxy`，但不放入 `bypass-tun`，避免与 Tailscale 路由冲突。

香港、台湾、日本、新加坡、美国是五个固定地区自动测速组，每 600 秒检测一次。全球和地区节点筛选都会排除订阅状态节点，包括“剩余流量、套餐到期、有效期、已用、重置”以及对应英文名称，防止信息节点进入策略组。

顶层策略顺序为 Proxy、各应用组、Auto 和五个地区组。Proxy 默认使用 Auto，也可切换地区、DIRECT 或单个订阅节点；应用组可选择 Proxy、DIRECT 和地区组。策略组均带图标；macOS 额外使用独立 Zed 策略处理 `zed.dev` 桌面服务。Microsoft 中国区服务优先直连，需代理的服务进入对应策略；macOS 的 Steam 中国区下载/CDN 也优先直连；Apple（含 iCloud）服务与 Zed 桌面服务整体直连，不设策略组，保证两端同步链路最短。Microsoft 中国区列表由 `domain-list-community` 中递归解析出的 `@cn` 条目生成，并置于 Microsoft 宽规则之前。

### iOS 配置

iOS 版包含 TikTok、BoxJs、Sub-Store、Script-Hub 等移动端策略和工具，TestFlight 地区解锁保持关闭。默认启用 Apple 天气增强、QQ 链接解锁、Spotify 去广告与歌词增强、哔哩哔哩、高德地图、京东、拼多多、淘宝、微信公众号、微信外部链接、微信小程序、闲鱼、小白智慧打印、喜马拉雅、下厨房、知乎和美团等专项净化插件。

拼多多当前使用 KeLee 的 Loon 原生去广告插件；Stash 生成物由云端同步任务按需构建，不写入公有仓库。

### macOS 配置

macOS 版面向 Mac 本机客户端，不作为局域网网关。它使用 Steam 策略替代移动端 TikTok，只保留广告平台过滤、DNS 泄漏防护、QuickSearch、节点检测和 Sub-Store，减少移动应用专项 MITM 范围。

## Stash iOS 与 Tailscale

Stash 版要求 iOS 客户端 3.4 或更高版本，使用名为 `Tailscale` 的策略组包裹 `Tailscale-Node` 原生节点（Tailscale 机器名为 `ios`）。它不依赖同时启动独立的 Tailscale App：`*.ts.net`、`100.64.0.0/10` 和 `fd7a:115c:a1e0::/48` 会进入该策略组，`*.tailscale.com` 登录与控制面则保持直连，避免认证和建链递归。该节点关闭普通公网延迟测试，因为没有指定 exit node；普通互联网流量仍由 Proxy 与各应用策略处理。

Sub-Store 由 Stash 自带（配置页「创建/工具」菜单里的 Sub-Store 条目）。`stash-ios.yaml` 只保留两处引用：`subscribe-url` 与 `SubStore` 远程代理集，都指向 `http://sub.store/download/ios?target=Stash`；**不再声明 `hosts`、`http.mitm`、`http.script` 与 `script-providers`**——自定义宿主映射会把来自带的 `sub.store` 挡到 `127.0.0.1`，前端只会报 `code: 0 Network Error` 并索要后端地址。若自带条目尚未安装，先在「配置 → 创建/工具」里安装 Sub-Store。

导入 `stash-ios.yaml` 后，先启用 Stash 并用 Safari 打开 `https://sub.store`，在本机 Sub-Store 中创建名称严格为 `ios` 的订阅，再把私人机场地址保存在这个订阅中。主配置已内置 `SubStore` 远程代理集，每 24 小时读取 `http://sub.store/download/ios?target=Stash`，并通过 `subscribe-url` 读取同一地址的流量/到期响应头；使用与 Stash 协议及字段最匹配的输出。不要把机场地址填到“通用 → 订阅链接”，也不要把它提交到仓库。首次创建后在“远程代理集”中手动刷新一次 `SubStore`，五个地区组就会自动纳入节点；`Proxy` 只列 `Auto`、`DIRECT` 与五个国家组（要挑单个节点进入对应国家组），`Auto` 则对所有订阅节点做自动测速；两者都不包含 `Tailscale`，避免没有 exit node 时把普通公网流量送入 Tailnet。Tailscale 由独立策略组和 Tailnet 规则使用。其他 Clash 系客户端可单独读取 `http://sub.store/download/ios?target=ClashMeta`，两者仍使用同一个 `ios` 订阅数据。

如果使用名称同为 `ios` 的组合订阅，添加并启用下面的覆写。它只把 `SubStore.url` 和 `subscribe-url` 同步切换到 `http://sub.store/download/collection/ios?target=Stash`，保留策略组、缓存路径和健康检查。单条订阅与组合订阅的下载接口不同，仅把组合订阅命名为 `ios` 不会自动切换。组合流量信息需要在 Sub-Store 中另行设置合并处理，主配置不能自行合并多个机场的流量/到期信息；详见 [Sub-Store 组合订阅文档](https://sub-store-org.github.io/doc/subscription/collection)。

```text
https://raw.githubusercontent.com/darkings/lat3ncy-proxy-configs/main/stash/overrides/substore-ios-collection.stoverride
```

### 按订阅节点生成非空国家组（可选）

`scripts/generate_stash_countries.py` 读取本机导出的私有节点 YAML，按节点名中的国旗/国家别名分类，只为实际有节点的国家生成 `url-test` 组，其余沿用主模板的 DNS、规则与 Tailscale 配置；输出的完整 YAML 由本地导入 Stash，不进仓库。

```text
python scripts/generate_stash_countries.py --nodes .local/nodes.yaml [--collection]
```

节点名或国家变化后需重新运行并重新导入；只刷新 Stash 代理集不会增删国家组。
随后打开代理列表中的 `Tailscale-Node` 节点菜单，进入“Tailscale 认证”并完成首次登录。公开文件故意省略 `auth-key`，也不要把认证密钥、私人订阅或 Tailnet 专属主机名提交到仓库。认证成功后，建议在 Tailscale 管理后台为该 Stash 设备关闭 Key Expiry；如果 Tailnet 使用 IPv6 地址，还需在 Stash“网络设置”中开启 Tunnel IPv6 Routing。没有 exit node 时，不要用 `Tailscale-Node` 访问普通公网 IP；如果确实需要公网出口，请在本地配置可用的 `exit-node`，不要提交到公开配置。

`sub.store` 是自带 Sub-Store 的合成域名（官方也说明它并非真实公网域名，官方前端在 `https://sub-store.vercel.app`），请求由 Stash 内部直接应答，不需要在配置里做任何映射。若首页仍没有流量或过期时间，请在 Sub-Store 的 `ios` 订阅中关闭“不查询订阅流量信息”，并在机场提供独立流量查询地址时填写 `subInfoUrl`/`subInfoUserAgent`；如果源站本身不返回 `Subscription-Userinfo`，Stash 无法凭空显示这些数据。

去广告分为两层：Cats-Team AdRules 默认在规则层拦截广告域名，不需要 MITM；应用响应净化必须使用 Stash 覆写、HTTP Engine 和受信任的 MITM 证书。`stash-ios.yaml` 只声明 Sub-Store 自身的脚本 provider；应用去广告的脚本 provider 随 `kelee-scripts.stoverride` 下发。Stash 的“仅使用 Tunnel 代理”必须关闭，否则多数应用请求不会进入 HTTP Engine；拼多多还会绕过系统 HTTP Proxy，因此其覆写对两个 MITM 域名定向启用了 `force-http-engine`。转换器会为每个首方 App 覆写的域名 REJECT 规则自动生成 UDP/443 的 AND 阻断，强制 QUIC 回退到可解密重写的 TCP（拼多多额外保留已识别 QUIC 的阻断）；转换器还按 anti-AD、Cats-Team、ClydeTime/RuCu6 与 fmz200 模块交叉核对结果补齐上游插件漏掉的广告面——域名层：喜马拉雅（`ad`、`adsebs`、`adbs`、`adwbs`、`dns`、`linkeye`、`location`、`xdcs-collector`）、高德（`optimus-ads`、`adashx`、`awaken`、`adiu`、`amdc` 等）、京东（`ad.3.cn`、`addata`、`ads-union`、`dsp-x`、`jrad`、`jzt`、`bdsp`、`kepler`、`dns.jd.com` 等）、淘宝（阿里妈妈、tanx、直通车 `simba` 全系、百川网关、`taobaoad.com`、`tunion-api` 等）、知乎（`appcloud`、`sugar`、`crash2`）、微信公众号（`ad.weixin.qq.com`）和番茄小说（`applog-hl`、穿山甲 `pangolin-sdk-toutiao.com`、`byteorge`、`iegadp` 等）；接口层：喜马拉雅（`adRealTime`、`ting`、焦点图）、高德（DSP 启动配置、`mps scene`、`city_feed`、`ads_attribution`、足迹/消息）、京东（`getWidgetV1052`）、淘宝（`poplayer` 弹层模板）、闲鱼（搜索激活/发现、`coin`、`resell`、圈子横幅等 6 条）、知乎（`ad-style-service`、`bazaar`/`market` 弹窗、评论头卡、`qa_ad_card`、`brand-ad` 等 17 条）和哔哩哔哩（`api.vc.bilibili.com` 搜索词/话题/动态竖列及番剧相关推荐），主配置规则层也补齐 `adbs`、`adwbs`、`dns.ximalaya.com` 三个缺口并加入喜马拉雅广告域名的 QUIC 阻断。Loon 的 `.lpx` 不能直接写进 Stash YAML；公有仓库只提交转换器和目标清单，21 个精选插件的 Stash 输出由本机部署脚本或云端 timer 生成，转换器保留 URL/JSON body rewrite/脚本/MITM/Mock 语义，并把受 UA 限制的脚本及拼多多动态 vendor chunk 改为自托管的内容哈希地址。额外补丁与上游行为分开记录：淘宝 `poplayer` 不再叠加 `reject-dict`，交由上游响应脚本精细清理；`Block_HTTPDNS` 不再拦截微信三个 HTTPDNS 域名，生成物显式写入 `DIRECT` 例外以匹配主配置；其余按 anti-AD/Cats-Team/fmzy200 交叉核对补充的域名与 QUIC 回退规则属于增强，不覆盖上游同一 URL 的脚本或精细处理。

`*.ts.net` 没有加入 `fake-ip-filter` 是有意设计：Stash 需要保留域名映射，才能把 MagicDNS 名称交给内置 Tailscale 节点。只有 Tailscale 登录与控制域名返回真实 IP 并直连。Cats-Team 广告规则每 6 小时更新，其余 MetaCubeX MRS 规则每天更新，以降低 iOS Network Extension 的规则内存占用。

国内分流按资源缺失场景保留本地兜底：微信/腾讯、抖音常用域名、百度和 Apple/iCloud 核心域名不依赖远程规则即可直连；微信三个 HTTPDNS 域名在广告库之前放行，其余本地业务直连规则在广告库之后，保留域名去广告。GitHub 下载入口使用本地代理规则，避免依赖尚未加载的 GitHub 规则集。其他国内流量依次由 `.cn`、CN-Domain、可解析域名的 CN-IP 和 GEOIP 处理，最后才进入 Proxy；GEOIP 仍需要客户端的 GeoIP 数据库，不能替代全部远程规则。去广告覆写默认前置规则，若覆写重新拦截微信 HTTPDNS，本地例外也可能被覆盖。

DNS 使用两个国内 UDP 服务器并发解析，避免四路重复查询；保持 Fake-IP 和 DNS 默认直连。移除全局 UDP/443 拦截，保留喜马拉雅及应用覆写对广告域名的定向 QUIC 阻断。以上采用 [Stash 官方配置优化建议](https://stash.wiki/en/faq/effective-stash) 和 [规则集缓存机制](https://stash.wiki/en/rules/rule-set)。

## KeLee 覆写转换、构建与发布

### 转换语义与已知限制

转换器（`scripts/convert_kelee_lpx.py` + `scripts/kelee_semantics.py` + `scripts/loon_expr.py`）只做小范围、可复核的语义映射，不删除规则、不截断表达式、不靠放宽括号检查“通过”：

- **Mock**：`response.body.mock(contentType, body[, status[, isBase64]])` 映射为 `http.mock` 的 `text` 或 `base64`、`status-code`、`headers`；内容类型按 Loon 类型映射为对应 HTTP `Content-Type`；二进制 gRPC Mock 同时保留 `grpc-status: 0`（Bilibili 3 条）。
- **Script 参数**：`script(path, {${a}, ${b}})` 的第二参数按脚本真实解析协议编码：Spotify/Bilibili 为 JSON 字符串，WeatherKit 为 `&` 分隔键值串；`enable=${switch}` 按默认状态决定是否生成脚本（默认关闭的脚本不会启用，也不会伪造 `enable` 字段）。
- **捕获改写**：`redirect(307, "${urlMatch.2}")` 生成最小 request 脚本返回 307 + Location；`url.replace` 生成 request URL 修改脚本；两者都只替换正则命中范围并保留未命中部分、编码与查询参数。Stash 官方文档未支持在 `url-rewrite` target 中做捕获替换，因此不猜语法。
- **jq**：远程 jq 与内联 jq 先按行去除 `#` 注释（保留换行），再压平输出；无法获取或括号不配平时阻断发布并给出诊断。真实 jq 语法验证只在本地有 jq 可执行文件时执行，否则明确记为未验证。
- **Header/Body**：`response.header.add` 使用 `http.header-rewrite`；只有当能**证明** header 正则覆盖某个 Mock 的全部 URL（完全相同的正则；或每条顶层分支都完整锚定且可完整枚举有限语言后逐个验证）时才合并进该 Mock 响应头；无法证明时保留独立 header-rewrite 规则，header 仍作用于其匹配的非 Mock 响应，绝不靠有限采样猜测。可证明全覆盖时不再重复输出 header 规则，部分覆盖时两者并存（Mock 内合并 + 普通响应规则）。有限语言证明不接受前导或内联的 `(?m)`/`(?s)` 等无法安全枚举的 flags；`(?i)` 仅在能安全推导大小写语义时参与证明。`response.body.replace` 使用 `response-replace-regex` 并原样保留正则/替换/转义。
- **动态值**：Action 值中的 `${参数}` 按 `[Argument]` 的构建时默认值展开（数字/布尔保留类型）；捕获变量 `${name.n}` 只用于 `url.replace`/`redirect` 模板；未知变量或捕获变量出现在不支持的位置会阻断该目标发布，不会被静默写进生成的 JavaScript 或 Stash 规则。
- **规则与策略**：上游 `PROXY` 显式映射为主线配置的 `Proxy`，`DIRECT`/`REJECT`/`REJECT-DROP` 原样；`AND`/`OR`/`NOT` 保持嵌套结构，策略后的 `no-resolve` 等选项保留。未知策略或语法阻断该目标发布，不静默丢弃。
- **冲突补丁与增强分开记录**：淘宝 `poplayer` 的额外 `reject-dict` 会让上游响应脚本失效，默认丢弃该增强并保留上游脚本；`Block_HTTPDNS` 的三个微信 HTTPDNS REJECT 与主配置 `DIRECT` 例外冲突，生成物改为显式 DIRECT 并记录冲突；QUIC UDP 回退、扩展到广告域名等属于本地增强，构建清单单独标注。

已知限制（这些差异会被构建清单记录，不会伪装成“忠实转换”）：

- Loon `[Argument]` 的**构建时默认值**会固化进 Stash 脚本参数；用户在 Loon 里改过的运行时参数值不会保留（本次未使用 Stash 覆写 Beta 的可编辑参数能力）。
- 条件重写目前只接受单个 `${url} ~= /正则/flags` 条件；`${response.status}`、请求方法、`&&`/`||` 多条件等语法会给出阻断诊断（当前 21 个上游插件未使用）。
- Loon 规则级 `REJECT-DICT`/`REJECT-IMG`/`REJECT-ARRAY`/`REJECT-200` 在 Stash 规则策略中没有等价项，降级为 `REJECT`（拦截效果保留，状态码/响应体不同）。
- 可莉过滤器 HypnotistWS 的正则 `<GetADResult>\.\*\?</GetADResult>` 中量词被转义为字面量，转换器按上游原样保留并给出告警，不擅自改成 `.*?`。
- 尚未在真实 Stash 客户端上完成设备端验证；RE2 兼容性、脚本参数编码、Mock 字节、捕获改写均由离线测试和脚本级 Node 复现验证，客户端仍需人工验收。

### 构建、缓存与更新

生成物目录（Git 忽略）包含 21 个 `.stoverride`、`kelee-scripts.stoverride`、`index.html`、`targets.json`、`scripts/`、`.build-manifest.json` 与旧版 `.hashes.json` 状态。脚本与动态 runtime chunk 使用内容哈希文件名（`scripts/<sha256前12位>-<名称>.js`），新的随机覆盖 URL 不再发生；同时保留无哈希的历史别名，供已安装旧覆写继续访问。Caddy 只对内容哈希路径发送长期 immutable 缓存，历史别名、`.stoverride`、`index.html` 均需要重新验证。

构建指纹由转换器/解析器/校验器/配置源码、`targets.json`、每个 LPX、每个被引用的 JS/jq/runtime 资源、公开地址与清单版本共同计算，没有手工维护的 CONVERTER_VERSION。上游只改 JS/jq、转换器代码改动、产物缺失或损坏、公开地址变化都会触发重建；构建或校验失败时不推进 `.build-manifest.json`/`.hashes.json` 状态，下次可重试。退出码约定：`0` 无变化、`2` 已构建、`1` 失败。

本地命令（需要网络读取上游；离线审计回放使用 `.local/upstream-audit` 且不联网）：

```text
python scripts/check_kelee_update.py --public-base "$KELEE_PUBLIC_BASE"
python scripts/validate_stash.py --dir <输出目录> --targets <输出目录>/targets.json --public-base "$KELEE_PUBLIC_BASE"
python scripts/convert_kelee_lpx.py --lpx-file plugin.lpx --out out.stoverride --public-base "$KELEE_PUBLIC_BASE"
python scripts/convert_kelee_lpx.py --audit-snapshot
```

公共地址只有一个来源：`-PublicHost`/`STASH_PUBLIC_HOST`、`KELEE_PUBLIC_BASE`（必需）、`KELEE_MIRROR_BASE`（可选镜像）必须一致；本机部署会在构建前注入，云端 timer 通过 EnvironmentFile 读取，GitHub 构建要求仓库变量 `KELEE_PUBLIC_BASE`，任何路径都不再依赖写死的生产域名。`RemoteRoot`、更新器根目录、HTTP 健康检查端口、Caddy 站点根目录、保留数量/天数均可配置并在渲染 systemd 单元、EnvironmentFile 与 Caddyfile 时校验；不支持的组合会在动手前报错。

### 发布事务、回滚与 GitHub 职责

- 本机 `deploy-stash.ps1` 与云端 `stash-kelee-update.timer` 共用同一把 `flock` 和同一个 `publish-release.sh` 事务：上传/暂存 → 校验构建清单与文件哈希 → 快照旧配置、旧公共地址与 timer 的 enabled/active 两个独立状态；systemctl 查询的 stdout/stderr/退出码分别保留，只接受明确的“状态文本+退出码”组合，空输出/未知/矛盾状态在首次事务变更前拒绝（不修改 timer/指针/配置/数据） → 切换更新器应用 → copy-forward 内容寻址资产 → 原子移动版本目录 → **锁内暂存/校验/安装** EnvironmentFile、systemd 单元与可选 Caddyfile → 原子切换 `current` → 按实际 Docker/Caddy 重建/重载服务 → 用**新地址与新缓存策略**做健康检查（首页、代表性覆写、provider 合集、内容哈希脚本的字节哈希与内容标记）→ 资产账本/保留策略 → 原子同步权威更新状态（提交点），之后只做非关键记账。失败时恢复旧 `current`、更新器应用指针、旧配置与 timer 的 enabled/active 状态（不混用 `--now`，不意外启动发布前未运行的 timer），重建旧服务并用**旧地址 + 旧配置**复验；任何关键恢复步骤失败返回 `EX_CRITICAL`、记录 `rollback_failed` 并保留配置备份与人工恢复路径，首次部署失败则移除指针而不是留下半套站点。
- 默认部署不强制以“新缓存策略”为健康检查失败条件；安装 Caddyfile（`-InstallCaddyfile`）或显式 `-StrictCacheHeaders` 时才把缓存头不匹配升级为失败，否则只记录警告（旧 Caddyfile 不会因此误阻断发布）。
- 旧版本按 `STASH_KEEP_RELEASES`（默认 3）与 `STASH_KEEP_ASSET_DAYS`（默认 30 天）清理，始终保护当前版本、回滚版本和仍被旧覆写引用的内容哈希脚本；清理前先确认当前版本引用与本机资产账本。
- 手工回滚（先加载 EnvironmentFile，否则助手会因缺少公共地址/路径而拒绝）：`set -a; . <updater>/env; set +a; bash <updater>/current/publish-release.sh --rollback-to <版本号>`；`deploy-stash.ps1 -DryRun` 只校验参数并打印计划（不构建、不联网、不写产物、不执行 ssh/scp），`-BuildOnly` 才进行本地构建，去掉开关执行部署。
- GitHub Actions 只承担**离线验证 + 构建并上传 artifact**：`validate.yml` 做真实 YAML 解析、结构/完整性校验、转换/解析/校验/构建/发布事务测试；`kelee-sync.yml` 分成无网络离线逻辑 job 和上游构建 job，`contents: read`，不上传仓库、不提交生成物、不发布站点。生产发布由本机部署或云端 timer 完成，二者状态一致。

### 真实客户端验收清单（尚未在设备上执行）

以下项目需要在实际 Stash iOS 设备上人工确认，仓库内测试不能替代：

1. 首次安装：导入 `stash-ios.yaml`，安装 Sub-Store 与 `ios` 订阅，刷新 `SubStore` 后 `Proxy`/`Auto`/国家组可用；随后导入覆写页与 `kelee-scripts.stoverride`。
2. DNS：微信三个 HTTPDNS 域名（`dns.weixin.qq.com`、`dns.weixin.qq.com.cn`、`aedns.weixin.qq.com`）在启用 `Block_HTTPDNS` 覆写后仍直连可用；`BlockAdvertisers` 不影响国内基础服务。
3. MITM：安装并信任证书，确认 Stash 走 HTTP Engine；哔哩哔哩 gRPC 页面无广告且不会因 Mock 报错；Spotify 底栏/接力参数按默认值生效。
4. 规则资源缺失：清空/阻断远程规则集后，微信、腾讯、抖音、百度、Apple 核心域名的本地兜底仍可直连，仅丢失依赖规则集的广告拦截。
5. Tailscale：`*.ts.net` 进入 Tailscale 组，`*.tailscale.com` 直连，普通公网流量不进入 Tailnet（无 exit node 时）。
6. 脚本更新：上游脚本更新后重新构建得到新的内容哈希 URL，Stash 刷新 provider 后执行新脚本；旧覆写安装期间旧的哈希 URL 仍可访问。
7. DNS/MITM/覆写层级：确认覆写规则未再次拦截微信 HTTPDNS，`poplayer` 由淘宝响应脚本清理而不是被 404 拒绝。
8. 发布与回滚：在测试部署上验证 `-DryRun` 无副作用、发布失败自动回滚、旧版本页面仍可打开；不得在真实生产环境演练。

### 回归测试命令

```text
python tests/test_loon_expr.py
python tests/test_kelee_conversion.py
python tests/test_validate_stash.py
python tests/test_kelee_build.py
python tests/test_kelee_update_state.py
python tests/test_audit_conversion.py
python tests/test_spotify_argument_repro.py
python tests/test_stash_routing.py
python tests/test_stash_countries.py
pwsh tests/Test-DeployDryRun.ps1
bash tests/test_publish_transaction.sh
python scripts/generate_loon_configs.py --check
```

测试默认离线、确定性：CI 使用 `tests/fixtures/` 的小型合成样本；`.local/upstream-audit` 存在时额外运行 21 个真实快照的审计回放与 Spotify 实际脚本复现，缺失时自动跳过并在输出中注明未验证项。

## Stelliberty Windows

Windows 版用于 Stelliberty 的 Mihomo 内核，以单个远程 YAML 覆写叠加在节点订阅或内置 Sub-Store 的 ClashMeta 输出上。覆写负责 DNS、嗅探、策略组和分流规则；端口、系统代理、TUN、局域网访问等客户端托管设置由 Stelliberty 界面管理。

### 配置方法

1. 在 Stelliberty 设置中启用内置 Sub-Store，并保持“允许局域网连接”关闭；如果不用 Sub-Store，也可以直接导入私人节点订阅。
2. 在 Sub-Store 中添加机场订阅并生成 ClashMeta 输出，然后将该输出添加为 Stelliberty 配置。只有在上游订阅无法直连下载时，才启用“为 Sub-Store 内所有请求使用代理”。
3. 打开 Stelliberty 的“覆写”页面，粘贴上方 Windows Raw 地址并导入，文件类型选择 YAML。
4. 不要启用“全局覆写”；在目标订阅的设置中只为该订阅绑定此远程覆写。
5. 使用规则模式，开启系统代理，关闭 TUN 和局域网访问；混合端口可设为 `7897`，也可保留 Stelliberty 默认值。
6. 关闭 Stelliberty 的 DNS 和嗅探接管，让远程覆写中的 Fake-IP、解析服务器、协议嗅探和 Tailscale 排除完整生效。

不要把 Windows Raw 地址添加成普通节点订阅，也不需要导入 JavaScript。内置 Sub-Store 默认只在本机使用；不要开启其局域网访问，也不要把订阅 URL、Token 或 Sub-Store 数据提交到仓库。

单 YAML 完整接管节点订阅的 DNS、嗅探、策略组和分流规则，同时继续使用订阅提供的节点。嗅探采用保守模式，不改写目标地址，并跳过局域网、MagicDNS、Tailscale 控制域名及 Tailnet IPv4/IPv6。配置将这些私有网络和 Windows 联网检测置于规则最前；Cats-Team AdRules 每 6 小时更新，MetaCubeX 私有网络及国内外分流 MRS 每天更新。

Windows 版的 Proxy 只列 Auto、DIRECT 与五个地区自动测速组（不展开具体节点），Auto 对所有订阅节点做自动测速；OpenAI、GitHub、Microsoft、Steam、Google、YouTube、Spotify、Telegram 等应用策略组只提供 Proxy、DIRECT 和地区自动组，避免重复展开全部节点。Zed 流量进入 Proxy，避免其 Cloudflare 编辑预测请求被直连网络间歇性重置；Apple（含 iCloud）流量完全直连，不经过任何策略组。Apple/iCloud 域名在 `fake-ip-filter` 中返回真实 IP，客户端（含不走系统代理的后台同步组件）直连真实节点，避免假地址中转。BYOK 模型接口继续使用对应服务或最终代理规则。Google 覆盖搜索、Gmail、Drive、Gemini 等服务；YouTube 使用更具体的独立规则并优先匹配。自动纳入订阅节点的策略组通过统一的 `exclude-filter` 排除流量、到期、客服、公告、频道等信息节点。

OneDrive 保留独立规则集，但流量并入 Microsoft 策略。Microsoft、Steam 的中国区下载/CDN 子集优先直连，商店、社区和国际服务进入对应策略组；Apple 全部服务（含 iCloud 同步与 APNs 推送）直连。影视采集站 CDN（lzcdn28/31/32.com 及其 CNAME 域 lzcname.com）使用 GeoDNS 动态边缘节点，直连避免代理出口冷连接拖慢模拟器播放首帧。Windows 配置不加入 TikTok、抖音、拼多多等移动 App 专项规则，也不包含 HTTPS 响应重写。

Stelliberty 的系统代理绕过属于应用设置，不在远程覆写中。建议保留 `100.*`、`*.ts.net` 和 `*.tailscale.com`。如需开启 TUN，应使用 `mixed` 栈、关闭严格路由，并把 `100.64.0.0/10` 和 `fd7a:115c:a1e0::/48` 加入路由排除；确认 WSL 和 Tailnet 均正常后再继续调整。

## 规则维护与校验

两份 Loon 主配置由一个共享模板和平台覆盖数据生成。公共规则、策略与段落应修改 `loon/templates/loon-shared.lcf.tmpl`；iOS/macOS 专属的网络参数、应用组、远程规则和插件应修改 `loon/platforms/ios.json` 或 `loon/platforms/macos.json`。不要直接维护生成后的主配置。修改源文件后运行：

```text
python scripts/generate_loon_configs.py
```

CI 会执行 `--check`，只要 `loon-ios.lcf` 或 `loon-macos.lcf` 与共享模板不一致就会失败。

需要刷新 Loon 的 Microsoft 中国区列表时，在仓库根目录运行：

```text
python scripts/generate_microsoft_cn.py
```

生成器会递归处理 `v2fly/domain-list-community` 的 include，只保留继承或显式标记为 `@cn` 的域名，并输出确定性排序的 `loon/rules/microsoft-cn.list`。更新后应审阅差异，再运行测试；不要直接把整个 Microsoft 列表设为直连。

GitHub Actions 会检查 Loon/Stash/Stelliberty 配置结构、策略与规则顺序、Microsoft-CN 生成结果、拼多多本地回退行为、README、YAML 解析、远程资源和敏感信息；KeLee 工作流还离线运行 Loon 表达式解析、转换语义、校验器、构建指纹/状态、DryRun、发布事务等回归测试，并用真实转换器和离线 fixture 构建一次完整发布包做结构/完整性校验。远程审计严格检查 GitHub Raw；KeLee 与 geodata 对通用 HTTP 客户端返回的 403/503 只记录警告。也可以在本地运行 `python scripts/check_remote_resources.py` 进行相同审计。

## 更新说明

Loon 主配置不会实时自动更新。仓库发布新版后，需要使用对应 Raw 链接刷新或重新导入；远程规则和插件由 Loon 的资源更新机制刷新。

Stash 主配置更新时需要刷新 Raw 配置；设备本地添加的 provider 和 Tailscale 登录状态不应写回公开文件，更新前应确认本地编辑不会被覆盖。MRS 规则由 Stash 按各自的 `interval` 在后台刷新。启动时 Stash 优先读取规则集的本地缓存，缓存不存在或无法解析才下载远程资源；保留现有 `path`，避免无故丢失缓存。导入后应检查远程资源中的 Apple、CN-Domain、CN-IP 已下载并有有效内容。`kelee-scripts.stoverride` 内的脚本 provider 使用内容哈希 URL 与需要重新验证的历史别名，刷新后会自动取到新内容；旧的 `.stoverride` 仍能访问其引用的旧脚本别名。更新 KeLee 覆写时同时刷新 provider 合集，避免脚本与覆写版本错配。单次刷新失败不等于旧缓存必然失效；资源缺失时，本地兜底仅保护列出的服务，不能保证所有网站的完整分流。若“直连”模式正常而“规则”模式异常，在“活跃连接”中检查命中规则；国内业务命中 `MATCH` 并走海外节点时，先刷新缺失规则资源。

Stelliberty 更新时，在“覆写”页面点击远程覆写的刷新按钮，再重新加载目标订阅；不需要删除或重新导入节点订阅。Sub-Store 输出按 Stelliberty 中设置的订阅更新周期刷新，Cats-Team 与 MetaCubeX 规则由 Mihomo 按 `interval` 自动更新。

节点订阅和 MITM 证书由设备本地独立维护，不会写入公开仓库。更新这里的配置不会自动上传、替换或公开这些私人内容。
