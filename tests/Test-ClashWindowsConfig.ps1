$ErrorActionPreference = 'Stop'

$repoRoot = Split-Path -Parent $PSScriptRoot
$configPath = Join-Path $repoRoot 'clash-verge-windows.yaml'
$scriptPath = Join-Path $repoRoot 'clash-verge-windows.js'
if (-not (Test-Path -LiteralPath $configPath)) { throw 'Missing Windows Clash Verge config' }
if (-not (Test-Path -LiteralPath $scriptPath)) { throw 'Missing Windows Clash Verge extension script' }

$config = Get-Content -LiteralPath $configPath -Raw
$script = Get-Content -LiteralPath $scriptPath -Raw
$combined = "$config`n$script"

function Assert-Match {
    param([string]$Text, [string]$Pattern, [string]$Message)
    if ($Text -notmatch $Pattern) { throw $Message }
}

function Assert-NoMatch {
    param([string]$Text, [string]$Pattern, [string]$Message)
    if ($Text -match $Pattern) { throw $Message }
}

Assert-Match $config '(?m)^allow-lan:\s*false\s*$' 'LAN proxy must be disabled'
Assert-Match $config '(?m)^\s+stack:\s*mixed\s*$' 'TUN stack must use mixed mode'
Assert-Match $config '(?m)^\s+strict-route:\s*false\s*$' 'Strict route must remain disabled for WSL compatibility'
$tunBlock = [regex]::Match($config, '(?ms)^tun:\s*\r?\n(?:^\s+.*\r?\n?)*').Value
Assert-NoMatch $tunBlock '(?m)^\s+enable:\s*(?:true|false)\s*$' 'Remote YAML must not force the TUN switch'
Assert-NoMatch $config '(?m)^prepend-rules:\s*$' 'New Clash Verge versions must use an extension script for rule insertion'
Assert-NoMatch $config '(?m)^\s+fake-ip-filter:\s*$' 'YAML must not replace the subscription fake-IP filter array'
Assert-NoMatch $config '(?m)^\s+route-exclude-address:\s*$' 'YAML must not replace existing TUN exclusions'
Assert-NoMatch $config '(?m)^rule-providers:\s*$' 'Rule providers are managed by the extension script'

Assert-Match $script 'DOMAIN-SUFFIX,ts\.net,DIRECT' 'Tailnet domains must be direct'
Assert-Match $script 'IP-CIDR,100\.64\.0\.0/10,DIRECT,no-resolve' 'Tailscale IPv4 must be direct'
Assert-Match $script 'IP-CIDR6,fd7a:115c:a1e0::/48,DIRECT,no-resolve' 'Tailscale IPv6 must be direct'
Assert-Match $script 'Windows-Private-Domain' 'Private-domain provider is missing'
Assert-Match $script 'Windows-Private-IP' 'Private-IP provider is missing'
Assert-Match $script 'RULE-SET,Cats-Team-AdRules,REJECT' 'Cats-Team ad rule is missing'
Assert-Match $script 'MetaCubeX/meta-rules-dat/meta/geo/geosite/private\.mrs' 'MetaCubeX private-domain MRS is missing'
Assert-Match $script 'MetaCubeX/meta-rules-dat/meta/geo/geoip/private\.mrs' 'MetaCubeX private-IP MRS is missing'
Assert-Match $script 'www\.msftconnecttest\.com' 'Windows connectivity-check direct rule is missing'
Assert-Match $script 'MetaCubeX/meta-rules-dat/meta/geo/geosite/spotify\.mrs' 'Spotify Windows rule provider is missing'
Assert-Match $script 'MetaCubeX/meta-rules-dat/meta/geo/geosite/telegram\.mrs' 'Telegram Windows domain provider is missing'
Assert-Match $script 'MetaCubeX/meta-rules-dat/meta/geo/geoip/telegram\.mrs' 'Telegram Windows IP provider is missing'
Assert-Match $script 'RULE-SET,Windows-Spotify,Windows-Spotify' 'Spotify policy rule is missing'
Assert-Match $script 'RULE-SET,Windows-Telegram-Domain,Windows-Telegram' 'Telegram domain policy rule is missing'
Assert-Match $script 'RULE-SET,Windows-Telegram-IP,Windows-Telegram,no-resolve' 'Telegram IP policy rule is missing'
Assert-Match $script 'name: "Windows-Auto"' 'Windows automatic node group is missing'
Assert-Match $script 'name: "Windows-Spotify"' 'Spotify Windows policy group is missing'
Assert-Match $script 'name: "Windows-Telegram"' 'Telegram Windows policy group is missing'
Assert-Match $script 'mergeUnique' 'Array-preserving merge helper is missing'
Assert-Match $script 'prependUnique' 'Rule-prepend deduplication helper is missing'

foreach ($app in @('TikTok', 'Pinduoduo', 'YouTube', 'Ximalaya', 'Zhihu', 'Bilibili')) {
    Assert-NoMatch $combined "(?i)$([regex]::Escape($app))" "Windows profile contains an app-specific rule: $app"
}

Assert-NoMatch $combined '(?im)^\s*(url|token|password|certificate):\s*(?:https?://[^\s]*[?&](?:token|key)=|gh[opusr]_|eyJ)' 'Possible secret detected'

Write-Output 'PASS: Windows Clash Verge config and script structure validation'
