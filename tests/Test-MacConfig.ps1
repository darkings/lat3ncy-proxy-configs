$ErrorActionPreference = 'Stop'

$repoRoot = Split-Path -Parent $PSScriptRoot
$configPath = Join-Path $repoRoot 'quantumultx-macos.conf'

if (-not (Test-Path -LiteralPath $configPath)) {
    throw 'Missing config: quantumultx-macos.conf'
}

$config = Get-Content -LiteralPath $configPath -Raw -Encoding UTF8
$activeLines = $config -split "`r?`n" | Where-Object {
    $_ -notmatch '^\s*[#;]' -and $_ -match '\S'
}

function Assert-Match {
    param([string]$Pattern, [string]$Message)
    if ($config -notmatch $Pattern) { throw $Message }
}

function Assert-NoMatch {
    param([string]$Pattern, [string]$Message)
    if ($config -match $Pattern) { throw $Message }
}

$requiredSections = @(
    'general', 'dns', 'policy', 'server_local', 'server_remote',
    'filter_local', 'filter_remote', 'rewrite_local', 'rewrite_remote',
    'task_local', 'http_backend', 'mitm'
)
foreach ($section in $requiredSections) {
    Assert-Match "(?m)^\[$([regex]::Escape($section))\]\s*$" "Missing section [$section]"
}

Assert-Match '(?m)^dns_exclusion_list\s*=.*\*\.ts\.net' 'MagicDNS *.ts.net must be excluded from fake-IP handling'
Assert-Match '(?m)^excluded_routes\s*=.*100\.64\.0\.0/10' 'Tailscale IPv4 range must bypass Quantumult X'
Assert-Match '(?m)^ip6-cidr\s*,\s*fd7a:115c:a1e0::/48\s*,\s*direct\s*$' 'Tailscale IPv6 range must be the first-class direct rule'
Assert-Match '(?m)^host-suffix\s*,\s*ts\.net\s*,\s*direct\s*$' 'Tailnet names must be direct'
Assert-Match '(?m)^host-suffix\s*,\s*tailscale\.com\s*,\s*direct\s*$' 'Tailscale control and DERP domains must be direct'
Assert-NoMatch '(?m)^dns_exclusion_list\s*=.*(?:^|,\s*)cvat(?:\s*,|\s*$)' 'The profile must not special-case the cvat short hostname'
Assert-NoMatch '(?m)^server\s*=\s*/cvat/system\s*$' 'The profile must not override DNS for the cvat short hostname'
Assert-NoMatch '(?m)^host\s*,\s*cvat\s*,\s*direct\s*$' 'The profile must not contain a cvat short-host routing rule'

Assert-NoMatch '(?m)^no-system\s*$' 'System DNS must remain enabled for Tailscale MagicDNS'
Assert-NoMatch '(?m)^no-ipv6\s*$' 'IPv6 must remain enabled for Tailscale'
Assert-NoMatch '(?m)^udp_drop_list\s*=' 'The Mac profile must not globally block QUIC/UDP 443'
Assert-NoMatch '(?i)KOP-XIAO' 'KOP-XIAO resources are intentionally excluded'
Assert-NoMatch '(?im)^\s*(password|token|passphrase|p12|certificate)\s*=' 'Sensitive credentials must not be embedded'

Assert-Match '(?m)^https://raw\.githubusercontent\.com/TG-Twilight/AWAvenue-Ads-Rule/.+update-interval=21600,.+opt-parser=false,.+enabled=false\s*$' 'AWAvenue must be a disabled six-hour fallback'
Assert-NoMatch '(?i)anti-ad\.net|surge2\.txt' 'The Mac profile must not include a Surge-only anti-AD resource'
Assert-Match '(?m)^https://raw\.githubusercontent\.com/Cats-Team/AdRules/main/qx\.conf,.+update-interval=21600,.+opt-parser=false,.+enabled=true\s*$' 'Cats-Team must be the enabled six-hour QX ad list'
Assert-Match '(?m)^https://limbopro\.com/Adblock4limbo\.(list|conf).+enabled=false\s*$' 'Adblock4limbo must not be enabled by default'
Assert-Match '(?m)^https://raw\.githubusercontent\.com/fmz200/wool_scripts/.+/XWebAds\.snippet.+enabled=true\s*$' 'XWebAds must be enabled'
Assert-Match '(?m)^https://raw\.githubusercontent\.com/zqzess/.+/Qsearch\.qxrewrite.+enabled=true\s*$' 'Qsearch must be enabled'
Assert-Match '(?m)^event-interaction\s+https://raw\.githubusercontent\.com/fmz200/.+/server_info\.js.+enabled=true\s*$' 'Node details action must be enabled'

$activeCron = $activeLines | Where-Object { $_ -match '^\s*(?:\S+\s+){4,5}https?://' -and $_ -notmatch 'enabled=false\s*$' }
if ($activeCron) {
    throw "Cron tasks must be disabled by default: $($activeCron -join '; ')"
}

$policyNames = [System.Collections.Generic.HashSet[string]]::new([System.StringComparer]::OrdinalIgnoreCase)
@('direct', 'proxy', 'reject') | ForEach-Object { [void]$policyNames.Add($_) }
foreach ($line in $activeLines) {
    if ($line -match '^(?:static|available|round-robin|dest-hash|url-latency-benchmark|ssid)\s*=\s*([^,]+)') {
        [void]$policyNames.Add($Matches[1].Trim())
    }
}

$referencedPolicies = [System.Collections.Generic.HashSet[string]]::new([System.StringComparer]::OrdinalIgnoreCase)
foreach ($line in $activeLines) {
    if ($line -match '^final\s*,\s*([^,]+)') {
        [void]$referencedPolicies.Add($Matches[1].Trim())
    } elseif ($line -match '^(?:host|host-suffix|host-keyword|host-wildcard|ip-cidr|ip6-cidr|geoip|user-agent)\s*,[^,]+,\s*([^,]+)') {
        [void]$referencedPolicies.Add($Matches[1].Trim())
    }
    if ($line -match 'force-policy=([^,]+)') {
        [void]$referencedPolicies.Add($Matches[1].Trim())
    }
}

$missingPolicies = $referencedPolicies | Where-Object { -not $policyNames.Contains($_) }
if ($missingPolicies) {
    throw "Undefined policy reference(s): $($missingPolicies -join ', ')"
}

Write-Output 'PASS: macOS Quantumult X config validation'
