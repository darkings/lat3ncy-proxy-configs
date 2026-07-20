$ErrorActionPreference = 'Stop'

$repoRoot = Split-Path -Parent $PSScriptRoot
$readme = Get-Content -LiteralPath (Join-Path $repoRoot 'README.md') -Raw -Encoding UTF8

$headings = @(
    '# 自用代理配置',
    '## 配置下载',
    '## 手机版说明',
    '### 手机版默认启用脚本',
    '## macOS 版说明',
    '### macOS 默认启用脚本',
    '## Windows 版说明',
    '### Windows 配置方法',
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
foreach ($file in @('quantumultx.conf', 'quantumultx-macos.conf', 'sparkle-windows-override.yaml')) {
    $url = "$base$file"
    if ($readme -notmatch [regex]::Escape($url)) { throw "Missing download URL: $file" }
    $codeBlock = '(?m)^```text\r?\n{0}\r?\n```\s*$' -f [regex]::Escape($url)
    if ($readme -notmatch $codeBlock) { throw "Download URL must use its own text code block: $file" }
}

if ($readme -notmatch '自用.+Quantumult X 手机版.+macOS.+Sparkle Windows') { throw 'Missing self-use cross-platform positioning' }
if ($readme -notmatch 'Windows YAML 不包含节点') { throw 'Missing Windows extension import guidance' }
if ($readme -notmatch '不能作为普通订阅单独激活') { throw 'Missing Windows YAML distinction' }
if ($readme -notmatch '覆写.+Windows Raw 地址.+文件类型选择 YAML') { throw 'Missing Sparkle remote override import steps' }
if ($readme -notmatch '不要启用“全局覆写”') { throw 'Missing profile-scoped override guidance' }
if ($readme -notmatch '关闭 Sparkle 的 DNS 和嗅探接管') { throw 'Missing Sparkle DNS and sniff ownership guidance' }
if ($readme -notmatch 'Sub-Store.+允许局域网连接.+关闭') { throw 'Missing local-only Sub-Store guidance' }
if ($readme -notmatch '嗅探采用保守模式，不改写目标地址') { throw 'Missing conservative sniffing guidance' }
if ($readme -notmatch '不要把 Windows Raw 地址添加成普通节点订阅') { throw 'Missing ordinary-profile warning' }
if ($readme -notmatch '不需要导入 JavaScript') { throw 'Missing single-YAML installation guidance' }
if ($readme -notmatch '不需要删除或重新导入节点订阅') { throw 'Missing Windows subscription preservation note' }
if ($readme -notmatch 'Windows 版为.+Spotify.+Telegram.+独立策略组') { throw 'Missing Windows desktop app scope statement' }
if ($readme -notmatch '不加入 TikTok.+移动 App 专项规则') { throw 'Missing mobile-app exclusion statement' }
if ($readme -notmatch '节点订阅.+MITM 证书.+不会') { throw 'Missing local subscription and certificate note' }
if ($readme -notmatch '远程规则和脚本.+update-interval.+自动') { throw 'Missing remote-resource automatic update guidance' }
if ($readme -match '(?m)^\s*\|.+\|\s*$') { throw 'README must not contain a comparison table' }

$mobileScripts = @(
    'B站去广告 · ZenmoFeiShi',
    '拼多多净化 · 怎么肥事、walala，本仓库修订',
    '墨鱼去开屏 2.0 · ddgksf2013',
    '百度贴吧去广告 · app2smile',
    '高德地图净化 · ddgksf2013',
    '网页广告净化 · fmz200',
    '喜马拉雅去广告 · fmz200',
    '下厨房去广告 · fmz200',
    '知乎去广告 · fmz200',
    '美团去广告 · fmz200',
    '淘宝去广告 · fmz200',
    '京东去广告 · fmz200',
    '闲鱼去广告 · fmz200',
    'WPS 去广告 · fmz200',
    '交管 12123 去广告 · fmz200',
    '微信公众号文章去广告 · fmz200',
    'Spotify · app2smile',
    '小红书净化 · fmz200',
    '抖音轻量净化 · fmz200',
    'Safari 聚合搜索 · zqzess'
)
$macScripts = @('X 网页广告净化 · fmz200', 'Safari 聚合搜索 · zqzess')
foreach ($script in $mobileScripts + $macScripts) {
    if ($readme -notmatch "(?m)^- $([regex]::Escape($script))\s*$") { throw "Missing enabled script: $script" }
}

Write-Output 'PASS: README cross-platform release validation'
