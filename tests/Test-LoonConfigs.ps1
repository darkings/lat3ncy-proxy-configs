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
$commonApps = @('Spotify', 'Telegram', 'OpenAI', 'GitHub', 'Microsoft', 'Apple', 'Google', 'YouTube')
$apps = @{
    iOS = $commonApps + 'TikTok'
    macOS = $commonApps[0..4] + @('Zed', 'Steam') + $commonApps[5..7]
}
$groupIcons = @{
    Proxy = 'Global'
    Spotify = 'Spotify'
    Telegram = 'Telegram'
    OpenAI = 'OpenAI'
    GitHub = 'github'
    Zed = 'https://raw.githubusercontent.com/zed-industries/zed/main/crates/zed/resources/app-icon.png'
    Microsoft = 'Microsoft'
    Steam = 'Steam'
    Apple = 'Apple'
    Google = 'Google'
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
    Assert-Match $config '(?m)^disable-stun=true\s*$' "$platform must block STUN to reduce direct-IP leakage"
    Assert-Match $config '(?m)^disconnect-on-policy-change=true\s*$' "$platform must reconnect sessions after policy changes"
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
    $subscriptionStatusTerms = @('官网', '订阅', '剩余', '流量', '套餐', '到期', '过期', '有效期', '已用', '重置', 'TRAFFIC', 'EXPIRE', 'EXPIRED', 'USED', 'TOTAL', 'REMAINING', 'RESET', 'BANDWIDTH')
    foreach ($filterName in @('全球') + $regions) {
        $filterLine = [regex]::Match($filters, "(?m)^$($filterName)节点=NameRegex,.+$").Value
        foreach ($term in $subscriptionStatusTerms) {
            Assert-Match $filterLine ([regex]::Escape($term)) "$platform $filterName node filter must exclude subscription status term: $term"
        }
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
        $iconUrl = if ($group -eq 'Zed') {
            $groupIcons[$group]
        } else {
            "https://raw.githubusercontent.com/Orz-3/mini/master/Color/$($groupIcons[$group]).png"
        }
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

    Assert-Match $groups '(?m)^Auto=url-test,\s*全球节点,\s*interval=600,' "$platform Auto must test all nodes every 600 seconds"
    foreach ($region in $regions) {
        Assert-Match $groups "(?m)^$region=url-test,\s*$($region)节点,\s*interval=600," "$platform $region must test its regional nodes every 600 seconds"
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
    if ($platform -eq 'macOS') {
        Assert-Match $remoteRules '(?m)/darkings/lat3ncy-proxy-configs/main/loon/rules/zed\.list,\s*policy=Zed,\s*tag=Zed,' 'macOS missing Zed rule'
    } else {
        Assert-NoMatch $remoteRules '(?m)/loon/rules/zed\.list,' 'iOS must not contain the desktop-only Zed rule'
    }
    Assert-Match $remoteRules '(?m)/darkings/lat3ncy-proxy-configs/main/loon/rules/microsoft-cn\.list,\s*policy=DIRECT,\s*tag=Microsoft CN,' "$platform missing Microsoft China direct rule"
    if ($remoteRules.IndexOf('/microsoft-cn.list') -gt $remoteRules.IndexOf('/Microsoft/Microsoft.list')) {
        throw "$platform Microsoft China direct rule must precede broad Microsoft rule"
    }
    foreach ($app in $apps[$platform]) {
        if ($app -in @('Apple', 'Zed')) { continue }
        Assert-Match $remoteRules "(?m)/$([regex]::Escape($app))/$([regex]::Escape($app))\.list,\s*policy=$([regex]::Escape($app))," "$platform missing remote rule for $app"
    }
    if ($remoteRules.IndexOf('/YouTube/YouTube.list') -gt $remoteRules.IndexOf('/Google/Google.list')) {
        throw "$platform YouTube rule must precede broad Google rule"
    }
    Assert-Match $remoteRules '(?m)/Repcz/Tool/X/Loon/Rules/AppleCN\.list,\s*policy=DIRECT,\s*tag=Apple CN,' "$platform missing Apple China direct rule"
    Assert-Match $remoteRules '(?m)/Repcz/Tool/X/Loon/Rules/AppleProxy\.list,\s*policy=Apple,\s*tag=Apple Proxy,' "$platform missing Apple proxy rule"
    if ($remoteRules.IndexOf('/AppleCN.list') -gt $remoteRules.IndexOf('/AppleProxy.list')) {
        throw "$platform Apple China direct rule must precede Apple proxy rule"
    }
    if ($platform -eq 'macOS') {
        Assert-Match $remoteRules '(?m)/SteamCN/SteamCN\.list,\s*policy=DIRECT,\s*tag=Steam CN,' 'macOS missing Steam China direct rule'
        if ($remoteRules.IndexOf('/SteamCN/SteamCN.list') -gt $remoteRules.IndexOf('/Steam/Steam.list')) {
            throw 'macOS Steam China direct rule must precede Steam proxy rule'
        }
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

$zedRulePath = Join-Path $repoRoot 'loon/rules/zed.list'
if (-not (Test-Path -LiteralPath $zedRulePath)) { throw 'Missing local Zed rule' }
$zedRule = Get-Content -LiteralPath $zedRulePath -Raw -Encoding UTF8
Assert-Match $zedRule '(?m)^DOMAIN-SUFFIX,zed\.dev\s*$' 'Zed rule must cover zed.dev and all service subdomains'
Assert-NoMatch $zedRule '(?m)^DOMAIN-SUFFIX,zed-industries\.com\s*$' 'Zed rule must not include an unverified company domain'

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
    'PinDuoDuo_remove_ads',
    'Remove_ads_by_keli',
    'Taobao_remove_ads',
    'Weixin_Official_Accounts_remove_ads',
    'Weixin_external_links_unlock',
    'WexinMiniPrograms_Remove_ads',
    'FleaMarket_remove_ads',
    'XiaobaiPrint_remove_ads',
    'Himalaya_remove_ads',
    'XiaChuFang_remove_ads',
    'Zhihu_remove_ads'
)
foreach ($plugin in $iosAppPlugins) {
    Assert-Match $iosPlugins "(?m)^https://kelee\.one/Tool/Loon/Lpx/$([regex]::Escape($plugin))\.lpx,.+enabled=true\s*$" "iOS missing enabled KeLee plugin: $plugin"
    Assert-NoMatch $macPlugins "(?m)/$([regex]::Escape($plugin))\.lpx," "macOS must not contain iOS app plugin: $plugin"
}
Assert-Match $iosPlugins '(?m)^https://kelee\.one/Tool/Loon/Lpx/PinDuoDuo_remove_ads\.lpx,\s*enabled=true\s*$' 'iOS missing KeLee Pinduoduo plugin'
Assert-NoMatch $iosPlugins '(?m)/loon/plugins/pinduoduo-cleanup\.lpx,' 'iOS must not load the fallback repository Pinduoduo plugin'
Assert-NoMatch $macPlugins '(?m)/PinDuoDuo_remove_ads\.lpx,' 'macOS must not contain iOS Pinduoduo plugin'
Assert-Match $iosPlugins '(?m)^https://raw\.githubusercontent\.com/fmz200/wool_scripts/main/Loon/plugin/split/partM/Meituan\.lpx,.+enabled=true\s*$' 'iOS missing enabled fmz200 plugin: Meituan'
Assert-NoMatch $macPlugins '(?m)/partM/Meituan\.lpx,' 'macOS must not contain iOS app plugin: Meituan'
$iosPluginUrls = [regex]::Matches($iosPlugins, '(?m)^https?://[^,\r\n]+') | ForEach-Object { $_.Value }
if (($iosPluginUrls | Sort-Object -Unique).Count -ne $iosPluginUrls.Count) {
    throw 'iOS plugin URLs must not contain duplicates'
}
Assert-NoMatch $iosPlugins '(?m)/TestFlightRegionUnlock\.lpx,' 'iOS must not contain TestFlight region unlock'
Assert-NoMatch $macPlugins '(?m)/TestFlightRegionUnlock\.lpx,' 'macOS must not contain TestFlight'
Assert-NoMatch (Get-Section $ios 'Proxy Group') '(?m)^Steam=' 'iOS must not expose Steam'
Assert-NoMatch (Get-Section $ios 'Remote Rule') '(?m)/Steam/Steam\.list' 'iOS must not route Steam separately'
Assert-NoMatch (Get-Section $mac 'Proxy Group') '(?m)^TikTok=' 'macOS must not expose TikTok'
Assert-NoMatch (Get-Section $mac 'Remote Rule') '(?m)/TikTok/TikTok\.list' 'macOS must not contain TikTok rules'

Write-Output 'PASS: Loon iOS and macOS config validation'
