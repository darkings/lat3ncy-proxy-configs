$ErrorActionPreference = 'Stop'

$repoRoot = Split-Path -Parent $PSScriptRoot
$rulePath = Join-Path $repoRoot 'loon/rules/microsoft-cn.list'
$generatorPath = Join-Path $repoRoot 'scripts/generate_microsoft_cn.py'

if (-not (Test-Path -LiteralPath $rulePath)) { throw 'Missing generated Microsoft-CN Loon rule' }
if (-not (Test-Path -LiteralPath $generatorPath)) { throw 'Missing Microsoft-CN generator' }

$content = Get-Content -LiteralPath $rulePath -Raw -Encoding UTF8
$rules = @($content -split "`r?`n" | Where-Object { $_ -and -not $_.StartsWith('#') })
if ($rules.Count -lt 100) { throw "Microsoft-CN rule count is unexpectedly low: $($rules.Count)" }

$invalid = @($rules | Where-Object { $_ -notmatch '^(?:DOMAIN|DOMAIN-SUFFIX|DOMAIN-KEYWORD),[^,\s]+$' })
if ($invalid) { throw "Invalid Microsoft-CN rules: $($invalid -join ', ')" }

$duplicates = @($rules | Group-Object | Where-Object Count -gt 1)
if ($duplicates) { throw "Duplicate Microsoft-CN rules: $($duplicates.Name -join ', ')" }

$sortKeys = [string[]]@($rules | ForEach-Object { "$(($_ -split ',', 2)[1])`0$_" })
$sortedKeys = [string[]]$sortKeys.Clone()
[Array]::Sort($sortedKeys, [StringComparer]::Ordinal)
if (($sortKeys -join "`n") -cne ($sortedKeys -join "`n")) {
    throw 'Microsoft-CN rules must remain deterministically sorted by domain'
}

foreach ($expected in @(
    'DOMAIN-SUFFIX,azure.cn',
    'DOMAIN-SUFFIX,bing.com.cn',
    'DOMAIN,download.microsoft.com',
    'DOMAIN-SUFFIX,microsoftonline.cn',
    'DOMAIN,officecdn.microsoft.com'
)) {
    if ($rules -cnotcontains $expected) { throw "Microsoft-CN rule missing expected entry: $expected" }
}

Write-Output "PASS: Microsoft-CN generated Loon rule validation ($($rules.Count) rules)"
