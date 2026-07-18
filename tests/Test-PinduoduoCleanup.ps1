$ErrorActionPreference = 'Stop'

$repoRoot = Split-Path -Parent $PSScriptRoot
$snippetPath = Join-Path $repoRoot 'rewrites/pinduoduo-cleanup.snippet'
$lines = Get-Content -LiteralPath $snippetPath

$observedDiscoveryIps = @(
    '101.35.212.35',
    '81.69.130.131',
    '114.110.97.30',
    '121.5.84.85'
)

foreach ($ip in $observedDiscoveryIps) {
    $escapedIp = [regex]::Escape($ip)
    $hostRules = @($lines | Where-Object { $_ -match "^host,\s*$escapedIp,\s*reject\s*$" })
    if ($hostRules.Count -ne 1) {
        throw "Expected exactly one connection-level reject for observed Pinduoduo discovery IP $ip; found $($hostRules.Count)"
    }
}

$legacyHttp404Rules = @($lines | Where-Object {
    $_ -notmatch '^\s*(//|#|;)' -and
    ($_ -match '\\/d4\\\?' -or $_ -match '\\/v2\\/d\\\?') -and
    $_ -match '\surl reject\s*$'
})
if ($legacyHttp404Rules.Count -ne 0) {
    throw "Address discovery must use Loon-style connection rejection, not HTTP 404 rewrites: $($legacyHttp404Rules -join '; ')"
}

$broadBlocking = $lines | Where-Object { $_ -notmatch '^\s*(//|#|;)' -and $_ -match '(?i)QUIC|udp_drop_list|443.*reject' }
if ($broadBlocking) {
    throw "Broad QUIC/UDP blocking is not allowed: $($broadBlocking -join '; ')"
}

Write-Output 'PASS: Pinduoduo address discovery uses Loon-style connection rejection'
