$ErrorActionPreference = 'Stop'

$repoRoot = Split-Path -Parent $PSScriptRoot
$readme = Get-Content -LiteralPath (Join-Path $repoRoot 'README.md') -Raw -Encoding UTF8

$headings = @(
    '# 自用代理配置',
    '## 配置下载',
    '## Loon iOS 与 macOS',
    '### iOS 配置',
    '### macOS 配置',
    '## Stash iOS 与 Tailscale',
    '## Sparkle Windows',
    '### 配置方法',
    '## 规则维护与校验',
    '## 更新说明'
)
$positions = foreach ($heading in $headings) {
    $match = [regex]::Match($readme, "(?m)^$([regex]::Escape($heading))\s*$")
    if (-not $match.Success) { throw "Missing README heading: $heading" }
    $match.Index
}
for ($i = 1; $i -lt $positions.Count; $i++) {
    if ($positions[$i] -le $positions[$i - 1]) { throw 'README headings are out of order' }
}

$base = 'https://raw.githubusercontent.com/darkings/lat3ncy-proxy-configs/main/'
foreach ($file in @('loon-ios.lcf', 'loon-macos.lcf', 'stash-ios.yaml', 'sparkle-windows-override.yaml')) {
    $url = "$base$file"
    if ($readme -notmatch [regex]::Escape($url)) { throw "Missing download URL: $file" }
    $codeBlock = '(?m)^```text\r?\n{0}\r?\n```\s*$' -f [regex]::Escape($url)
    if ($readme -notmatch $codeBlock) { throw "Download URL must use its own text code block: $file" }
}
$directStashUrl = 'https://cdn.jsdelivr.net/gh/darkings/lat3ncy-proxy-configs@main/stash-ios.yaml'
if ($readme -notmatch [regex]::Escape($directStashUrl)) { throw 'Missing direct Stash jsDelivr URL' }
$directStashCodeBlock = '(?m)^```text\r?\n{0}\r?\n```\s*$' -f [regex]::Escape($directStashUrl)
if ($readme -notmatch $directStashCodeBlock) { throw 'Direct Stash URL must use its own text code block' }
$stashPinduoduoUrl = 'https://stash.ponyo.fun/PinDuoDuo_remove_ads.stoverride'
if ($readme -notmatch [regex]::Escape($stashPinduoduoUrl)) { throw 'Missing direct Stash Pinduoduo override URL' }
$stashProviderUrl = 'https://stash.ponyo.fun/kelee-scripts.stoverride'
if ($readme -notmatch [regex]::Escape($stashProviderUrl)) { throw 'Missing direct Stash script-provider override URL' }

foreach ($removedPath in @(
    'quantumultx.conf',
    'quantumultx-macos.conf',
    'rules/ios-update-block.list',
    'rewrites/pinduoduo-cleanup.snippet',
    'rewrites/scripts/pinduoduo-homepage-cleanup.js'
)) {
    if (Test-Path -LiteralPath (Join-Path $repoRoot $removedPath)) {
        throw "Removed Quantumult X resource still exists: $removedPath"
    }
}
if ($readme -match '(?i)Quantumult|\bQX\b') { throw 'README must not advertise removed Quantumult X configurations' }

foreach ($guidance in @(
    'Loon 配置使用对应 Raw 链接导入',
    '本地添加节点订阅',
    '香港、台湾、日本、新加坡、美国是五个固定地区自动测速组',
    '剩余流量、套餐到期',
    'MITM 证书',
    'macOS 还将 `100.100.100.100/32` 加入 `skip-proxy`',
    'Stash 版要求 iOS 客户端 3.4 或更高版本',
    '创建名称严格为 `ios` 的订阅',
    '`sub.store` 被固定映射到 `127.0.0.1`',
    'Cats-Team AdRules 默认在规则层拦截广告域名',
    'Loon 的 `.lpx` 不能直接写进 Stash YAML',
    '进入“Tailscale 认证”并完成首次登录',
    '`*.ts.net` 没有加入 `fake-ip-filter` 是有意设计',
    '不能作为普通订阅单独激活',
    '不要启用“全局覆写”',
    '关闭 Sparkle 的 DNS 和嗅探接管',
    '嗅探采用保守模式，不改写目标地址',
    '不要把 Windows Raw 地址添加成普通节点订阅',
    '不需要删除或重新导入节点订阅',
    'OneDrive 保留独立规则集，但流量并入 Microsoft 策略'
)) {
    if ($readme -notmatch [regex]::Escape($guidance)) { throw "Missing README guidance: $guidance" }
}
if ($readme -match '(?m)^\s*\|.+\|\s*$') { throw 'README must not contain a comparison table' }

if ($readme -notmatch 'scripts/generate_microsoft_cn\.py') { throw 'README must document Microsoft-CN generation' }
if ($readme -notmatch 'scripts/generate_loon_configs\.py') { throw 'README must document shared Loon generation' }
if ($readme -notmatch 'Zed 与 Apple（含 iCloud）') { throw 'README must document the direct-connect scope' }
if ($readme -notmatch 'KeLee.+403/503') { throw 'README must document KeLee remote-audit warning policy' }

Write-Output 'PASS: Loon, Stash, and Sparkle README validation'
