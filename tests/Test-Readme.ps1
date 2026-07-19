$ErrorActionPreference = 'Stop'

$repoRoot = Split-Path -Parent $PSScriptRoot
$readme = Get-Content -LiteralPath (Join-Path $repoRoot 'README.md') -Raw

$headings = @('# Quantumult X 配置发布', '## 配置下载', '## 手机版说明', '## macOS 版说明')
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
if ($readme -notmatch '节点订阅.+MITM 证书.+不会') { throw 'Missing local subscription and certificate update note' }

foreach ($removed in @('拼多多净化维护', 'GitHub 更新监控脚本使用 BoxJS 参数', '## 定时任务', '## 注意事项')) {
    if ($readme -match [regex]::Escape($removed)) { throw "README still contains removed content: $removed" }
}
if ($readme -match '(?m)^\s*\|.+\|\s*$') { throw 'README must not contain a comparison table' }

Write-Output 'PASS: README release structure validation'
