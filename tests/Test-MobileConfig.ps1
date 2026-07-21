$ErrorActionPreference = 'Stop'

$repoRoot = Split-Path -Parent $PSScriptRoot
$configPath = Join-Path $repoRoot 'quantumultx.conf'

if (-not (Test-Path -LiteralPath $configPath)) {
    throw 'Missing config: quantumultx.conf'
}

$config = Get-Content -LiteralPath $configPath -Raw -Encoding UTF8

function Assert-Match {
    param([string]$Pattern, [string]$Message)
    if ($config -notmatch $Pattern) { throw $Message }
}

Assert-Match '(?m)^dns_exclusion_list=.*\*\.ts\.net.*\*\.tailscale\.com\s*$' 'Tailscale domains must bypass fake-IP handling'
Assert-Match '(?m)^excluded_routes=.*100\.64\.0\.0/10\s*$' 'Tailscale IPv4 range must bypass Quantumult X'
Assert-Match '(?m)^ip-cidr\s*,\s*100\.64\.0\.0/10\s*,\s*direct\s*$' 'Tailnet IPv4 must be direct'
Assert-Match '(?m)^ip6-cidr\s*,\s*fd7a:115c:a1e0::/48\s*,\s*direct\s*$' 'Tailnet IPv6 must be direct'
Assert-Match '(?m)^host-suffix\s*,\s*ts\.net\s*,\s*direct\s*$' 'MagicDNS names must be direct'
Assert-Match '(?m)^host-suffix\s*,\s*tailscale\.com\s*,\s*direct\s*$' 'Tailscale control domains must be direct'
Assert-Match '(?m)^hostname\s*=.*-\*\.ts\.net.*-\*\.tailscale\.com\s*$' 'Tailscale domains must bypass MITM'

$tailscalePosition = $config.IndexOf('ip-cidr, 100.64.0.0/10, direct')
$firstExistingRulePosition = $config.IndexOf('host-suffix, amemv.com, 国内服务')
if ($tailscalePosition -lt 0 -or $firstExistingRulePosition -lt 0 -or $tailscalePosition -gt $firstExistingRulePosition) {
    throw 'Tailscale rules must precede existing application routing rules'
}

Write-Output 'PASS: mobile Quantumult X Tailscale validation'
