$ErrorActionPreference = 'Stop'

$repoRoot = Split-Path -Parent $PSScriptRoot
$configPath = Join-Path $repoRoot 'sparkle-windows-override.yaml'
$legacyScriptPath = Join-Path $repoRoot 'sparkle-windows-override.js'
if (-not (Test-Path -LiteralPath $configPath)) { throw 'Missing Windows Sparkle override' }
if (Test-Path -LiteralPath $legacyScriptPath) { throw 'Sparkle remote override must not require JavaScript' }

$config = Get-Content -LiteralPath $configPath -Raw -Encoding UTF8

function Assert-Match {
    param([string]$Pattern, [string]$Message)
    if ($config -notmatch $Pattern) { throw $Message }
}

function Assert-NoMatch {
    param([string]$Pattern, [string]$Message)
    if ($config -match $Pattern) { throw $Message }
}

Assert-Match '(?m)^# Sparkle / Mihomo Windows 远程 YAML 覆写\s*$' 'Missing Sparkle override header'
Assert-NoMatch '(?m)^(mixed-port|allow-lan|mode|ipv6|log-level|unified-delay|tcp-concurrent|profile|tun):' 'Sparkle-managed setting leaked into remote override'
Assert-NoMatch '(?m)^proxy-providers:\s*$' 'Node subscriptions must remain in Sparkle or Sub-Store'
Assert-NoMatch '(?m)^prepend-rules:\s*$' 'Sparkle YAML override must use a complete rules array'
Assert-Match '(?m)^dns:\s*$' 'Sparkle override must own DNS when DNS control is disabled'
Assert-Match '(?m)^sniffer:\s*$' 'Sparkle override must own sniffing when sniff control is disabled'
Assert-Match '(?m)^\s+override-destination:\s*false\s*$' 'Sniffer must not rewrite destinations by default'
Assert-Match '(?m)^\s+skip-domain:\s*$' 'Sniffer domain exclusions are missing'
Assert-Match '(?m)^\s+skip-dst-address:\s*$' 'Sniffer address exclusions are missing'
Assert-Match '(?m)^proxy-groups:\s*$' 'Sparkle override must own proxy groups'
Assert-Match '(?m)^rule-providers:\s*$' 'Sparkle override must own rule providers'
Assert-Match '(?m)^rules:\s*$' 'Sparkle override must own routing rules'

foreach ($group in @(
    'Auto', 'Proxy', 'Spotify', 'Telegram',
    'OpenAI', 'GitHub', 'Microsoft', 'OneDrive',
    'Steam', 'Apple', 'YouTube'
)) {
    Assert-Match "(?m)^\s+- name: $([regex]::Escape($group))\s*$" "Missing proxy group: $group"
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
    'Apple-CN',
    'Apple',
    'YouTube',
    'CN-Domain',
    'NonCN-Domain',
    'CN-IP'
)
foreach ($provider in $providers) {
    Assert-Match "(?m)^\s{2}$([regex]::Escape($provider)):\s*$" "Missing rule provider: $provider"
}
if (([regex]::Matches($config, '(?m)^\s{4}proxy:\s*Proxy\s*$')).Count -ne $providers.Count) {
    throw 'Every GitHub rule provider must update through Proxy'
}
Assert-NoMatch '(?m)^\s+- name:\s*Windows-' 'Visible proxy group names must not use a Windows prefix'
Assert-NoMatch '(?m)^\s{2}Windows-[^:]+:\s*$' 'Rule provider names must not use a Windows prefix'

Assert-Match '(?m)^\s+- "\+\.ts\.net"\s*$' 'MagicDNS must bypass fake IP'
Assert-Match '(?m)^\s+- "\+\.tailscale\.com"\s*$' 'Tailscale domains must bypass DNS and sniffing'
Assert-Match '(?m)^\s+- DOMAIN-SUFFIX,ts\.net,DIRECT\s*$' 'Tailnet domain rule is missing'
Assert-Match '(?m)^\s+- IP-CIDR,100\.64\.0\.0/10,DIRECT,no-resolve\s*$' 'Tailscale IPv4 direct rule is missing'
Assert-Match '(?m)^\s+- IP-CIDR6,fd7a:115c:a1e0::/48,DIRECT,no-resolve\s*$' 'Tailscale IPv6 direct rule is missing'
Assert-Match '(?m)^\s+- RULE-SET,Cats-Team-AdRules,REJECT\s*$' 'Cats-Team ad rule is missing'
Assert-Match '(?m)^\s+- RULE-SET,Microsoft-CN,DIRECT\s*$' 'Microsoft China direct rule is missing'
Assert-Match '(?m)^\s+- RULE-SET,Steam-CN,DIRECT\s*$' 'Steam China download direct rule is missing'
Assert-Match '(?m)^\s+- RULE-SET,Apple-CN,DIRECT\s*$' 'Apple China CDN direct rule is missing'
Assert-Match '(?m)^\s+- RULE-SET,OpenAI,OpenAI\s*$' 'OpenAI policy rule is missing'
Assert-Match '(?m)^\s+- RULE-SET,GitHub,GitHub\s*$' 'GitHub policy rule is missing'
Assert-Match '(?m)^\s+- RULE-SET,OneDrive,OneDrive\s*$' 'OneDrive policy rule is missing'
Assert-Match '(?m)^\s+- RULE-SET,Microsoft,Microsoft\s*$' 'Microsoft policy rule is missing'
Assert-Match '(?m)^\s+- RULE-SET,Steam,Steam\s*$' 'Steam store and community policy rule is missing'
Assert-Match '(?m)^\s+- RULE-SET,Apple,Apple\s*$' 'Apple international policy rule is missing'
Assert-Match '(?m)^\s+- RULE-SET,YouTube,YouTube\s*$' 'YouTube policy rule is missing'
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
$oneDrivePosition = $config.IndexOf('  - RULE-SET,OneDrive,OneDrive')
$steamCnPosition = $config.IndexOf('  - RULE-SET,Steam-CN,DIRECT')
$steamPosition = $config.IndexOf('  - RULE-SET,Steam,Steam')
$appleCnPosition = $config.IndexOf('  - RULE-SET,Apple-CN,DIRECT')
$applePosition = $config.IndexOf('  - RULE-SET,Apple,Apple')
$finalPosition = $config.IndexOf('  - MATCH,Proxy')
if ($tailscalePosition -lt 0 -or $adPosition -lt 0 -or $finalPosition -lt 0 -or
    $tailscalePosition -gt $adPosition -or $adPosition -gt $finalPosition) {
    throw 'Rule order must be system direct, ad blocking, then final routing'
}
if ($microsoftCnPosition -gt $microsoftPosition -or $oneDrivePosition -gt $microsoftPosition -or
    $steamCnPosition -gt $steamPosition -or $appleCnPosition -gt $applePosition) {
    throw 'China download/CDN and OneDrive rules must precede their broad service rules'
}

foreach ($app in @('TikTok', 'Pinduoduo', 'Ximalaya', 'Zhihu', 'Bilibili')) {
    Assert-NoMatch "(?i)$([regex]::Escape($app))" "Windows profile contains a mobile-app rule: $app"
}

Assert-NoMatch '(?im)^\s*(url|token|password|certificate):\s*(?:https?://[^\s]*[?&](?:token|key)=|gh[opusr]_|eyJ)' 'Possible secret detected'

Write-Output 'PASS: Windows Sparkle remote-YAML override validation'
