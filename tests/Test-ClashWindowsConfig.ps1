$ErrorActionPreference = 'Stop'

$repoRoot = Split-Path -Parent $PSScriptRoot
$configPath = Join-Path $repoRoot 'clash-verge-windows.yaml'
$legacyScriptPath = Join-Path $repoRoot 'clash-verge-windows.js'
if (-not (Test-Path -LiteralPath $configPath)) { throw 'Missing Windows Clash Verge config' }
if (Test-Path -LiteralPath $legacyScriptPath) { throw 'Single-YAML mode must not require a JavaScript extension' }

$config = Get-Content -LiteralPath $configPath -Raw

function Assert-Match {
    param([string]$Pattern, [string]$Message)
    if ($config -notmatch $Pattern) { throw $Message }
}

function Assert-NoMatch {
    param([string]$Pattern, [string]$Message)
    if ($config -match $Pattern) { throw $Message }
}

Assert-Match '(?m)^allow-lan:\s*false\s*$' 'LAN proxy must be disabled'
Assert-Match '(?ms)^tun:\s*\r?\n(?:^\s+.*\r?\n?)*?^\s+enable:\s*false\s*$' 'TUN must be disabled by default'
Assert-Match '(?m)^\s+stack:\s*mixed\s*$' 'TUN stack must use mixed mode'
Assert-Match '(?m)^\s+strict-route:\s*false\s*$' 'Strict route must remain disabled for WSL compatibility'
Assert-NoMatch '(?m)^prepend-rules:\s*$' 'Single-YAML mode must use a complete rules array'
Assert-Match '(?m)^proxy-groups:\s*$' 'Single-YAML mode must own proxy groups'
Assert-Match '(?m)^rule-providers:\s*$' 'Single-YAML mode must own rule providers'
Assert-Match '(?m)^rules:\s*$' 'Single-YAML mode must own routing rules'

foreach ($group in @(
    'Windows-Auto', 'Windows-Proxy', 'Windows-Spotify', 'Windows-Telegram',
    'Windows-OpenAI', 'Windows-GitHub', 'Windows-Microsoft', 'Windows-OneDrive',
    'Windows-Steam', 'Windows-Apple', 'Windows-YouTube'
)) {
    Assert-Match "(?m)^\s+- name: $([regex]::Escape($group))\s*$" "Missing proxy group: $group"
}

$providers = @(
    'Cats-Team-AdRules',
    'Windows-Private-Domain',
    'Windows-Private-IP',
    'Windows-Spotify',
    'Windows-Telegram-Domain',
    'Windows-Telegram-IP',
    'Windows-OpenAI',
    'Windows-GitHub',
    'Windows-Microsoft-CN',
    'Windows-Microsoft',
    'Windows-OneDrive',
    'Windows-Steam-CN',
    'Windows-Steam',
    'Windows-Apple-CN',
    'Windows-Apple',
    'Windows-YouTube',
    'Windows-CN-Domain',
    'Windows-NonCN-Domain',
    'Windows-CN-IP'
)
foreach ($provider in $providers) {
    Assert-Match "(?m)^\s{2}$([regex]::Escape($provider)):\s*$" "Missing rule provider: $provider"
}
if (([regex]::Matches($config, '(?m)^\s{4}proxy:\s*Windows-Proxy\s*$')).Count -ne $providers.Count) {
    throw 'Every GitHub rule provider must update through Windows-Proxy'
}

Assert-Match '(?m)^\s+- "\+\.ts\.net"\s*$' 'MagicDNS must bypass fake IP'
Assert-Match '(?m)^\s+- 100\.64\.0\.0/10\s*$' 'Tailscale IPv4 must bypass TUN'
Assert-Match '(?m)^\s+- fd7a:115c:a1e0::/48\s*$' 'Tailscale IPv6 must bypass TUN'
Assert-Match '(?m)^\s+- DOMAIN-SUFFIX,ts\.net,DIRECT\s*$' 'Tailnet domain rule is missing'
Assert-Match '(?m)^\s+- RULE-SET,Cats-Team-AdRules,REJECT\s*$' 'Cats-Team ad rule is missing'
Assert-Match '(?m)^\s+- RULE-SET,Windows-Microsoft-CN,DIRECT\s*$' 'Microsoft China direct rule is missing'
Assert-Match '(?m)^\s+- RULE-SET,Windows-Steam-CN,DIRECT\s*$' 'Steam China download direct rule is missing'
Assert-Match '(?m)^\s+- RULE-SET,Windows-Apple-CN,DIRECT\s*$' 'Apple China CDN direct rule is missing'
Assert-Match '(?m)^\s+- RULE-SET,Windows-OpenAI,Windows-OpenAI\s*$' 'OpenAI policy rule is missing'
Assert-Match '(?m)^\s+- RULE-SET,Windows-GitHub,Windows-GitHub\s*$' 'GitHub policy rule is missing'
Assert-Match '(?m)^\s+- RULE-SET,Windows-OneDrive,Windows-OneDrive\s*$' 'OneDrive policy rule is missing'
Assert-Match '(?m)^\s+- RULE-SET,Windows-Microsoft,Windows-Microsoft\s*$' 'Microsoft policy rule is missing'
Assert-Match '(?m)^\s+- RULE-SET,Windows-Steam,Windows-Steam\s*$' 'Steam store and community policy rule is missing'
Assert-Match '(?m)^\s+- RULE-SET,Windows-Apple,Windows-Apple\s*$' 'Apple international policy rule is missing'
Assert-Match '(?m)^\s+- RULE-SET,Windows-YouTube,Windows-YouTube\s*$' 'YouTube policy rule is missing'
Assert-Match '(?m)^\s+- RULE-SET,Windows-Spotify,Windows-Spotify\s*$' 'Spotify policy rule is missing'
Assert-Match '(?m)^\s+- RULE-SET,Windows-Telegram-Domain,Windows-Telegram\s*$' 'Telegram domain policy rule is missing'
Assert-Match '(?m)^\s+- RULE-SET,Windows-Telegram-IP,Windows-Telegram,no-resolve\s*$' 'Telegram IP policy rule is missing'
Assert-Match '(?m)^\s+- RULE-SET,Windows-CN-Domain,DIRECT\s*$' 'China domain direct rule is missing'
Assert-Match '(?m)^\s+- RULE-SET,Windows-NonCN-Domain,Windows-Proxy\s*$' 'Non-China domain proxy rule is missing'
Assert-Match '(?m)^\s+- RULE-SET,Windows-CN-IP,DIRECT,no-resolve\s*$' 'China IP direct rule is missing'
Assert-Match '(?m)^\s+- MATCH,Windows-Proxy\s*$' 'Final Windows proxy rule is missing'

$tailscalePosition = $config.IndexOf('  - DOMAIN-SUFFIX,ts.net,DIRECT')
$adPosition = $config.IndexOf('  - RULE-SET,Cats-Team-AdRules,REJECT')
$microsoftCnPosition = $config.IndexOf('  - RULE-SET,Windows-Microsoft-CN,DIRECT')
$microsoftPosition = $config.IndexOf('  - RULE-SET,Windows-Microsoft,Windows-Microsoft')
$oneDrivePosition = $config.IndexOf('  - RULE-SET,Windows-OneDrive,Windows-OneDrive')
$steamCnPosition = $config.IndexOf('  - RULE-SET,Windows-Steam-CN,DIRECT')
$steamPosition = $config.IndexOf('  - RULE-SET,Windows-Steam,Windows-Steam')
$appleCnPosition = $config.IndexOf('  - RULE-SET,Windows-Apple-CN,DIRECT')
$applePosition = $config.IndexOf('  - RULE-SET,Windows-Apple,Windows-Apple')
$finalPosition = $config.IndexOf('  - MATCH,Windows-Proxy')
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

Write-Output 'PASS: Windows Clash Verge single-YAML validation'
