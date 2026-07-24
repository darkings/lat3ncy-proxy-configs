$ErrorActionPreference = 'Stop'

$repoRoot = Split-Path -Parent $PSScriptRoot
$loonPluginPath = Join-Path $repoRoot 'loon/plugins/pinduoduo-cleanup.lpx'
$homepageScriptPath = Join-Path $repoRoot 'loon/scripts/pinduoduo-homepage-cleanup.js'
$scanScriptPath = Join-Path $repoRoot 'loon/scripts/pinduoduo-scan-cleanup.js'
$vendorChunkPath = Join-Path $repoRoot 'loon/vendor/pinduoduo/9410-b8806e870a26db7d.js'

foreach ($requiredPath in @($loonPluginPath, $homepageScriptPath, $scanScriptPath, $vendorChunkPath)) {
    if (-not (Test-Path -LiteralPath $requiredPath)) {
        throw "Missing Loon Pinduoduo implementation: $requiredPath"
    }
}

$loonPlugin = Get-Content -Raw -LiteralPath $loonPluginPath -Encoding UTF8
$homepageScript = Get-Content -Raw -LiteralPath $homepageScriptPath -Encoding UTF8
$scanScript = Get-Content -Raw -LiteralPath $scanScriptPath -Encoding UTF8

foreach ($section in @('Rule', 'Rewrite', 'Script', 'MITM')) {
    if ($loonPlugin -notmatch "(?m)^\[$section\]\s*$") {
        throw "Loon Pinduoduo plugin missing section: $section"
    }
}

$homepageScriptUrl = 'https://raw.githubusercontent.com/darkings/lat3ncy-proxy-configs/main/loon/scripts/pinduoduo-homepage-cleanup.js'
$scanScriptUrl = 'https://raw.githubusercontent.com/darkings/lat3ncy-proxy-configs/main/loon/scripts/pinduoduo-scan-cleanup.js'
foreach ($scriptUrl in @($homepageScriptUrl, $scanScriptUrl)) {
    if ($loonPlugin -notmatch [regex]::Escape($scriptUrl)) {
        throw "Loon plugin missing repository script URL: $scriptUrl"
    }
}

$vendorChunkUrl = 'https://raw.githubusercontent.com/darkings/lat3ncy-proxy-configs/main/loon/vendor/pinduoduo/9410-b8806e870a26db7d.js'
if ($scanScript -notmatch [regex]::Escape($vendorChunkUrl)) {
    throw 'Loon scan cleanup script must load the vendor chunk from the Loon resource tree'
}
if ($loonPlugin -match '/rewrites/' -or $scanScript -match '/rewrites/') {
    throw 'Loon Pinduoduo resources must not depend on the removed Quantumult X rewrite tree'
}

foreach ($rule in @(
    'DOMAIN, meta.pinduoduo.com, REJECT',
    'DOMAIN, cdl-1.pddpic.com, REJECT',
    'DOMAIN, titan.pinduoduo.com, REJECT'
)) {
    if ($loonPlugin -notmatch "(?m)^$([regex]::Escape($rule))\s*$") {
        throw "Loon plugin missing Pinduoduo blocking rule: $rule"
    }
}
foreach ($contract in @(
    'volantis3-open\/component',
    'api\/philo\/personal\/hub',
    'api\/oak\/integration\/render',
    'api\/cappuccino\/splash'
)) {
    if ($loonPlugin.IndexOf($contract, [StringComparison]::Ordinal) -lt 0) {
        throw "Loon plugin missing rewrite coverage: $contract"
    }
}
if ($loonPlugin -notmatch '(?m)^hostname\s*=.*api\.pinduoduo\.com.*m\.pinduoduo\.net') {
    throw 'Loon plugin MITM hostnames must cover the scripted Pinduoduo responses'
}
foreach ($contract in @('delete result\.icon_set', 'bottom_tabs', 'buffer_bottom_tabs', 'allowedBottomLinks')) {
    if ($homepageScript -notmatch $contract) {
        throw "Loon homepage script missing cleanup behavior: $contract"
    }
}

$nodeContract = @'
const { cleanHomepage } = require(process.argv[1]);
const payload = {
  result: {
    icon_set: { icons: [1] },
    search_bar_hot_query: { text: "ad" },
    dy_module: { irregular_banner_dy: { id: 1 } },
    bottom_tabs: [
      { link: "index.html" },
      { link: "chat_list.html" },
      { link: "personal.html" },
      { link: "pdd_live_tab_list.html" },
      { link: "classification.html" }
    ],
    buffer_bottom_tabs: [
      { link: "index.html" },
      { link: "chat_list.html" },
      { link: "personal.html" },
      { link: "pdd_live_tab_list.html" }
    ]
  }
};
const result = cleanHomepage(payload).result;
if ("icon_set" in result || "search_bar_hot_query" in result) process.exit(11);
if ("irregular_banner_dy" in result.dy_module) process.exit(12);
for (const key of ["bottom_tabs", "buffer_bottom_tabs"]) {
  const links = result[key].map(item => item.link);
  if (links.join(",") !== "index.html,chat_list.html,personal.html") process.exit(13);
}
console.log("PASS: Loon homepage script removes restored tabs");
'@
$nodeOutput = & node -e $nodeContract $homepageScriptPath
if ($LASTEXITCODE -ne 0 -or $nodeOutput -notcontains 'PASS: Loon homepage script removes restored tabs') {
    throw 'Loon homepage cleanup behavior contract failed'
}

Write-Output 'PASS: Loon Pinduoduo cleanup validation'
