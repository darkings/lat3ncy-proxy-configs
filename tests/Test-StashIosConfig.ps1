$ErrorActionPreference = 'Stop'

$repoRoot = Split-Path -Parent $PSScriptRoot
$configPath = Join-Path $repoRoot 'stash-ios.yaml'
if (-not (Test-Path -LiteralPath $configPath)) { throw 'Missing Stash iOS config' }
$config = Get-Content -LiteralPath $configPath -Raw -Encoding UTF8

function Assert-Match {
    param([string]$Pattern, [string]$Message)
    if ($config -notmatch $Pattern) { throw $Message }
}

function Assert-NoMatch {
    param([string]$Pattern, [string]$Message)
    if ($config -match $Pattern) { throw $Message }
}

foreach ($key in @('mode', 'dns', 'proxies', 'proxy-providers', 'proxy-groups', 'rule-providers', 'rules')) {
    Assert-Match "(?m)^$([regex]::Escape($key)):\s*" "Stash config missing top-level key: $key"
}

Assert-Match '(?ms)^proxies:\s*\r?\n\s+- name: Tailscale-Node\s*\r?\n\s+type: tailscale\s*\r?\n\s+hostname: ios\s*\r?\n\s+control-url: https://controlplane\.tailscale\.com\s*\r?\n\s+ephemeral: false' 'Stash native Tailscale node is incomplete'
Assert-NoMatch '(?m)^\s*auth-key:' 'Public Stash config must not contain a Tailscale auth key'
Assert-NoMatch '(?m)^\s*exit-node:' 'Public Stash config must not force a Tailscale exit node'
Assert-Match '(?m)^proxy-providers:\s*\{\}\s*$' 'Private proxy providers must remain empty in the public config'

$fakeIpMatch = [regex]::Match($config, '(?ms)^\s{2}fake-ip-filter:\s*\r?\n(.*?)(?=^\S|^\s{2}[a-zA-Z][^:]*:)')
if (-not $fakeIpMatch.Success) { throw 'Stash fake-ip-filter is missing' }
$fakeIpFilter = $fakeIpMatch.Groups[1].Value
if ($fakeIpFilter -notmatch '\+\.tailscale\.com') { throw 'Tailscale control domains must return real IPs' }
if ($fakeIpFilter -match '\+\.ts\.net') { throw 'MagicDNS names must retain fake IP mapping for routing to the native Tailscale node' }

foreach ($rule in @(
    'DOMAIN-SUFFIX,tailscale.com,DIRECT',
    'DOMAIN-SUFFIX,ts.net,Tailscale',
    'IP-CIDR,100.64.0.0/10,Tailscale,no-resolve',
    'IP-CIDR6,fd7a:115c:a1e0::/48,Tailscale,no-resolve'
)) {
    Assert-Match "(?m)^\s+- $([regex]::Escape($rule))\s*$" "Stash missing Tailscale rule: $rule"
}

$tailscalePosition = $config.IndexOf('  - DOMAIN-SUFFIX,ts.net,Tailscale')
$privatePosition = $config.IndexOf('  - RULE-SET,Private-Domain,DIRECT')
$adPosition = $config.IndexOf('  - RULE-SET,Cats-Team-AdRules,REJECT')
$finalPosition = $config.IndexOf('  - MATCH,Proxy')
if ($tailscalePosition -lt 0 -or $privatePosition -lt 0 -or $adPosition -lt 0 -or $finalPosition -lt 0 -or
    $tailscalePosition -gt $privatePosition -or $privatePosition -gt $adPosition -or $adPosition -gt $finalPosition) {
    throw 'Stash rules must order Tailscale, private networks, ads, then final routing'
}

$expectedGroups = @('Proxy', 'Tailscale', 'Spotify', 'Telegram', 'OpenAI', 'GitHub', 'Microsoft', 'Google', 'YouTube', 'TikTok', 'Auto', 'Hong Kong', 'Taiwan', 'Japan', 'Singapore', 'United States')
foreach ($group in $expectedGroups) {
    Assert-Match "(?m)^\s+- name: $([regex]::Escape($group))\s*$" "Stash missing proxy group: $group"
}
foreach ($group in @('Proxy', 'Auto', 'Hong Kong', 'Taiwan', 'Japan', 'Singapore', 'United States')) {
    $block = [regex]::Match($config, "(?ms)^\s{2}- name: $([regex]::Escape($group))\s*\r?\n.*?(?=^\s{2}- name:|^rule-providers:)").Value
    if ($block -notmatch '(?m)^\s{4}include-all:\s*true\s*$') { throw "Stash $group must include locally added providers" }
    if ($block -notmatch '\(\?!Tailscale\(\?:-Node\)\?\$\)') { throw "Stash $group must exclude the Tailscale node from ordinary proxy selection" }
}
Assert-Match '(?ms)^\s{2}- name: Tailscale\s*\r?\n\s{4}type: select\s*\r?\n\s{4}proxies:\s*\r?\n\s{6}- Tailscale-Node\s*$' 'Stash Tailscale strategy group is incomplete'
Assert-Match '(?ms)^\s{2}- name: Proxy\s*\r?\n.*?^\s{6}- Tailscale\s*$' 'Stash Proxy group must expose Tailscale as a selectable policy'

$providers = @('Cats-Team-AdRules', 'Private-Domain', 'Private-IP', 'Spotify', 'Telegram-Domain', 'Telegram-IP', 'OpenAI', 'GitHub', 'Microsoft-CN', 'Microsoft', 'Apple', 'YouTube', 'Google', 'TikTok', 'CN-Domain', 'NonCN-Domain', 'CN-IP')
foreach ($provider in $providers) {
    Assert-Match "(?m)^\s{2}$([regex]::Escape($provider)):\s*$" "Stash missing rule provider: $provider"
}
$mrsCount = [regex]::Matches($config, '(?m)^\s{4}format:\s*mrs\s*$').Count
if ($mrsCount -ne $providers.Count) { throw "Every Stash rule provider must use MRS; found $mrsCount of $($providers.Count)" }

if ($config.IndexOf('  - RULE-SET,Microsoft-CN,DIRECT') -gt $config.IndexOf('  - RULE-SET,Microsoft,Microsoft')) {
    throw 'Stash Microsoft-CN must precede broad Microsoft rules'
}
if ($config.IndexOf('  - RULE-SET,YouTube,YouTube') -gt $config.IndexOf('  - RULE-SET,Google,Google')) {
    throw 'Stash YouTube must precede broad Google rules'
}

Write-Output 'PASS: Stash iOS configuration validation'
