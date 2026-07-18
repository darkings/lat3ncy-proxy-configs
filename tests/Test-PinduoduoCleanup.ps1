$ErrorActionPreference = 'Stop'

$repoRoot = Split-Path -Parent $PSScriptRoot
$snippetPath = Join-Path $repoRoot 'rewrites/pinduoduo-cleanup.snippet'
$configPath = Join-Path $repoRoot 'quantumultx.conf'
$filterPath = Join-Path $repoRoot 'rules/pinduoduo-network-block.list'
$snippetLines = Get-Content -LiteralPath $snippetPath
$config = Get-Content -Raw -LiteralPath $configPath

if (-not (Test-Path -LiteralPath $filterPath)) {
    throw 'Missing remote Pinduoduo IP-CIDR filter resource'
}

$filterLines = Get-Content -LiteralPath $filterPath

$observedDiscoveryIps = @(
    '101.35.204.35',
    '101.35.212.35',
    '81.69.130.131',
    '114.110.96.6',
    '114.110.96.26',
    '114.110.97.30',
    '121.5.84.85'
)

foreach ($ip in $observedDiscoveryIps) {
    $escapedIp = [regex]::Escape($ip)
    $cidrRules = @($filterLines | Where-Object { $_ -match "^ip-cidr,\s*$escapedIp/32,\s*reject\s*$" })
    if ($cidrRules.Count -ne 1) {
        throw "Expected exactly one IP-CIDR reject for observed Pinduoduo discovery IP $ip; found $($cidrRules.Count)"
    }
}

$filterUrl = 'https://raw.githubusercontent.com/darkings/lat3ncy-quantumultx-config/main/rules/pinduoduo-network-block.list'
$remoteReferences = @($config -split "`r?`n" | Where-Object {
    $_ -match "^$([regex]::Escape($filterUrl)),\s*tag=拼多多网络阻断@darkings,\s*force-policy=reject,.*enabled=true\s*$"
})
if ($remoteReferences.Count -ne 1) {
    throw "Expected exactly one enabled remote Pinduoduo network filter reference; found $($remoteReferences.Count)"
}

$ineffectiveIpHostRules = @($snippetLines | Where-Object {
    $_ -match '^host,\s*(?:\d{1,3}\.){3}\d{1,3},\s*reject\s*$'
})
if ($ineffectiveIpHostRules.Count -ne 0) {
    throw "IP literals must be rejected in the filter layer, not rewrite host rules: $($ineffectiveIpHostRules -join '; ')"
}

$legacyHttp404Rules = @($snippetLines | Where-Object {
    $_ -notmatch '^\s*(//|#|;)' -and
    ($_ -match '\\/d4\\\?' -or $_ -match '\\/v2\\/d\\\?') -and
    $_ -match '\surl reject\s*$'
})
if ($legacyHttp404Rules.Count -ne 0) {
    throw "Address discovery must use Loon-style connection rejection, not HTTP 404 rewrites: $($legacyHttp404Rules -join '; ')"
}

$broadBlocking = $filterLines | Where-Object { $_ -notmatch '^\s*(//|#|;)' -and $_ -match '(?i)0\.0\.0\.0/0|::/0|QUIC|UDP|443' }
if ($broadBlocking) {
    throw "Pinduoduo filter must contain only observed /32 targets: $($broadBlocking -join '; ')"
}

$activeFilterRules = @($filterLines | Where-Object { $_ -notmatch '^\s*(//|#|;|$)' })
$nonExactRules = @($activeFilterRules | Where-Object { $_ -notmatch '^ip-cidr,\s*(?:\d{1,3}\.){3}\d{1,3}/32,\s*reject\s*$' })
if ($nonExactRules.Count -ne 0) {
    throw "Shared Tencent Cloud and Baidu ranges must not be blocked; only exact /32 targets are allowed: $($nonExactRules -join '; ')"
}

if ($activeFilterRules.Count -ne $observedDiscoveryIps.Count) {
    throw "Expected only the $($observedDiscoveryIps.Count) observed Pinduoduo discovery IPs; found $($activeFilterRules.Count) active rules"
}

Write-Output 'PASS: Pinduoduo address discovery uses a remote IP-CIDR filter'
