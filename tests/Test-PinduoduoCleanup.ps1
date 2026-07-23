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

$loonPluginPath = Join-Path $repoRoot 'loon/plugins/pinduoduo-cleanup.lpx'
$loonScriptPath = Join-Path $repoRoot 'loon/scripts/pinduoduo-homepage-cleanup.js'
foreach ($requiredPath in @($loonPluginPath, $loonScriptPath)) {
    if (-not (Test-Path -LiteralPath $requiredPath)) {
        throw "Missing Loon Pinduoduo implementation: $requiredPath"
    }
}

$loonPlugin = Get-Content -Raw -LiteralPath $loonPluginPath -Encoding UTF8
$loonScript = Get-Content -Raw -LiteralPath $loonScriptPath -Encoding UTF8
foreach ($section in @('Rule', 'Rewrite', 'Script', 'MITM')) {
    if ($loonPlugin -notmatch "(?m)^\[$section\]\s*$") {
        throw "Loon Pinduoduo plugin missing section: $section"
    }
}

$expectedLoonScriptUrl = 'https://raw.githubusercontent.com/darkings/lat3ncy-proxy-configs/main/loon/scripts/pinduoduo-homepage-cleanup.js'
if ($loonPlugin -notmatch "(?m)^http-response \^https:\\/\\/api\\\.pinduoduo\\\.com\\/api\\/alexa\\/homepage\\/hub.*script-path\s*=\s*$([regex]::Escape($expectedLoonScriptUrl)),\s*requires-body\s*=\s*true,.*tag\s*=.+$") {
    throw 'Loon plugin must run the repository homepage cleanup script on every homepage hub response'
}
foreach ($rule in @(
    'DOMAIN, meta.pinduoduo.com, REJECT',
    'DOMAIN, cdl-1.pddpic.com, REJECT',
    'DOMAIN, titan.pinduoduo.com, REJECT'
)) {
    if ($loonPlugin -notmatch "(?m)^$([regex]::Escape($rule))\s*$") {
        throw "Loon plugin missing Pinduoduo blocking rule: $rule"
    }
}
foreach ($contract in @(
    'volantis3-open\\/component',
    'api\\/philo\\/personal\\/hub',
    'api\\/oak\\/integration\\/render',
    'api\\/cappuccino\\/splash'
)) {
    if ($loonPlugin -notmatch $contract) {
        throw "Loon plugin missing rewrite coverage: $contract"
    }
}
if ($loonPlugin -notmatch '(?m)^hostname\s*=.*api\.pinduoduo\.com.*m\.pinduoduo\.net') {
    throw 'Loon plugin MITM hostnames must cover the scripted Pinduoduo responses'
}
foreach ($contract in @('delete result\.icon_set', 'bottom_tabs', 'buffer_bottom_tabs', 'allowedBottomLinks')) {
    if ($loonScript -notmatch $contract) {
        throw "Loon homepage script missing cleanup behavior: $contract"
    }
}

$nodeContract = @'
const { cleanHomepage } = require(process.argv[1]);
const payload = {
  result: {
    icon_set: { icons: [1] },
    search_bar_hot_query: { text: "ad" },
    dy_module: { irregular_banner_dy: { id: 1 } },
    bottom_tabs: [
      { link: "index.html" },
      { link: "chat_list.html" },
      { link: "personal.html" },
      { link: "pdd_live_tab_list.html" },
      { link: "classification.html" }
    ],
    buffer_bottom_tabs: [
      { link: "index.html" },
      { link: "chat_list.html" },
      { link: "personal.html" },
      { link: "pdd_live_tab_list.html" }
    ]
  }
};
const result = cleanHomepage(payload).result;
if ("icon_set" in result || "search_bar_hot_query" in result) process.exit(11);
if ("irregular_banner_dy" in result.dy_module) process.exit(12);
for (const key of ["bottom_tabs", "buffer_bottom_tabs"]) {
  const links = result[key].map(item => item.link);
  if (links.join(",") !== "index.html,chat_list.html,personal.html") process.exit(13);
}
console.log("PASS: Loon homepage script removes restored tabs");
'@
$nodeOutput = & node -e $nodeContract $loonScriptPath
if ($LASTEXITCODE -ne 0 -or $nodeOutput -notcontains 'PASS: Loon homepage script removes restored tabs') {
    throw 'Loon homepage cleanup behavior contract failed'
}

Write-Output 'PASS: QX and Loon Pinduoduo cleanup validation'
