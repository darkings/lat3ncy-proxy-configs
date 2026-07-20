# Quantumult X 自用配置

这个仓库保存我当前自用的两套 Quantumult X 配置：手机版和 macOS 版。配置根据自己的设备、常用应用和 Tailscale 网络整理，不作为适合所有环境的通用模板。公开内容不包含私人节点订阅、MITM 证书、密码或 Token。

## 配置下载

手机版：[导入 `quantumultx.conf`](https://raw.githubusercontent.com/darkings/lat3ncy-quantumultx-config/main/quantumultx.conf)

macOS 版：[导入 `quantumultx-macos.conf`](https://raw.githubusercontent.com/darkings/lat3ncy-quantumultx-config/main/quantumultx-macos.conf)

首次使用时，在对应设备的 Quantumult X 中通过 Raw 链接导入配置。已经导入过的设备，可使用同一链接刷新或重新导入新版配置。

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

## 更新说明

主配置文件不会实时自动更新。仓库发布新版后，需要在 Quantumult X 中使用对应 Raw 链接刷新或重新导入；远程资源则按照配置中的 `update-interval` 自动检查上游更新。

节点订阅和 MITM 证书由设备本地独立维护，不会写入公开仓库，也不会因为远程规则更新而重新导入或重新安装。重新导入主配置前，如果设备上修改过本地配置，建议先保留旧配置作为备份。
