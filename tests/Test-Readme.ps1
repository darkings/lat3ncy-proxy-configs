$ErrorActionPreference = 'Stop'

$repoRoot = Split-Path -Parent $PSScriptRoot
$readme = Get-Content -LiteralPath (Join-Path $repoRoot 'README.md') -Raw

$headings = @(
    '# Quantumult X 自用配置',
    '## 配置下载',
    '## 手机版说明',
    '### 手机版默认启用脚本',
    '## macOS 版说明',
    '### macOS 默认启用脚本',
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

$mobile = 'https://raw.githubusercontent.com/darkings/lat3ncy-quantumultx-config/main/quantumultx.conf'
$mac = 'https://raw.githubusercontent.com/darkings/lat3ncy-quantumultx-config/main/quantumultx-macos.conf'
if ($readme -notmatch [regex]::Escape($mobile)) { throw 'Missing mobile download URL' }
if ($readme -notmatch [regex]::Escape($mac)) { throw 'Missing macOS download URL' }
if ($readme -notmatch '自用.+手机版.+macOS') { throw 'Missing self-use mobile and macOS positioning' }
if ($readme -notmatch '节点订阅.+MITM 证书.+不会') { throw 'Missing local subscription and certificate update note' }
if ($readme -notmatch '主配置.+(?:刷新|重新导入)') { throw 'Missing main-profile refresh guidance' }
if ($readme -notmatch '远程资源.+update-interval.+自动') { throw 'Missing remote-resource automatic update guidance' }

foreach ($removed in @('拼多多净化维护', 'GitHub 更新监控脚本使用 BoxJS 参数', '## 定时任务', '## 注意事项')) {
    if ($readme -match [regex]::Escape($removed)) { throw "README still contains removed content: $removed" }
}
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
foreach ($forbiddenHeading in @('默认启用的工具', '默认启用的定时任务', '预置但默认关闭')) {
    if ($readme -match "(?m)^#{2,4}\s+$([regex]::Escape($forbiddenHeading))\s*$") { throw "README contains excluded script category: $forbiddenHeading" }
}

Write-Output 'PASS: README release structure validation'
