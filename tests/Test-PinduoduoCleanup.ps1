$ErrorActionPreference = 'Stop'

$repoRoot = Split-Path -Parent $PSScriptRoot
$snippetPath = Join-Path $repoRoot 'rewrites/pinduoduo-cleanup.snippet'
$configPath = Join-Path $repoRoot 'quantumultx.conf'
$legacyFilterPath = Join-Path $repoRoot 'rules/pinduoduo-network-block.list'
$snippetLines = Get-Content -LiteralPath $snippetPath -Encoding UTF8
$config = Get-Content -Raw -LiteralPath $configPath -Encoding UTF8

$ipv4UrlPattern = '^http:\/\/(?:\d{1,3}\.){3}\d{1,3}(?::\d+)?\/(?:d4|v2\/d)(?:\?.*)?$'
$ipv6UrlPattern = '^http:\/\/\[[0-9A-Fa-f:.%]+\](?::\d+)?\/(?:d4|v2\/d)(?:\?.*)?$'
$duplicatedD4UrlPattern = '^http:\/\/(?:\d{1,3}\.){3}\d{1,3}(?::\d+)?http:\/\/(?:\d{1,3}\.){3}\d{1,3}(?::\d+)?\/d4(?:\?.*)?$'
$pinduoduoHeaderPattern = '\r\nUser-Agent:.*BundleID\/com\.xunmeng\.pinduoduo'

$expectedRules = @(
    "$ipv4UrlPattern $pinduoduoHeaderPattern url-and-header reject",
    "$ipv6UrlPattern $pinduoduoHeaderPattern url-and-header reject",
    "$duplicatedD4UrlPattern $pinduoduoHeaderPattern url-and-header reject"
)

foreach ($rule in $expectedRules) {
    $matches = @($snippetLines | Where-Object { $_ -ceq $rule })
    if ($matches.Count -ne 1) {
        throw "Expected exactly one official-style Pinduoduo url-and-header rule: $rule; found $($matches.Count)"
    }
}

$ipv4Regex = [regex]$ipv4UrlPattern
foreach ($url in @(
    'http://81.69.130.131/d4?foo=bar',
    'http://114.110.97.97/v2/d?foo=bar',
    'http://101.35.204.35:80/v2/d'
)) {
    if (-not $ipv4Regex.IsMatch($url)) {
        throw "IPv4 address-discovery rule does not match observed URL: $url"
    }
}

foreach ($url in @(
    'https://114.110.97.97/v2/d?foo=bar',
    'http://114.110.97.97/v2/different',
    'http://api.pinduoduo.com/v2/d'
)) {
    if ($ipv4Regex.IsMatch($url)) {
        throw "IPv4 address-discovery rule is too broad: $url"
    }
}

$duplicatedD4Regex = [regex]$duplicatedD4UrlPattern
$observedDuplicatedD4Url = 'http://114.110.96.6http://114.110.96.6/d4?appid=1&os=2&clientVersion=8.16.0&type=ADDRS'
if (-not $duplicatedD4Regex.IsMatch($observedDuplicatedD4Url)) {
    throw "Duplicated-URL compatibility rule does not match the latest HAR request: $observedDuplicatedD4Url"
}
foreach ($url in @(
    'http://114.110.96.6/d4?appid=1&type=ADDRS',
    'http://114.110.96.6http://114.110.96.6/v2/d?appid=1&type=ADDRS',
    'http://api.pinduoduo.comhttp://114.110.96.6/d4?appid=1&type=ADDRS'
)) {
    if ($duplicatedD4Regex.IsMatch($url)) {
        throw "Duplicated-URL compatibility rule is too broad: $url"
    }
}

$headerRegex = [regex]$pinduoduoHeaderPattern
$pinduoduoHeaders = "GET /v2/d HTTP/1.1`r`nUser-Agent: Mozilla/5.0 BundleID/com.xunmeng.pinduoduo AppVersion/8.16.0`r`n"
$otherHeaders = "GET /v2/d HTTP/1.1`r`nUser-Agent: Mozilla/5.0 BundleID/com.example.other`r`n"
if (-not $headerRegex.IsMatch($pinduoduoHeaders)) {
    throw 'Header rule does not match the observed Pinduoduo BundleID'
}
if ($headerRegex.IsMatch($otherHeaders)) {
    throw 'Header rule must not match another app using the same shared cloud IP'
}

$legacyFilterUrl = 'https://raw.githubusercontent.com/darkings/lat3ncy-proxy-configs/main/rules/pinduoduo-network-block.list'
if ($config -match [regex]::Escape($legacyFilterUrl)) {
    throw 'Main configuration must not reference the global Pinduoduo IP-CIDR filter'
}
if (Test-Path -LiteralPath $legacyFilterPath) {
    throw 'Legacy rotating-IP filter file must be removed'
}

$ineffectiveIpHostRules = @($snippetLines | Where-Object {
    $_ -match '^host,\s*(?:\d{1,3}\.){3}\d{1,3},\s*reject\s*$'
})
if ($ineffectiveIpHostRules.Count -ne 0) {
    throw "IP literals must not use host rules: $($ineffectiveIpHostRules -join '; ')"
}

$homepagePrefix = '^https:\/\/api\.pinduoduo\.com\/api\/alexa\/homepage\/hub url script-response-body '
$homepageRules = @($snippetLines | Where-Object { $_.StartsWith($homepagePrefix) })
if ($homepageRules.Count -ne 1) {
    throw "Expected the homepage bottom-tab response script to remain enabled; found $($homepageRules.Count)"
}

Write-Output 'PASS: Pinduoduo address discovery uses URL-and-header matching without global IP blocks'
