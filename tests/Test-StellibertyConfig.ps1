$ErrorActionPreference = 'Stop'

$repoRoot = Split-Path -Parent $PSScriptRoot
$configPath = Join-Path $repoRoot 'stelliberty-override.yaml'
$legacyScriptPath = Join-Path $repoRoot 'stelliberty-override.js'
if (-not (Test-Path -LiteralPath $configPath)) { throw 'Missing Stelliberty override' }
if (Test-Path -LiteralPath $legacyScriptPath) { throw 'Stelliberty override must not require JavaScript' }

$config = Get-Content -LiteralPath $configPath -Raw -Encoding UTF8

function Assert-Match {
    param([string]$Pattern, [string]$Message)
    if ($config -notmatch $Pattern) { throw $Message }
}

function Assert-NoMatch {
    param([string]$Pattern, [string]$Message)
    if ($config -match $Pattern) { throw $Message }
}

Assert-Match '(?m)^# Stelliberty / Mihomo Windows YAML 覆写\s*$' 'Missing Stelliberty override header'
Assert-NoMatch '(?m)^(mixed-port|allow-lan|mode|ipv6|log-level|unified-delay|tcp-concurrent|profile|tun):' 'Stelliberty-managed setting leaked into override'
Assert-NoMatch '(?m)^proxy-providers:\s*$' 'Node subscriptions must remain in Stelliberty subscription or Sub-Store'
Assert-NoMatch '(?m)^prepend-rules:\s*$' 'Stelliberty YAML override must use a complete rules array'
Assert-Match '(?m)^dns:\s*$' 'Stelliberty override must own DNS when DNS control is disabled'
Assert-Match '(?m)^sniffer:\s*$' 'Stelliberty override must own sniffing when sniff control is disabled'
Assert-Match '(?m)^\s+override-destination:\s*false\s*$' 'Sniffer must not rewrite destinations by default'
Assert-NoMatch '(?m)^\s+QUIC:\s*$' 'Sniffer must not probe QUIC on port 443'
Assert-Match '(?m)^\s+skip-domain:\s*$' 'Sniffer domain exclusions are missing'
Assert-Match '(?m)^\s+skip-dst-address:\s*$' 'Sniffer address exclusions are missing'
Assert-Match '(?m)^proxy-groups:\s*$' 'Stelliberty override must own proxy groups'
Assert-Match '(?m)^rule-providers:\s*$' 'Stelliberty override must own rule providers'
Assert-Match '(?m)^rules:\s*$' 'Stelliberty override must own routing rules'

foreach ($group in @(
    'Auto', 'Hong Kong', 'Taiwan', 'Japan', 'Singapore', 'United States',
    'Proxy', 'Spotify', 'Telegram',
    'OpenAI', 'GitHub', 'Microsoft',
    'Steam', 'Google', 'YouTube'
)) {
    Assert-Match "(?m)^\s+- name: $([regex]::Escape($group))\s*$" "Missing proxy group: $group"
}
$subscriptionStatusFilter = '(?i)(群|邀请|返利|循环|官网|客服|网站|网址|获取|订阅|剩余|流量|套餐|到期|过期|有效期|已用|重置|机场|下次|版本|官址|备用|联系|邮箱|工单|贩卖|通知|倒卖|防止|国内|地址|频道|无法|说明|使用|提示|特别|访问|支持|教程|关注|更新|作者|加入|超时|收藏|福利|好友|失联|\b(?:USE|USED|TOTAL|EXPIRE|EXPIRED|EMAIL|PANEL|CHANNEL|AUTHOR|NOTICE|TRAFFIC|REMAINING|RESET|BANDWIDTH)\d*\b|\d{4}-\d{2}-\d{2}|\dG)'
$excludeFilterLines = [regex]::Matches($config, "(?m)^\s{4}exclude-filter:\s*'$([regex]::Escape($subscriptionStatusFilter))'\s*$")
if ($excludeFilterLines.Count -ne 7) {
    throw 'Every Stelliberty group that expands subscription nodes must exclude status entries'
}
foreach ($blockedName in @('香港 剩余流量 20GB', '日本 套餐到期', 'US Remaining 50G', '新加坡 Bandwidth 100G', '客服频道', 'Airport Notice', '2026-08-01')) {
    if ($blockedName -notmatch $subscriptionStatusFilter) { throw "Subscription status filter missed: $blockedName" }
}
foreach ($allowedName in @('香港 01', '日本-HY2', 'United States 02')) {
    if ($allowedName -match $subscriptionStatusFilter) { throw "Subscription status filter rejected a normal node: $allowedName" }
}

foreach ($region in @('Hong Kong', 'Taiwan', 'Japan', 'Singapore', 'United States')) {
    $references = [regex]::Matches($config, "(?m)^\s{6}- $([regex]::Escape($region))\s*$").Count
    if ($references -ne 9) { throw "Every selectable group must include regional auto group: $region" }
}
$selectGroups = @('Proxy', 'Spotify', 'Telegram', 'OpenAI', 'GitHub', 'Microsoft', 'Steam', 'Google', 'YouTube')
Assert-Match '(?ms)- name: Auto.*?lazy: false' 'Auto group must probe immediately on startup'
foreach ($group in $selectGroups) {
    $block = [regex]::Match(
        $config,
        "(?ms)^\s{2}- name: $([regex]::Escape($group))\s*\r?\n.*?(?=^\s{2}- name:|^rule-providers:)"
    ).Value
    if (-not $block) { throw "Missing selectable group block: $group" }
    if ($block.IndexOf('      - DIRECT') -gt $block.IndexOf('      - Hong Kong')) {
        throw "Regional auto groups must be placed after DIRECT in group: $group"
    }
    if ($group -ne 'Proxy' -and $block -match '(?m)^\s{4}(?:include-all|exclude-filter):') {
        throw "Application group must not expand every subscription node: $group"
    }
}
$topLevelOrder = @(
    'Proxy', 'Spotify', 'Telegram', 'OpenAI', 'GitHub', 'Microsoft', 'Steam', 'Google', 'YouTube',
    'Auto', 'Hong Kong', 'Taiwan', 'Japan', 'Singapore', 'United States'
)
$lastTopLevelPosition = -1
foreach ($group in $topLevelOrder) {
    $position = $config.IndexOf("  - name: $group")
    if ($position -lt 0) { throw "Missing top-level proxy group: $group" }
    if ($position -le $lastTopLevelPosition) { throw "Top-level proxy group is out of order: $group" }
    $lastTopLevelPosition = $position
}
if (([regex]::Matches($config, '(?m)^\s{4}tolerance:\s*150\s*$')).Count -ne 6) { throw 'Every automatic latency group must use 150ms tolerance' }
if (([regex]::Matches($config, '(?m)^\s{4}expected-status:\s*204\s*$')).Count -ne 6) {
    throw 'Every automatic latency group must require HTTP 204'
}

$providers = @(
    'Cats-Team-AdRules',
    'Private-Domain',
    'Private-IP',
    'Spotify',
    'Telegram-Domain',
    'Telegram-IP',
    'OpenAI',
    'GitHub',
    'Microsoft-CN',
    'Microsoft',
    'OneDrive',
    'Steam-CN',
    'Steam',
    'Apple',
    'YouTube',
    'Google',
    'CN-Domain',
    'NonCN-Domain',
    'CN-IP'
)
foreach ($provider in $providers) {
    Assert-Match "(?m)^\s{2}$([regex]::Escape($provider)):\s*$" "Missing rule provider: $provider"
}
if (([regex]::Matches($config, '(?m)^\s{4}proxy:\s*DIRECT\s*$')).Count -ne 2) { throw 'Private rule providers must update via DIRECT' }
if (([regex]::Matches($config, '(?m)^\s{4}proxy:\s*Proxy\s*$')).Count -ne ($providers.Count - 2)) {
    throw 'Every GitHub rule provider must update through Proxy (except Private)'
}
Assert-NoMatch '(?m)^\s+- name:\s*Windows-' 'Visible proxy group names must not use a Windows prefix'
Assert-NoMatch '(?m)^\s{2}Windows-[^:]+:\s*$' 'Rule provider names must not use a Windows prefix'
Assert-NoMatch '(?m)^\s+- name:\s*OneDrive\s*$' 'OneDrive must share the Microsoft policy group'

Assert-Match '(?m)^\s+- "\+\.ts\.net"\s*$' 'MagicDNS must bypass fake IP'
Assert-Match '(?m)^\s+- "\+\.tailscale\.com"\s*$' 'Tailscale domains must bypass DNS and sniffing'
Assert-Match '(?m)^\s+- "\+\.icloud\.com"\s*$' 'iCloud domains must return real IPs for direct sync'
Assert-Match '(?m)^\s+- "\+\.apple\.com"\s*$' 'Apple domains must return real IPs for direct access'
Assert-Match '(?m)^\s+- DOMAIN-SUFFIX,ts\.net,DIRECT\s*$' 'Tailnet domain rule is missing'
Assert-Match '(?m)^\s+- IP-CIDR,100\.64\.0\.0/10,DIRECT,no-resolve\s*$' 'Tailscale IPv4 direct rule is missing'
Assert-Match '(?m)^\s+- IP-CIDR6,fd7a:115c:a1e0::/48,DIRECT,no-resolve\s*$' 'Tailscale IPv6 direct rule is missing'
Assert-Match '(?m)^\s+- RULE-SET,Cats-Team-AdRules,REJECT\s*$' 'Cats-Team ad rule is missing'
Assert-Match '(?m)^\s+- RULE-SET,Microsoft-CN,DIRECT\s*$' 'Microsoft China direct rule is missing'
Assert-Match '(?m)^\s+- RULE-SET,Steam-CN,DIRECT\s*$' 'Steam China download direct rule is missing'
Assert-Match '(?m)^\s+- RULE-SET,Apple,DIRECT\s*$' 'Apple direct rule is missing'
Assert-Match '(?m)^\s+- RULE-SET,OpenAI,OpenAI\s*$' 'OpenAI policy rule is missing'
Assert-Match '(?m)^\s+- RULE-SET,GitHub,GitHub\s*$' 'GitHub policy rule is missing'
Assert-Match '(?m)^\s+- DOMAIN-SUFFIX,zed\.dev,DIRECT\s*$' 'Zed direct rule is missing'
Assert-Match '(?m)^\s+- DOMAIN-SUFFIX,msftncsi\.com,DIRECT\s*$' 'Windows NCSI suffix rule is missing'
Assert-Match '(?m)^\s+- DOMAIN-SUFFIX,msftconnecttest\.com,DIRECT\s*$' 'Windows connect-test suffix rule is missing'
Assert-Match '(?m)^\s+- RULE-SET,OneDrive,Microsoft\s*$' 'OneDrive must route through Microsoft policy'
Assert-Match '(?m)^\s+- RULE-SET,Microsoft,Microsoft\s*$' 'Microsoft policy rule is missing'
Assert-Match '(?m)^\s+- RULE-SET,Steam,Steam\s*$' 'Steam store and community policy rule is missing'
Assert-Match '(?m)^\s+- RULE-SET,YouTube,YouTube\s*$' 'YouTube policy rule is missing'
Assert-Match '(?m)^\s+- RULE-SET,Google,Google\s*$' 'Google policy rule is missing'
Assert-Match '(?m)^\s+- RULE-SET,Spotify,Spotify\s*$' 'Spotify policy rule is missing'
Assert-Match '(?m)^\s+- RULE-SET,Telegram-Domain,Telegram\s*$' 'Telegram domain policy rule is missing'
Assert-Match '(?m)^\s+- RULE-SET,Telegram-IP,Telegram,no-resolve\s*$' 'Telegram IP policy rule is missing'
Assert-Match '(?m)^\s+- RULE-SET,CN-Domain,DIRECT\s*$' 'China domain direct rule is missing'
Assert-Match '(?m)^\s+- RULE-SET,NonCN-Domain,Proxy\s*$' 'Non-China domain proxy rule is missing'
Assert-Match '(?m)^\s+- RULE-SET,CN-IP,DIRECT,no-resolve\s*$' 'China IP direct rule is missing'
Assert-Match '(?m)^\s+- MATCH,Proxy\s*$' 'Final proxy rule is missing'

$tailscalePosition = $config.IndexOf('  - DOMAIN-SUFFIX,ts.net,DIRECT')
$adPosition = $config.IndexOf('  - RULE-SET,Cats-Team-AdRules,REJECT')
$microsoftCnPosition = $config.IndexOf('  - RULE-SET,Microsoft-CN,DIRECT')
$microsoftPosition = $config.IndexOf('  - RULE-SET,Microsoft,Microsoft')
$oneDrivePosition = $config.IndexOf('  - RULE-SET,OneDrive,Microsoft')
$steamCnPosition = $config.IndexOf('  - RULE-SET,Steam-CN,DIRECT')
$steamPosition = $config.IndexOf('  - RULE-SET,Steam,Steam')
$applePosition = $config.IndexOf('  - RULE-SET,Apple,DIRECT')
$nonCnPosition = $config.IndexOf('  - RULE-SET,NonCN-Domain,Proxy')
$youtubePosition = $config.IndexOf('  - RULE-SET,YouTube,YouTube')
$googlePosition = $config.IndexOf('  - RULE-SET,Google,Google')
$finalPosition = $config.IndexOf('  - MATCH,Proxy')
if ($tailscalePosition -lt 0 -or $adPosition -lt 0 -or $finalPosition -lt 0 -or
    $tailscalePosition -gt $adPosition -or $adPosition -gt $finalPosition) {
    throw 'Rule order must be system direct, ad blocking, then final routing'
}
if ($microsoftCnPosition -gt $microsoftPosition -or $oneDrivePosition -gt $microsoftPosition -or
    $steamCnPosition -gt $steamPosition) {
    throw 'China download/CDN and OneDrive rules must precede their broad service rules'
}
if ($applePosition -lt 0 -or $applePosition -gt $nonCnPosition) {
    throw 'Apple direct rule must precede the broad non-China rule'
}

if ($youtubePosition -lt 0 -or $googlePosition -lt 0 -or $youtubePosition -gt $googlePosition) {
    throw 'YouTube rule must precede broad Google rule'
}

foreach ($app in @('TikTok', 'Pinduoduo', 'Ximalaya', 'Zhihu', 'Bilibili')) {
    Assert-NoMatch "(?i)$([regex]::Escape($app))" "Windows profile contains a mobile-app rule: $app"
}

Assert-NoMatch '(?im)^\s*(url|token|password|certificate):\s*(?:https?://[^\s]*[?&](?:token|key)=|gh[opusr]_|eyJ)' 'Possible secret detected'

Write-Output 'PASS: Stelliberty override validation'
