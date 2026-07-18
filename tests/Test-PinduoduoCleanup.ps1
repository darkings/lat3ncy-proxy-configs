$ErrorActionPreference = 'Stop'

$repoRoot = Split-Path -Parent $PSScriptRoot
$snippetPath = Join-Path $repoRoot 'rewrites/pinduoduo-cleanup.snippet'
$lines = Get-Content -LiteralPath $snippetPath

function Get-RejectPattern {
    param([string]$Marker)

    $matching = @($lines | Where-Object { $_ -match $Marker -and $_ -match ' url reject\s*$' })
    if ($matching.Count -ne 1) {
        throw "Expected exactly one precise reject rule for $Marker; found $($matching.Count)"
    }
    return $matching[0] -replace '\s+url reject\s*$', ''
}

function Assert-Matches {
    param([string]$Pattern, [string]$Url, [string]$Message)
    if ($Url -notmatch $Pattern) { throw $Message }
}

function Assert-DoesNotMatch {
    param([string]$Pattern, [string]$Url, [string]$Message)
    if ($Url -match $Pattern) { throw $Message }
}

$d4Pattern = Get-RejectPattern '\\/d4\\\?'
$v2Pattern = Get-RejectPattern '\\/v2\\/d\\\?'

Assert-Matches $d4Pattern 'http://121.5.84.85/d4?appid=1&os=2&clientVersion=8.16.0&scene=3&front=1&titanId=x&type=ADDRS' 'Normal Pinduoduo d4 address discovery must be rejected'
Assert-Matches $d4Pattern 'http://114.110.97.30http://114.110.97.30/d4?appid=1&os=2&clientVersion=8.16.0&scene=3&front=1&titanId=x&type=ADDRS' 'Quantumult X duplicated-host d4 capture must be rejected'
Assert-DoesNotMatch $d4Pattern 'http://121.5.84.85/d4?appid=2&os=2&clientVersion=8.16.0&type=ADDRS' 'Other app IDs must not be rejected'
Assert-DoesNotMatch $d4Pattern 'http://121.5.84.85/d4?appid=1&os=2&clientVersion=8.16.0&type=REPORT' 'Non-address d4 traffic must not be rejected'

Assert-Matches $v2Pattern 'http://101.35.212.35/v2/d?id=45237&ttl=1&dn=ABC&type=addrs' 'Pinduoduo v2 address discovery must be rejected'
Assert-DoesNotMatch $v2Pattern 'http://101.35.212.35/v2/d?id=99999&ttl=1&dn=ABC&type=addrs' 'Other service IDs must not be rejected'
Assert-DoesNotMatch $v2Pattern 'http://101.35.212.35/v2/d?id=45237&ttl=1&dn=ABC&type=report' 'Non-address v2 traffic must not be rejected'

$broadBlocking = $lines | Where-Object { $_ -notmatch '^\s*(//|#|;)' -and $_ -match '(?i)QUIC|udp_drop_list|443.*reject' }
if ($broadBlocking) {
    throw "Broad QUIC/UDP blocking is not allowed: $($broadBlocking -join '; ')"
}

Write-Output 'PASS: Pinduoduo address-discovery rules are precise'
