# Quantumult X 自用配置

这是经过脱敏和整理的 Quantumult X 配置。节点订阅、MITM 请在设备本地单独配置。

配置直链：

`https://raw.githubusercontent.com/darkings/lat3ncy-quantumultx-config/main/quantumultx.conf`

macOS 本机专用配置直链：

`https://raw.githubusercontent.com/darkings/lat3ncy-quantumultx-config/main/quantumultx-macos.conf`

## macOS 本机配置

`quantumultx-macos.conf` 是独立的桌面配置，不会替换或修改手机端 `quantumultx.conf`。它面向 Mac 本机使用，不包含局域网网关、设备转发、私人节点订阅、证书、密码或 Token。

- Tailscale：系统 DNS 与 IPv6 保持启用；`cvat` 短主机名和 `*.ts.net` 使用系统解析，`100.64.0.0/10` 绕过 QX，`fd7a:115c:a1e0::/48`、`cvat`、`*.ts.net` 和 `*.tailscale.com` 优先直连。Tailnet 服务不会经过代理策略或广告规则。
- 广告拦截：默认只启用 AWAvenue 基础规则、X 网页广告净化和 Safari Qsearch。anti-AD、Adblock4limbo、Spotify、WPS 与微信文章重写默认关闭，避免规则重复和扩大 MITM 范围。
- 桌面应用：预置 Microsoft、OpenAI、GitHub、Telegram、Twitter、Instagram、YouTube、Netflix、Spotify、Steam 和国际版 TikTok 策略；中国版抖音保持国内服务直连。
- 定时任务：Mac 配置中的 Epic 周免、GitHub 更新监控和 macOS/iOS 限免均默认关闭，避免与手机重复通知；节点详情查询为手动执行。
- 网页元素：QX 的域名拦截能阻止网络广告请求，但不一定能消除页面留白。Safari、Chrome 等桌面浏览器建议另外使用可信内容拦截扩展处理 CSS 元素隐藏，不建议为所有网站开启宽泛 MITM。

Mac 首次使用：

1. 用上述 macOS 直链导入配置。
2. 在 Mac 的 Quantumult X 中单独添加节点订阅。
3. 仅在需要 XWebAds、Qsearch 等 HTTPS 重写时，在 Mac 上生成并信任 MITM 证书；不要从仓库导入证书。
4. 先启动 Tailscale，再启动 QX。修改排除路由后，重新连接两个应用；用短主机名 `cvat`、完整的 `cvat.<tailnet>.ts.net` 或 Tailscale IP 验证直连。
5. 远程分流和重写资源会按各自的 `update-interval` 自动刷新。更新这些资源不需要重装证书或重新导入节点订阅。

## 主要策略

- 国内服务：微信、国内域名、国内 IP/ASN 与抖音走 `国内服务`，默认优先直连。
- TikTok：国际版 TikTok 继续按 TikTok 分流规则使用独立策略，不与抖音净化混用。
- 知乎：保持原有分流和净化设置。
- DNS：使用 `223.5.5.5` 与 `119.29.29.29`。
- 去广告：使用墨鱼去开屏 2.0，并保留应用专用净化规则；Spotify 使用 app2smile 规则。
- 搜索：启用 Qsearch Safari 搜索重定向。
- Tailscale：保留 `100.64.0.0/10` 排除路由，并将 `*.ts.net` 加入 DNS 排除列表，避免代理接管 Tailscale 网络及 MagicDNS 解析。
- iOS/macOS 更新：启用“iOS系统更新屏蔽@hippiezhu”后会阻止系统更新检查与下载；需要更新系统时，请在 Quantumult X 的资源列表禁用该规则并刷新。
- 拼多多：基于怎么肥事、walala、ZenmoFeiShi 与可莉（KeLee）的规则维护仓库内修订版；过滤首页与订单营销内容，阻断额外底栏组件、推荐接口和遥测域名，并净化扫码取件页。首页由仓库内专用响应脚本处理，并归一化压缩与传输响应头。

## 拼多多净化维护

- QX 入口：`rewrites/pinduoduo-cleanup.snippet`
- 首页响应脚本：`rewrites/scripts/pinduoduo-homepage-cleanup.js`
- 扫码取件响应脚本：`rewrites/scripts/pinduoduo-scan-cleanup.js`
- 经审计的取件页分块：`rewrites/vendor/pinduoduo/9410-b8806e870a26db7d.js`
- 分块通过 jsDelivr 引用仓库内固定提交 `93955a63afe561b665d6dab49c9dcc4ea257ceb5`，避免跟随 `main` 漂移；包装脚本中记录了官方、KeLee 上游和仓库文件的 SHA-256。
- QX 配置原有 `udp_drop_list = 443` 继续阻断 QUIC。地址发现服务使用 QX 官方 `url-and-header` 重写，同时匹配裸 IP 的 `/d4`、`/v2/d` 路径与拼多多 `BundleID` 后返回空 404；不再维护会轮换且可能误伤共享云服务的 IP-CIDR。该规则处理明文 HTTP，不增加 MITM 主机名或证书要求。

扫码取件净化依赖拼多多当前分块文件名 `9410-b8806e870a26db7d.js`。拼多多更新网页资源后，如果文件名改变，替换会自动失配而不会阻断正常取件查询，此时需要重新审计并更新仓库资产。

## 使用方法

1. 在 Quantumult X 中导入上面的配置直链。
2. 在 App 内单独添加私人节点订阅，不要把订阅地址写入公开配置。
3. 如需重写功能，在 Quantumult X 内生成 MITM 证书，安装并信任后再启用相关功能。
4. 更新配置前建议备份本机配置

## 定时任务

当前启用：今日油价、节假提醒、Epic 周免（周六 10:00），以及原配置中的手动网络检测任务。

当前禁用：汇率监控、每日一言、今日黄历、钉钉打卡、GitHub 仓库更新监控。禁用项均保留在配置中，去掉行首 `;` 并确认凭据与通知参数后方可启用。

GitHub 更新监控脚本使用 BoxJS 参数：

- `lkGithubMonitorToken`：GitHub Token
- `lkGithubMonitorTgNotifyUrl`：Telegram 通知 URL
- `lkGithubMonitorRepo`：需要监控的仓库

建议监控本仓库 `darkings/lat3ncy-quantumultx-config`。以上凭据只保存在设备或 BoxJS 中，禁止提交到仓库。

## 注意事项

配置引用多个第三方仓库，远程脚本更新后可能发生行为变化。建议定期检查来源和最近提交；涉及会员功能修改、重定向或去广告的规则可能随 App 更新失效，请按需关闭对应远程重写。
