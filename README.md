# 自用代理配置

这个仓库保存我当前自用的 Quantumult X 手机版、Quantumult X macOS 版和 Sparkle Windows 版配置。配置按自己的设备、常用应用和 Tailscale 网络维护，不作为适合所有环境的通用模板。公开内容不包含私人节点订阅、MITM 证书、密码或 Token。

## 配置下载

手机版：

```text
https://raw.githubusercontent.com/darkings/lat3ncy-proxy-configs/main/quantumultx.conf
```

macOS 版：

```text
https://raw.githubusercontent.com/darkings/lat3ncy-proxy-configs/main/quantumultx-macos.conf
```

Windows 版：

```text
https://raw.githubusercontent.com/darkings/lat3ncy-proxy-configs/main/sparkle-windows-override.yaml
```

Quantumult X 配置使用对应 Raw 链接导入。Windows YAML 不包含节点，不能作为普通订阅单独激活；它需要在 Sparkle 中作为远程 YAML 覆写绑定到节点订阅或内置 Sub-Store 的 ClashMeta 输出。

## 手机版说明

手机版面向 iPhone 和 iPad 日常使用，保留移动应用分流、应用专项净化、广告拦截、系统更新屏蔽以及手动和定时任务。中国版抖音保持直连，国际版 TikTok 使用独立策略；知乎保持原有处理。AWAvenue 是默认广告域名规则，拼多多、喜马拉雅、高德地图等应用使用各自的专项净化。

手机版完整排除 Tailscale：`100.64.0.0/10` 绕过 Quantumult X，Tailnet IPv4/IPv6、`*.ts.net` 与 `*.tailscale.com` 优先直连并跳过 Fake-IP 和 MITM，避免 Tailnet 服务或控制连接被代理和广告规则接管。

### 手机版默认启用脚本

- B站去广告 · ZenmoFeiShi
- 拼多多净化 · 怎么肥事、walala，本仓库修订
- 墨鱼去开屏 2.0 · ddgksf2013
- 百度贴吧去广告 · app2smile
- 高德地图净化 · ddgksf2013
- 网页广告净化 · fmz200
- 喜马拉雅去广告 · fmz200
- 下厨房去广告 · fmz200
- 知乎去广告 · fmz200
- 美团去广告 · fmz200
- 淘宝去广告 · fmz200
- 京东去广告 · fmz200
- 闲鱼去广告 · fmz200
- WPS 去广告 · fmz200
- 交管 12123 去广告 · fmz200
- 微信公众号文章去广告 · fmz200
- Spotify · app2smile
- 小红书净化 · fmz200
- 抖音轻量净化 · fmz200
- Safari 聚合搜索 · zqzess

## macOS 版说明

macOS 版面向 Mac 本机客户端，不作为局域网网关。它保留桌面常用服务策略和必要的网页重写，并缩小移动应用专项规则与 MITM 范围；定时任务默认关闭，避免与手机重复执行。

Cats-Team AdRules 是 macOS 版默认启用的广告主规则，每 6 小时检查一次更新；AWAvenue 作为默认关闭的轻量备用规则，两套规则不建议同时启用。Tailscale 的 IPv4、IPv6、`*.ts.net` 和控制域名优先直连，系统 DNS 与 IPv6 保持启用。

### macOS 默认启用脚本

- X 网页广告净化 · fmz200
- Safari 聚合搜索 · zqzess

## Windows 版说明

Windows 版用于 Sparkle 的 Mihomo 内核，以单个远程 YAML 覆写叠加在节点订阅或内置 Sub-Store 的 ClashMeta 输出上。覆写负责 DNS、嗅探、策略组和分流规则；端口、系统代理、TUN、局域网访问等客户端托管设置由 Sparkle 界面管理。

### Windows 配置方法

1. 在 Sparkle 设置中启用内置 Sub-Store，并保持“允许局域网连接”关闭；如果不用 Sub-Store，也可以直接导入私人节点订阅。
2. 在 Sub-Store 中添加机场订阅并生成 ClashMeta 输出，然后将该输出添加为 Sparkle 配置。只有在上游订阅无法直连下载时，才启用“为 Sub-Store 内所有请求使用代理”。
3. 打开 Sparkle 的“覆写”页面，粘贴上方 Windows Raw 地址并导入，文件类型选择 YAML。
4. 不要启用“全局覆写”；在目标订阅的设置中只为该订阅绑定此远程覆写。
5. 在 Sparkle 中使用规则模式，开启系统代理，关闭 TUN 和局域网访问；混合端口可设为 `7897`，也可保留 Sparkle 默认值。
6. 关闭 Sparkle 的 DNS 和嗅探接管，让远程覆写中的 Fake-IP、解析服务器、协议嗅探和 Tailscale 排除完整生效。

不要把 Windows Raw 地址添加成普通节点订阅，也不需要导入 JavaScript。内置 Sub-Store 默认只在本机使用；不要开启其局域网访问，也不要把订阅 URL、Token 或 Sub-Store 数据提交到仓库。

单 YAML 完整接管节点订阅的 DNS、嗅探、策略组和分流规则，同时继续使用订阅提供的节点。嗅探采用保守模式，不改写目标地址，并跳过局域网、MagicDNS、Tailscale 控制域名及 Tailnet IPv4/IPv6。配置将这些私有网络和 Windows 联网检测置于规则最前；Cats-Team AdRules 每 6 小时更新，MetaCubeX 私有网络及国内外分流 MRS 每天更新。

Windows 版提供全节点自动测速，以及香港、台湾、日本、新加坡、美国五个地区自动测速组；每组按节点名称筛选并选择最低延迟节点。Proxy 和 OpenAI、GitHub、Microsoft、Steam、Apple、YouTube、Spotify、Telegram 等应用策略组均可直接选择地区自动组，也保留单节点手动选择。OneDrive 保留独立规则集，但流量并入 Microsoft 策略，减少重复的可见策略组。Microsoft、Steam 与 Apple 的中国区下载/CDN 子集优先直连，商店、社区和国际服务进入对应策略组；不加入 TikTok、抖音、拼多多等移动 App 专项规则，也不包含 Quantumult X 式 HTTPS 响应重写。浏览器页面的元素隐藏仍建议交给浏览器内容拦截扩展处理。

Sparkle 的系统代理绕过属于应用设置，不在远程覆写中。建议保留 `100.*`、`*.ts.net` 和 `*.tailscale.com`。如需开启 TUN，应在 Sparkle 中使用 `mixed` 栈、关闭严格路由，并把 `100.64.0.0/10` 和 `fd7a:115c:a1e0::/48` 加入路由排除；确认 WSL 和 Tailnet 均正常后再继续调整。

## 更新说明

Quantumult X 主配置不会实时自动更新。仓库发布新版后，需要使用对应 Raw 链接刷新或重新导入；配置引用的远程规则和脚本按照各自的 `update-interval` 自动检查上游更新。

Windows 版更新时，在 Sparkle 的“覆写”页面点击该远程覆写的刷新按钮，再重新加载目标订阅；不需要删除或重新导入节点订阅。Sub-Store 输出按 Sparkle 中设置的订阅更新周期刷新，Cats-Team 与 MetaCubeX 规则由 Mihomo 按 `interval` 自动更新。

节点订阅和 MITM 证书由设备本地独立维护，不会写入公开仓库。更新这里的配置不会自动上传、替换或公开这些私人内容。
