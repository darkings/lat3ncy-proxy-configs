# Quantumult X 配置发布

提供分别面向 iPhone/iPad 与 macOS 本机使用的两套 Quantumult X 配置。

## 配置下载

手机版：[导入 `quantumultx.conf`](https://raw.githubusercontent.com/darkings/lat3ncy-quantumultx-config/main/quantumultx.conf)

macOS 版：[导入 `quantumultx-macos.conf`](https://raw.githubusercontent.com/darkings/lat3ncy-quantumultx-config/main/quantumultx-macos.conf)

在 Quantumult X 中使用对应 Raw 链接导入；以后刷新远程配置即可获取发布更新。节点订阅和 MITM 证书由设备本地独立维护，不会随远程配置更新而重新导入或重新安装。

## 手机版说明

面向 iPhone 与 iPad，保留移动应用分流、应用专项净化、系统更新屏蔽、手动任务和移动端 MITM 重写。AWAvenue 为默认广告域名规则，并包含拼多多、抖音、知乎、高德地图、喜马拉雅等移动应用专项处理；中国版抖音直连，国际版 TikTok 使用独立策略。Tailscale 地址与 `*.ts.net` 绕过代理。

## macOS 版说明

面向 Mac 本机客户端，不作为局域网网关。桌面应用策略和 Tailscale 直连优先，定时任务默认关闭，MITM 重写范围比手机版更精简。Cats-Team 是默认广告主规则并每 6 小时检查更新；AWAvenue 保留为默认关闭的轻量备用规则，不建议同时启用两套广告规则。
