$ErrorActionPreference = 'Stop'

$repoRoot = Split-Path -Parent $PSScriptRoot
$configs = [ordered]@{
    iOS = Join-Path $repoRoot 'loon-ios.lcf'
    macOS = Join-Path $repoRoot 'loon-macos.lcf'
}

function Assert-Match {
    param([string]$Text, [string]$Pattern, [string]$Message)
    if ($Text -notmatch $Pattern) { throw $Message }
}

function Assert-NoMatch {
    param([string]$Text, [string]$Pattern, [string]$Message)
    if ($Text -match $Pattern) { throw $Message }
}

function Get-Section {
    param([string]$Text, [string]$Name)
    $match = [regex]::Match(
        $Text,
        "(?ms)^\[$([regex]::Escape($Name))\]\s*\r?\n(.*?)(?=^\[[^\]]+\]\s*$|\z)"
    )
    if (-not $match.Success) { throw "Missing section [$Name]" }
    $match.Groups[1].Value
}

$requiredSections = @(
    'General', 'Proxy', 'Remote Proxy', 'Remote Filter', 'Proxy Group',
    'Rule', 'Remote Rule', 'Host', 'Rewrite', 'Script', 'Plugin', 'Mitm'
)
$regions = @('香港', '台湾', '日本', '新加坡', '美国')
$commonApps = @('Spotify', 'Telegram', 'OpenAI', 'GitHub', 'Microsoft', 'Apple', 'YouTube')
$apps = @{
    iOS = $commonApps + 'TikTok'
    macOS = $commonApps[0..4] + 'Steam' + $commonApps[5..6]
}
$groupIcons = @{
    Proxy = 'Global'
    Spotify = 'Spotify'
    Telegram = 'Telegram'
    OpenAI = 'OpenAI'
    GitHub = 'github'
    Microsoft = 'Microsoft'
    Steam = 'Steam'
    Apple = 'Apple'
    YouTube = 'YouTube'
    TikTok = 'TikTok'
    Auto = 'Urltest'
    香港 = 'HK'
    台湾 = 'TW'
    日本 = 'JP'
    新加坡 = 'SG'
    美国 = 'US'
}

foreach ($platform in $configs.Keys) {
    $path = $configs[$platform]
    if (-not (Test-Path -LiteralPath $path)) { throw "Missing Loon config: $([IO.Path]::GetFileName($path))" }
    $config = Get-Content -LiteralPath $path -Raw -Encoding UTF8

    foreach ($section in $requiredSections) {
        Assert-Match $config "(?m)^\[$([regex]::Escape($section))\]\s*$" "$platform missing section [$section]"
    }

    Assert-Match $config '(?m)^# Based on iKeLee Loon Auto Select Configuration\s*$' "$platform missing iKeLee attribution"
    Assert-Match $config '(?m)^# Source: https://raw\.githubusercontent\.com/luestr/ProxyResource/' "$platform missing upstream source"
    Assert-Match $config '(?m)^# License: CC BY-NC-SA 4\.0\s*$' "$platform missing license notice"
    Assert-Match $config '(?m)^ip-mode=dual\s*$' "$platform must use dual-stack IP mode"
    Assert-Match $config '(?m)^ipv6-vif=on\s*$' "$platform must enable the IPv6 virtual interface"
    Assert-Match $config '(?m)^dns-server=system\s*$' "$platform must retain system DNS"
    Assert-Match $config '(?m)^disable-stun=false\s*$' "$platform must not globally block STUN"
    $general = Get-Section $config 'General'
    foreach ($value in @('100.64.0.0/10', 'fd7a:115c:a1e0::/48', '*.ts.net', '*.tailscale.com')) {
        Assert-Match $general "(?m)^skip-proxy=.*$([regex]::Escape($value))" "$platform skip-proxy missing $value"
    }
    if ($platform -eq 'macOS') {
        Assert-Match $general '(?m)^real-ip=\*\.ts\.net,\*\.tailscale\.com\s*$' 'macOS must return real IPs for Tailscale domains'
        Assert-Match $general '(?m)^skip-proxy=127\.0\.0\.1,localhost,\*\.local,192\.168\.0\.0/16,10\.0\.0\.0/8,172\.16\.0\.0/12,100\.64\.0\.0/10,100\.100\.100\.100/32,fd7a:115c:a1e0::/48,\*\.ts\.net,\*\.tailscale\.com,e\.crashlynatics\.com\s*$' 'macOS skip-proxy must preserve the verified Tailscale routing order'
        Assert-Match $general '(?m)^bypass-tun=10\.0\.0\.0/8,127\.0\.0\.0/8,169\.254\.0\.0/16,172\.16\.0\.0/12,192\.0\.0\.0/24,192\.0\.2\.0/24,192\.88\.99\.0/24,192\.168\.0\.0/16,198\.51\.100\.0/24,203\.0\.113\.0/24,224\.0\.0\.0/4,255\.255\.255\.255/32\s*$' 'macOS bypass-tun must avoid conflicting with Tailscale routes'
        Assert-NoMatch $general '(?m)^bypass-tun=.*(?:100\.64\.0\.0/10|fd7a:115c:a1e0::/48|\*\.ts\.net|\*\.tailscale\.com)' 'macOS bypass-tun must not bypass Tailscale traffic'
    } else {
        Assert-Match $config '(?m)^real-ip=.*\*\.ts\.net.*\*\.tailscale\.com' "$platform must return real IPs for Tailscale domains"
        foreach ($value in @('100.64.0.0/10', 'fd7a:115c:a1e0::/48', '*.ts.net', '*.tailscale.com')) {
            Assert-Match $general "(?m)^bypass-tun=.*$([regex]::Escape($value))" "$platform bypass-tun missing $value"
        }
    }

    $filters = Get-Section $config 'Remote Filter'
    Assert-Match $filters '(?m)^全球节点=NameRegex,' "$platform missing global node filter"
    foreach ($region in $regions) {
        Assert-Match $filters "(?m)^$($region)节点=NameRegex," "$platform missing node filter for $region"
    }
    Assert-NoMatch $filters '(?m)^(韩国|游戏).*=NameRegex,' "$platform must not expose Korea or game filters"

    $groups = Get-Section $config 'Proxy Group'
    $topLevelOrder = @('Proxy') + $apps[$platform] + @('Auto') + $regions
    $lastPosition = -1
    foreach ($group in $topLevelOrder) {
        $position = $groups.IndexOf("$group=")
        if ($position -lt 0) { throw "$platform missing policy group: $group" }
        if ($position -le $lastPosition) { throw "$platform policy group is out of order: $group" }
        $lastPosition = $position
        $groupLine = [regex]::Match($groups, "(?m)^$([regex]::Escape($group))=.+$").Value
        $iconUrl = "https://raw.githubusercontent.com/Orz-3/mini/master/Color/$($groupIcons[$group]).png"
        Assert-Match $groupLine ",\s*img-url=$([regex]::Escape($iconUrl))\s*$" "$platform $group missing its policy-group icon"
    }

    $proxyLine = [regex]::Match($groups, '(?m)^Proxy=.+$').Value
    foreach ($choice in @('Auto', 'DIRECT') + $regions + '全球节点') {
        Assert-Match $proxyLine "(?:^|,\s*)$([regex]::Escape($choice))(?:,|$)" "$platform Proxy missing choice: $choice"
    }
    foreach ($app in $apps[$platform]) {
        Assert-NoMatch $proxyLine "(?:^|,\s*)$([regex]::Escape($app))(?:,|$)" "$platform Proxy must not contain app group: $app"
        $appLine = [regex]::Match($groups, "(?m)^$([regex]::Escape($app))=.+$").Value
        foreach ($choice in @('Proxy', 'DIRECT') + $regions) {
            Assert-Match $appLine "(?:^|,\s*)$([regex]::Escape($choice))(?:,|$)" "$platform $app missing choice: $choice"
        }
        Assert-NoMatch $appLine '(?:^|,\s*)Auto(?:,|$)' "$platform $app must not directly contain Auto"
    }

    Assert-Match $groups '(?m)^Auto=url-test,\s*全球节点,' "$platform Auto must test all nodes"
    foreach ($region in $regions) {
        Assert-Match $groups "(?m)^$region=url-test,\s*$($region)节点," "$platform $region must test its regional nodes"
    }

    $rules = Get-Section $config 'Rule'
    foreach ($pattern in @(
        '^DOMAIN-SUFFIX,\s*ts\.net,\s*DIRECT\s*$',
        '^DOMAIN-SUFFIX,\s*tailscale\.com,\s*DIRECT\s*$',
        '^IP-CIDR,\s*100\.64\.0\.0/10,\s*DIRECT,\s*no-resolve\s*$',
        '^IP-CIDR6,\s*fd7a:115c:a1e0::/48,\s*DIRECT,\s*no-resolve\s*$'
    )) {
        Assert-Match $rules "(?m)$pattern" "$platform missing a Tailscale direct rule"
    }
    if ($rules.IndexOf('DOMAIN-SUFFIX, ts.net, DIRECT') -gt $rules.IndexOf('FINAL, Proxy')) {
        throw "$platform Tailscale rules must precede FINAL"
    }

    $remoteRules = Get-Section $config 'Remote Rule'
    foreach ($app in $apps[$platform]) {
        Assert-Match $remoteRules "(?m)/$([regex]::Escape($app))/$([regex]::Escape($app))\.list,\s*policy=$([regex]::Escape($app))," "$platform missing remote rule for $app"
    }
    Assert-Match $remoteRules '(?m)/LAN_SPLITTER\.lsr,\s*policy=DIRECT,' "$platform missing LAN direct rule"
    Assert-Match $remoteRules '(?m)/rule/Loon/WeChat/WeChat\.list,\s*policy=DIRECT,\s*tag=微信转圈,\s*enabled=true' "$platform missing the native Loon WeChat direct rule"
    Assert-Match $remoteRules '(?m)/REGION_SPLITTER\.lsr,\s*policy=DIRECT,' "$platform missing China direct rule"
    if ($remoteRules.IndexOf('/rule/Loon/WeChat/WeChat.list') -gt $remoteRules.IndexOf('/REGION_SPLITTER.lsr')) {
        throw "$platform WeChat direct rule must precede the general China rule"
    }

    $plugins = Get-Section $config 'Plugin'
    foreach ($plugin in @('BlockAdvertisers', 'QuickSearch', 'Prevent_DNS_Leaks', 'Node_detection_tool', 'Sub-Store')) {
        Assert-Match $plugins "(?m)/$([regex]::Escape($plugin))\.lpx,.+enabled=true" "$platform missing enabled plugin: $plugin"
    }

    Assert-NoMatch $config '(?im)^\s*(?:ca-p12|ca-passphrase)\s*=[ \t]*\S+' "$platform must not embed certificate material"
    Assert-NoMatch $config '(?i)(?:ss|ssr|vmess|vless|trojan|hysteria2?)://|gh[opusr]_|eyJ[A-Za-z0-9_-]{20,}' "$platform may contain a node or token"
}

$ios = Get-Content -LiteralPath $configs.iOS -Raw -Encoding UTF8
$mac = Get-Content -LiteralPath $configs.macOS -Raw -Encoding UTF8
$iosPlugins = Get-Section $ios 'Plugin'
$macPlugins = Get-Section $mac 'Plugin'

foreach ($plugin in @('Block_HTTPDNS', 'BoxJs', 'Script-Hub')) {
    Assert-Match $iosPlugins "(?m)/$plugin\.lpx,.+enabled=true" "iOS missing enabled plugin: $plugin"
    Assert-NoMatch $macPlugins "(?m)/$plugin\.lpx," "macOS must not contain plugin: $plugin"
}
$iosAppPlugins = @(
    'AppleWeatherEnhancer',
    'QQ_Redirect',
    'Spotify_remove_ads',
    'Spotify_lyrics_translation',
    'Bilibili_remove_ads',
    'Amap_remove_ads',
    'JD_remove_ads',
    'Remove_ads_by_keli',
    'PinDuoDuo_remove_ads',
    'Taobao_remove_ads',
    'Weixin_Official_Accounts_remove_ads',
    'Weixin_external_links_unlock',
    'WexinMiniPrograms_Remove_ads',
    'FleaMarket_remove_ads',
    'XiaobaiPrint_remove_ads',
    'Himalaya_remove_ads'
)
foreach ($plugin in $iosAppPlugins) {
    Assert-Match $iosPlugins "(?m)^https://kelee\.one/Tool/Loon/Lpx/$([regex]::Escape($plugin))\.lpx,.+enabled=true\s*$" "iOS missing enabled KeLee plugin: $plugin"
    Assert-NoMatch $macPlugins "(?m)/$([regex]::Escape($plugin))\.lpx," "macOS must not contain iOS app plugin: $plugin"
}
$iosPluginUrls = [regex]::Matches($iosPlugins, '(?m)^https?://[^,\r\n]+') | ForEach-Object { $_.Value }
if (($iosPluginUrls | Sort-Object -Unique).Count -ne $iosPluginUrls.Count) {
    throw 'iOS plugin URLs must not contain duplicates'
}
Assert-Match $iosPlugins '(?m)/TestFlightRegionUnlock\.lpx,.+enabled=false' 'iOS must keep TestFlight disabled'
Assert-NoMatch $macPlugins '(?m)/TestFlightRegionUnlock\.lpx,' 'macOS must not contain TestFlight'
Assert-NoMatch (Get-Section $ios 'Proxy Group') '(?m)^Steam=' 'iOS must not expose Steam'
Assert-NoMatch (Get-Section $ios 'Remote Rule') '(?m)/Steam/Steam\.list' 'iOS must not route Steam separately'
Assert-NoMatch (Get-Section $mac 'Proxy Group') '(?m)^TikTok=' 'macOS must not expose TikTok'
Assert-NoMatch (Get-Section $mac 'Remote Rule') '(?m)/TikTok/TikTok\.list' 'macOS must not contain TikTok rules'

Write-Output 'PASS: Loon iOS and macOS config validation'
