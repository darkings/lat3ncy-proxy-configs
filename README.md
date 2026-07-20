# 自用代理配置

这个仓库保存我当前自用的 Quantumult X 手机版、Quantumult X macOS 版和 Clash Verge Rev Windows 版配置。配置按自己的设备、常用应用和 Tailscale 网络维护，不作为适合所有环境的通用模板。公开内容不包含私人节点订阅、MITM 证书、密码或 Token。

## 配置下载

- 手机版：[导入 `quantumultx.conf`](https://raw.githubusercontent.com/darkings/lat3ncy-proxy-configs/main/quantumultx.conf)
- macOS 版：[导入 `quantumultx-macos.conf`](https://raw.githubusercontent.com/darkings/lat3ncy-proxy-configs/main/quantumultx-macos.conf)
- Windows 基础配置：[导入 `clash-verge-windows.yaml`](https://raw.githubusercontent.com/darkings/lat3ncy-proxy-configs/main/clash-verge-windows.yaml)
- Windows 扩展脚本：[导入 `clash-verge-windows.js`](https://raw.githubusercontent.com/darkings/lat3ncy-proxy-configs/main/clash-verge-windows.js)

Quantumult X 配置使用对应 Raw 链接导入。Windows 文件不是节点订阅：保留已有节点订阅，分别新建一个远程“扩展配置”和一个远程“扩展脚本”，填入上面的 YAML 与 JavaScript Raw 链接，将两者关联到现有订阅，然后重新激活该订阅。

## 手机版说明

手机版面向 iPhone 和 iPad 日常使用，保留移动应用分流、应用专项净化、广告拦截、系统更新屏蔽以及手动和定时任务。中国版抖音保持直连，国际版 TikTok 使用独立策略；知乎保持原有处理。AWAvenue 是默认广告域名规则，拼多多、喜马拉雅、高德地图等应用使用各自的专项净化。

手机版保留 Tailscale 网络排除：`100.64.0.0/10` 绕过 Quantumult X，`*.ts.net` 不进入 Fake-IP 解析，避免 Tailnet 服务被代理或广告规则接管。

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

Windows 版用于 Clash Verge Rev 的 Mihomo 内核，由远程 YAML 扩展配置与 JavaScript 扩展脚本组成，并叠加在私人节点订阅上。基础配置使用规则模式和 Fake-IP DNS，不开放局域网代理；建议开启系统代理、关闭 TUN，以减少与 WSL、Tailscale 和其他虚拟网络的冲突。

扩展脚本将 `100.64.0.0/10`、`fd7a:115c:a1e0::/48`、`*.ts.net`、Tailscale 控制域名、私有域名/IP 和 Windows 联网检测置于规则最前，并以追加去重方式加入 TUN 路由与 Fake-IP 排除，不覆盖节点订阅原有数组。Cats-Team AdRules 作为桌面端广告主规则，每 6 小时自动更新；MetaCubeX 私有网络 MRS 每天更新。

Windows 版为具有原生 Windows 客户端的 Spotify 和 Telegram 提供独立策略组及 MetaCubeX 域名/IP 规则；不加入 TikTok、抖音、拼多多等移动 App 专项规则，也不包含 Quantumult X 式 HTTPS 响应重写。浏览器页面的元素隐藏仍建议交给浏览器内容拦截扩展处理。

Clash Verge Rev 的“系统代理绕过”属于应用设置，不在 Mihomo 配置文件中。建议保留 `100.*`、`*.ts.net`，并补充 `*.tailscale.com`。如需开启 TUN，先保持 `stack: mixed`、`strict-route: false`，确认 WSL 和 Tailnet 都正常后再调整。

## 更新说明

Quantumult X 主配置不会实时自动更新。仓库发布新版后，需要使用对应 Raw 链接刷新或重新导入；配置引用的远程规则和脚本按照各自的 `update-interval` 自动检查上游更新。

Windows 版更新时，在 Clash Verge Rev 中同时刷新 YAML 扩展配置和 JavaScript 扩展脚本，再重新激活节点订阅即可，不需要删除或重新导入节点订阅。Cats-Team 与 MetaCubeX 规则由 Mihomo 按 `interval` 自动更新。

节点订阅和 MITM 证书由设备本地独立维护，不会写入公开仓库。更新这里的配置不会自动上传、替换或公开这些私人内容。
