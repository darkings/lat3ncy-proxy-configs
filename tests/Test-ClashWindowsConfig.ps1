$ErrorActionPreference = 'Stop'

$repoRoot = Split-Path -Parent $PSScriptRoot
$configPath = Join-Path $repoRoot 'clash-verge-windows.yaml'
if (-not (Test-Path -LiteralPath $configPath)) { throw 'Missing Windows Clash Verge config' }

$config = Get-Content -LiteralPath $configPath -Raw

function Assert-Match {
    param([string]$Pattern, [string]$Message)
    if ($config -notmatch $Pattern) { throw $Message }
}

function Assert-NoMatch {
    param([string]$Pattern, [string]$Message)
    if ($config -match $Pattern) { throw $Message }
}

Assert-Match '(?m)^allow-lan:\s*false\s*$' 'LAN proxy must be disabled by default'
Assert-Match '(?m)^\s+enable:\s*false\s*$' 'TUN must be disabled by default'
Assert-Match '(?m)^\s+stack:\s*mixed\s*$' 'TUN stack must use mixed mode'
Assert-Match '(?m)^\s+strict-route:\s*false\s*$' 'Strict route must be disabled for WSL compatibility'
Assert-Match '(?m)^\s+- 100\.64\.0\.0/10\s*$' 'Tailscale IPv4 must be excluded'
Assert-Match '(?m)^\s+- fd7a:115c:a1e0::/48\s*$' 'Tailscale IPv6 must be excluded'
Assert-Match '(?m)^\s+- "\+\.ts\.net"\s*$' 'MagicDNS must bypass fake IP'
Assert-Match '(?m)^\s+- DOMAIN-SUFFIX,ts\.net,DIRECT\s*$' 'Tailnet domains must be direct'
Assert-Match '(?m)^\s+- RULE-SET,Cats-Team-AdRules,REJECT\s*$' 'Cats-Team must be the active ad rule'
Assert-Match '(?m)^\s+url: https://raw\.githubusercontent\.com/Cats-Team/AdRules/main/adrules-mihomo\.mrs\s*$' 'Cats-Team Mihomo MRS URL is missing'
Assert-Match '(?m)^\s+interval: 21600\s*$' 'Ad rules must refresh every six hours'
Assert-NoMatch '(?im)^\s*(url|token|password|certificate):\s*(?:https?://[^\s]*[?&](?:token|key)=|gh[opusr]_|eyJ)' 'Possible secret detected'

$tailscaleRule = $config.IndexOf('  - DOMAIN-SUFFIX,ts.net,DIRECT')
$adRule = $config.IndexOf('  - RULE-SET,Cats-Team-AdRules,REJECT')
if ($tailscaleRule -lt 0 -or $adRule -lt 0 -or $tailscaleRule -gt $adRule) {
    throw 'Tailscale direct rules must precede the ad rule'
}

Write-Output 'PASS: Windows Clash Verge extension validation'
