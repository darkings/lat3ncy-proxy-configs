# 自用代理配置

这个仓库保存我当前自用的 Loon iOS、Loon macOS 和 Sparkle Windows 配置。配置按自己的设备、常用应用和 Tailscale 网络维护，不作为适合所有环境的通用模板。公开内容不包含私人节点订阅、MITM 证书、密码或 Token。

## 配置下载

Loon iOS：

```text
https://raw.githubusercontent.com/darkings/lat3ncy-proxy-configs/main/loon-ios.lcf
```

Loon macOS：

```text
https://raw.githubusercontent.com/darkings/lat3ncy-proxy-configs/main/loon-macos.lcf
```

Sparkle Windows：

```text
https://raw.githubusercontent.com/darkings/lat3ncy-proxy-configs/main/sparkle-windows-override.yaml
```

Loon 配置使用对应 Raw 链接导入，导入后需在本地添加节点订阅并生成、安装 MITM 证书。Windows YAML 不包含节点，不能作为普通订阅单独激活，需要在 Sparkle 中作为远程 YAML 覆写绑定到节点订阅或内置 Sub-Store 的 ClashMeta 输出。

## Loon iOS 与 macOS

两份配置基于 iKeLee 的 Auto Select 模板定制，并保留原作者和授权说明。公开文件中的节点、远程订阅和证书字段保持为空。

两端均启用 IPv6 和系统 DNS，禁用 STUN 以减少直连 IP 泄漏，并在策略切换时重建现有连接。Tailscale 的 `100.64.0.0/10`、`fd7a:115c:a1e0::/48`、`*.ts.net` 与 `*.tailscale.com` 优先直连。iOS 继续绕过代理和 TUN；macOS 还将 `100.100.100.100/32` 加入 `skip-proxy`，但不放入 `bypass-tun`，避免与 Tailscale 路由冲突。

香港、台湾、日本、新加坡、美国是五个固定地区自动测速组，每 600 秒检测一次。全球和地区节点筛选都会排除订阅状态节点，包括“剩余流量、套餐到期、有效期、已用、重置”以及对应英文名称，防止信息节点进入策略组。

顶层策略顺序为 Proxy、各应用组、Auto 和五个地区组。Proxy 默认使用 Auto，也可切换地区、DIRECT 或单个订阅节点；应用组可选择 Proxy、DIRECT 和地区组。两端策略组均带图标。Microsoft 中国区、Apple 中国区服务优先直连，需代理的服务进入对应策略；macOS 的 Steam 中国区下载/CDN 也优先直连。Microsoft 中国区列表由 `domain-list-community` 中递归解析出的 `@cn` 条目生成，并置于 Microsoft 宽规则之前。

### iOS 配置

iOS 版包含 TikTok、BoxJs、Sub-Store、Script-Hub 等移动端策略和工具，TestFlight 地区解锁保持关闭。默认启用 Apple 天气增强、QQ 链接解锁、Spotify 去广告与歌词增强、哔哩哔哩、高德地图、京东、拼多多、淘宝、微信公众号、微信外部链接、微信小程序、闲鱼、小白智慧打印、喜马拉雅、下厨房、知乎和美团等专项净化插件。

拼多多当前使用 KeLee 的 Loon 原生去广告插件；仓库内原有插件、脚本和 vendor 文件保留作备用，但不在 iOS 配置中加载。

### macOS 配置

macOS 版面向 Mac 本机客户端，不作为局域网网关。它使用 Steam 策略替代移动端 TikTok，只保留广告平台过滤、DNS 泄漏防护、QuickSearch、节点检测和 Sub-Store，减少移动应用专项 MITM 范围。

## Sparkle Windows

Windows 版用于 Sparkle 的 Mihomo 内核，以单个远程 YAML 覆写叠加在节点订阅或内置 Sub-Store 的 ClashMeta 输出上。覆写负责 DNS、嗅探、策略组和分流规则；端口、系统代理、TUN、局域网访问等客户端托管设置由 Sparkle 界面管理。

### 配置方法

1. 在 Sparkle 设置中启用内置 Sub-Store，并保持“允许局域网连接”关闭；如果不用 Sub-Store，也可以直接导入私人节点订阅。
2. 在 Sub-Store 中添加机场订阅并生成 ClashMeta 输出，然后将该输出添加为 Sparkle 配置。只有在上游订阅无法直连下载时，才启用“为 Sub-Store 内所有请求使用代理”。
3. 打开 Sparkle 的“覆写”页面，粘贴上方 Windows Raw 地址并导入，文件类型选择 YAML。
4. 不要启用“全局覆写”；在目标订阅的设置中只为该订阅绑定此远程覆写。
5. 使用规则模式，开启系统代理，关闭 TUN 和局域网访问；混合端口可设为 `7897`，也可保留 Sparkle 默认值。
6. 关闭 Sparkle 的 DNS 和嗅探接管，让远程覆写中的 Fake-IP、解析服务器、协议嗅探和 Tailscale 排除完整生效。

不要把 Windows Raw 地址添加成普通节点订阅，也不需要导入 JavaScript。内置 Sub-Store 默认只在本机使用；不要开启其局域网访问，也不要把订阅 URL、Token 或 Sub-Store 数据提交到仓库。

单 YAML 完整接管节点订阅的 DNS、嗅探、策略组和分流规则，同时继续使用订阅提供的节点。嗅探采用保守模式，不改写目标地址，并跳过局域网、MagicDNS、Tailscale 控制域名及 Tailnet IPv4/IPv6。配置将这些私有网络和 Windows 联网检测置于规则最前；Cats-Team AdRules 每 6 小时更新，MetaCubeX 私有网络及国内外分流 MRS 每天更新。

Windows 版提供全节点自动测速，以及香港、台湾、日本、新加坡、美国五个地区自动测速组。Proxy 保留单节点手动选择；OpenAI、GitHub、Microsoft、Steam、Apple、Google、YouTube、Spotify、Telegram 等应用策略组只提供 Proxy、DIRECT 和地区自动组，避免重复展开全部节点。Google 覆盖搜索、Gmail、Drive、Gemini 等服务；YouTube 使用更具体的独立规则并优先匹配。自动纳入订阅节点的策略组通过统一的 `exclude-filter` 排除流量、到期、客服、公告、频道等信息节点。

OneDrive 保留独立规则集，但流量并入 Microsoft 策略。Microsoft、Steam 与 Apple 的中国区下载/CDN 子集优先直连，商店、社区和国际服务进入对应策略组；Windows 配置不加入 TikTok、抖音、拼多多等移动 App 专项规则，也不包含 HTTPS 响应重写。

Sparkle 的系统代理绕过属于应用设置，不在远程覆写中。建议保留 `100.*`、`*.ts.net` 和 `*.tailscale.com`。如需开启 TUN，应使用 `mixed` 栈、关闭严格路由，并把 `100.64.0.0/10` 和 `fd7a:115c:a1e0::/48` 加入路由排除；确认 WSL 和 Tailnet 均正常后再继续调整。

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

GitHub Actions 会检查 Loon/Sparkle 配置结构、策略与规则顺序、Microsoft-CN 生成结果、拼多多本地回退行为、README、YAML 解析、远程资源和敏感信息。远程审计严格检查 GitHub Raw；KeLee 与 geodata 对通用 HTTP 客户端返回的 403/503 只记录警告。也可以在本地运行 `python scripts/check_remote_resources.py` 进行相同审计。

## 更新说明

Loon 主配置不会实时自动更新。仓库发布新版后，需要使用对应 Raw 链接刷新或重新导入；远程规则和插件由 Loon 的资源更新机制刷新。

Sparkle 更新时，在“覆写”页面点击远程覆写的刷新按钮，再重新加载目标订阅；不需要删除或重新导入节点订阅。Sub-Store 输出按 Sparkle 中设置的订阅更新周期刷新，Cats-Team 与 MetaCubeX 规则由 Mihomo 按 `interval` 自动更新。

节点订阅和 MITM 证书由设备本地独立维护，不会写入公开仓库。更新这里的配置不会自动上传、替换或公开这些私人内容。
