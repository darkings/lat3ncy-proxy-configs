# Quantumult X 自用配置

这是经过脱敏和整理的 Quantumult X 配置。节点订阅、MITM 请在设备本地单独配置。

配置直链：

`https://raw.githubusercontent.com/darkings/lat3ncy-quantumultx-config/main/quantumultx.conf`

## 主要策略

- 国内服务：微信、国内域名、国内 IP/ASN 与抖音走 `国内服务`，默认优先直连。
- TikTok：国际版 TikTok 继续按 TikTok 分流规则使用独立策略，不与抖音净化混用。
- 知乎：保持原有分流和净化设置。
- DNS：使用 `223.5.5.5` 与 `119.29.29.29`。
- 去广告：使用墨鱼去开屏 2.0，并保留应用专用净化规则；Spotify 使用 app2smile 规则。
- 搜索：启用 Qsearch Safari 搜索重定向。
- Tailscale：保留 `100.64.0.0/10` 排除路由，并将 `*.ts.net` 加入 DNS 排除列表，避免代理接管 Tailscale 网络及 MagicDNS 解析。
- iOS/macOS 更新：启用“iOS系统更新屏蔽@hippiezhu”后会阻止系统更新检查与下载；需要更新系统时，请在 Quantumult X 的资源列表禁用该规则并刷新。
- 拼多多：基于怎么肥事、walala、ZenmoFeiShi 与可莉（KeLee）的规则维护仓库内修订版；过滤首页与订单营销内容，阻断额外底栏组件、推荐接口和遥测域名，并净化扫码取件页。

## 拼多多净化维护

- QX 入口：`rewrites/pinduoduo-cleanup.snippet`
- 扫码取件响应脚本：`rewrites/scripts/pinduoduo-scan-cleanup.js`
- 经审计的取件页分块：`rewrites/vendor/pinduoduo/9410-b8806e870a26db7d.js`
- 分块通过 jsDelivr 引用仓库内固定提交 `93955a63afe561b665d6dab49c9dcc4ea257ceb5`，避免跟随 `main` 漂移；包装脚本中记录了官方、KeLee 上游和仓库文件的 SHA-256。
- 未移植 Loon 的 QUIC 拒绝规则，以及两条依赖 User-Agent 条件的 HTTP IP 上报规则；现有仓库 IP-CIDR 规则保持不变。

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
