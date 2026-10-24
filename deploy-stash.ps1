#Requires -Version 5.1

<#
.SYNOPSIS
零凭据部署脚本：本地构建 KeLee Loon→Stash 产物，上传后通过云端的共享发布事务
（deploy/stash-updater/publish-release.sh）原子切换版本、重建容器、按内容哈希做
健康检查，失败自动回滚；随后渲染/安装云端 systemd 定时任务。

.DESCRIPTION
- 本地只做构建与上传；发布指针切换、锁、健康检查、回滚、保留策略全部由云端
  publish-release.sh 执行，手动部署与云端 timer 共用同一个实现（单锁
  <RemoteRoot>/updater/update.lock）。
- 公共站点地址只有一个来源：-PublicHost、KELEE_PUBLIC_BASE、STASH_PUBLIC_HOST
  （可选 KELEE_MIRROR_BASE 必须与之一致）。同时提供多个且不一致时脚本直接报错；
  部署文件不再内置任何生产域名。
- -DryRun 只做参数校验并打印执行计划：不运行转换器、不联网、不写本地产物、
  不执行 ssh/scp/tar。
- -BuildOnly 只构建并校验本地产物，不接触远端（需要联网读取上游，仅在真正构建时）。
- 不保存密码、私钥、Token；SSH 认证交给 ~/.ssh/config、ssh-agent 或系统凭据管理器，
  并强制 BatchMode，认证不可用时直接失败。

.EXAMPLE
.\deploy-stash.ps1 -PublicHost stash.example.com -DryRun
.EXAMPLE
.\deploy-stash.ps1 -PublicHost stash.example.com -BuildOnly
.EXAMPLE
.\deploy-stash.ps1 -PublicHost stash.example.com
.EXAMPLE
.\deploy-stash.ps1 -PublicHost stash.example.com -RollbackTo 20260101-030000-000
#>

[CmdletBinding()]
param(
    [string]$SshTarget = "jie",
    [string]$RemoteRoot = "/opt/stash",
    [string]$PublicHost = "",
    [string]$SourceDir = "stash/overrides/kelee",
    [string]$BuilderScript = "scripts/check_kelee_update.py",
    [string[]]$BuilderArgs = @("--force"),
    [int]$HttpPort = 8080,
    [string]$ContainerName = "stash",
    [int]$KeepReleases = 3,
    [int]$KeepAssetDays = 30,
    [string]$CaddySiteRoot = "/srv",
    [int]$ConnectTimeoutSec = 15,
    [string]$RollbackTo = "",
    [switch]$SkipPublicCheck,
    [switch]$SkipCaddyCheck,
    [switch]$InstallCaddyfile,
    [switch]$StrictCacheHeaders,
    [switch]$AllowBaseChange,
    [switch]$SkipBuild,
    [switch]$DryRun,
    [switch]$BuildOnly
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

function Write-Step {
    param([string]$Message)
    Write-Host "[部署] $Message" -ForegroundColor DarkCyan
}

function Write-PlanLine {
    param([string]$Message)
    Write-Host "  $Message"
}

function Assert-Pattern {
    param(
        [Parameter(Mandatory)][string]$Value,
        [Parameter(Mandatory)][string]$Name,
        [Parameter(Mandatory)][string]$Pattern,
        [Parameter(Mandatory)][string]$Message
    )
    if ($Value -notmatch $Pattern) {
        throw "$Name $Message：$Value"
    }
}

function Resolve-NormalizedRemoteRoot {
    param([Parameter(Mandatory)][string]$Value)

    $normalized = $Value.Trim()
    if ([string]::IsNullOrWhiteSpace($normalized)) { throw "RemoteRoot 不能为空。" }
    if (-not $normalized.StartsWith("/")) { throw "RemoteRoot 必须是绝对路径：$Value" }
    while ($normalized.EndsWith("/")) { $normalized = $normalized.Substring(0, $normalized.Length - 1) }
    if ($normalized -eq "" -or $normalized -eq "/") { throw "RemoteRoot 不能是根目录 /。" }
    Assert-Pattern -Value $normalized -Name "RemoteRoot" -Pattern '^/[A-Za-z0-9._/-]+$' -Message "只允许字母数字 . _ - /"
    if ($normalized -match '(^|/)\.\.?(/|$)') { throw "RemoteRoot 不能包含 . 或 .. 路径段：$normalized" }
    if ($normalized -match '//') { throw "RemoteRoot 不能包含连续斜杠：$normalized" }
    if ($normalized.Length -gt 200) { throw "RemoteRoot 过长：$normalized" }
    return $normalized
}

function ConvertTo-NormalizedHost {
    param(
        [Parameter(Mandatory)][string]$Value,
        [Parameter(Mandatory)][string]$Name
    )

    $text = $Value.Trim()
    if ([string]::IsNullOrWhiteSpace($text)) { throw "$Name 不能为空。" }
    $scheme = ""
    $hostPart = $text
    if ($text -match '^([A-Za-z][A-Za-z0-9+.-]*)://') {
        $scheme = $Matches[1].ToLowerInvariant()
        if ($scheme -ne "https" -and $scheme -ne "http") {
            throw "$Name 只支持 http/https：$Value"
        }
        $hostPart = $text.Substring($Matches[0].Length)
    }
    $hostPart = $hostPart.TrimEnd("/")
    if ($hostPart -match '[/?#]') { throw "$Name 只能是域名，不能包含路径/查询：$Value" }
    if ($hostPart -match '@') { throw "$Name 不能包含认证信息：$Value" }
    if ($hostPart -match ':') { throw "$Name 不能包含端口，请使用 -HttpPort 配置本地健康检查端口：$Value" }
    if ($hostPart -notmatch '^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$') {
        throw "$Name 不是合法域名：$Value"
    }
    if ($hostPart.Contains("..") -or $hostPart.Length -gt 253) { throw "$Name 不是合法域名：$Value" }
    return [pscustomobject]@{
        Name   = $Name
        Host   = $hostPart.ToLowerInvariant()
        Scheme = $scheme
        Raw    = $Value
    }
}

function Resolve-PublicConfiguration {
    param([string]$PublicHostParam)

    $sources = @()
    if (-not [string]::IsNullOrWhiteSpace($PublicHostParam)) {
        $sources += ConvertTo-NormalizedHost -Value $PublicHostParam -Name "-PublicHost"
    }
    foreach ($envName in @("KELEE_PUBLIC_BASE", "STASH_PUBLIC_HOST", "KELEE_MIRROR_BASE")) {
        $envValue = [Environment]::GetEnvironmentVariable($envName)
        if (-not [string]::IsNullOrWhiteSpace($envValue)) {
            $sources += ConvertTo-NormalizedHost -Value $envValue -Name $envName
        }
    }

    if ($sources.Count -eq 0) {
        throw ("未设置公共站点地址。请用 -PublicHost <域名> 传入，或设置环境变量 " +
            "KELEE_PUBLIC_BASE=https://<域名> / STASH_PUBLIC_HOST=<域名>。" +
            "部署文件不再内置生产域名。")
    }

    $primary = $sources[0]
    $sourceSummary = ($sources | ForEach-Object { "$($_.Name)=$($_.Raw)" }) -join "；"
    foreach ($source in $sources) {
        if ($source.Host -ne $primary.Host) {
            throw "公共地址来源不一致：$sourceSummary。请只保留一个地址来源后再部署。"
        }
        if ($source.Scheme -ne "" -and $primary.Scheme -ne "" -and $source.Scheme -ne $primary.Scheme) {
            throw "公共地址协议不一致：$sourceSummary。"
        }
        if ($source.Scheme -ne "" -and $primary.Scheme -eq "") { $primary.Scheme = $source.Scheme }
    }

    $scheme = $primary.Scheme
    if ([string]::IsNullOrWhiteSpace($scheme)) { $scheme = "https" }
    $base = "${scheme}://$($primary.Host)"
    return [pscustomobject]@{
        Host       = $primary.Host
        Scheme     = $scheme
        Base       = $base
        MirrorBase = $base
        Sources    = $sourceSummary
    }
}

function Resolve-RepoPath {
    param(
        [Parameter(Mandatory)][string]$RepoRoot,
        [Parameter(Mandatory)][string]$Path
    )
    if ([IO.Path]::IsPathRooted($Path)) { return [IO.Path]::GetFullPath($Path) }
    $candidate = Join-Path $RepoRoot ($Path -replace '\\', '/')
    return [IO.Path]::GetFullPath($candidate)
}

function Get-NativeTool {
    param([Parameter(Mandatory)][string[]]$Names)

    foreach ($name in $Names) {
        $command = Get-Command -Name $name -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($null -ne $command) {
            if ($command.PSObject.Properties.Name -contains "Path" -and $command.Path) { return $command.Path }
            if ($command.PSObject.Properties.Name -contains "Source" -and $command.Source) { return $command.Source }
        }
    }
    throw "找不到所需命令：$($Names -join ', ')。请先安装对应工具。"
}

function Get-FileSha256 {
    param([Parameter(Mandatory)][string]$Path)
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

# ---------------------------------------------------------------------------
# Bundle validation (deploy side, independent from the cloud validator)
# ---------------------------------------------------------------------------

function Test-BundleArtifacts {
    param(
        [Parameter(Mandatory)][string]$Path,
        [Parameter(Mandatory)]$Configuration
    )

    if (-not (Test-Path -LiteralPath $Path -PathType Container)) {
        throw "找不到待部署产物目录：$Path（先运行构建，或检查 -SourceDir）"
    }
    $indexPath = Join-Path $Path "index.html"
    if (-not (Test-Path -LiteralPath $indexPath -PathType Leaf)) { throw "产物缺少 index.html：$indexPath" }
    if ((Get-Item -LiteralPath $indexPath).Length -eq 0) { throw "index.html 为空：$indexPath" }

    $targetsPath = Join-Path $Path "targets.json"
    if (-not (Test-Path -LiteralPath $targetsPath -PathType Leaf)) { throw "产物缺少 targets.json：$targetsPath" }
    try {
        $targets = @(Get-Content -LiteralPath $targetsPath -Raw -Encoding UTF8 | ConvertFrom-Json)
    } catch {
        throw "targets.json 解析失败：$($_.Exception.Message)"
    }
    if ($targets.Count -eq 0) { throw "targets.json 为空：$targetsPath" }

    $overrides = @()
    foreach ($target in $targets) {
        $filename = ""
        if ($null -ne $target.PSObject.Properties["filename"]) { $filename = [string]$target.filename }
        if ([string]::IsNullOrWhiteSpace($filename)) { throw "targets.json 条目缺少 filename。" }
        if ($filename.EndsWith(".lpx")) { $override = $filename.Substring(0, $filename.Length - 4) + ".stoverride" }
        elseif ($filename.EndsWith(".stoverride")) { $override = $filename }
        else { $override = "$filename.stoverride" }
        if ($override -match '[/\\]' -or $override.StartsWith(".")) { throw "targets.json 中不允许的路径：$override" }
        $overridePath = Join-Path $Path $override
        if (-not (Test-Path -LiteralPath $overridePath -PathType Leaf)) {
            throw "产物缺少目标覆写文件：$override"
        }
        $overrides += $override
    }

    $providerPath = Join-Path $Path "kelee-scripts.stoverride"
    if (-not (Test-Path -LiteralPath $providerPath -PathType Leaf)) { throw "产物缺少脚本 provider 合集：kelee-scripts.stoverride" }
    $providerText = Get-Content -LiteralPath $providerPath -Raw -Encoding UTF8
    if ($providerText -notmatch '(?m)^script-providers:') { throw "kelee-scripts.stoverride 缺少 script-providers 段。" }
    if (-not $providerText.Contains("$($Configuration.Base)/")) {
        throw "kelee-scripts.stoverride 未使用本次部署地址 $($Configuration.Base)/（构建时 KELEE_PUBLIC_BASE 可能不一致）"
    }

    $scriptsPath = Join-Path $Path "scripts"
    if (-not (Test-Path -LiteralPath $scriptsPath -PathType Container)) { throw "产物缺少 scripts/ 目录：$scriptsPath" }
    $scriptFiles = @(Get-ChildItem -LiteralPath $scriptsPath -File -Recurse -Force)
    if ($scriptFiles.Count -eq 0) { throw "产物 scripts/ 目录为空：$scriptsPath" }

    $indexText = Get-Content -LiteralPath $indexPath -Raw -Encoding UTF8
    if (-not $indexText.Contains("$($Configuration.Base)/")) {
        throw "index.html 未使用本次部署地址 $($Configuration.Base)/（构建时 KELEE_PUBLIC_BASE 可能不一致）"
    }
    if ($Configuration.Host -notmatch '^stash\.') {
        # Guard against a bundle built for a hostname that still looks like the
        # old self-hosted site (generic pattern, no production hostname is
        # hardcoded in any deploy file).
        foreach ($textToScan in @($indexText, $providerText)) {
            foreach ($match in [regex]::Matches([string]$textToScan, 'https://([A-Za-z0-9.-]+)/')) {
                $foundHost = $match.Groups[1].Value
                if ($foundHost -ne $Configuration.Host -and $foundHost -match '^stash\.') {
                    throw "产物包含非本次部署地址的 stash.* 域名：$foundHost；请确认构建时使用统一地址后再部署。"
                }
            }
        }
    }

    # Placeholders that must have been resolved at build time.
    $yamlFiles = @(Get-ChildItem -LiteralPath $Path -Filter "*.stoverride" -File -Force)
    foreach ($file in $yamlFiles) {
        $text = Get-Content -LiteralPath $file.FullName -Raw -Encoding UTF8
        if ($text -match '\$\{[A-Za-z_][A-Za-z0-9_.]*\}') {
            throw "$($file.Name) 仍包含未展开的 Loon 占位符 `${{...}}。"
        }
    }

    # Every hashed asset referenced by the published text must exist.
    $referenced = New-Object System.Collections.Generic.HashSet[string]
    $referenceSources = @($indexText, $providerText)
    foreach ($file in $yamlFiles) { $referenceSources += (Get-Content -LiteralPath $file.FullName -Raw -Encoding UTF8) }
    foreach ($sourceText in $referenceSources) {
        foreach ($match in [regex]::Matches([string]$sourceText, 'scripts/[A-Za-z0-9][A-Za-z0-9._-]*\.js')) {
            [void]$referenced.Add($match.Value)
        }
    }
    $hashedCount = 0
    foreach ($rel in $referenced) {
        $filePath = Join-Path $Path ($rel -replace '/', [IO.Path]::DirectorySeparatorChar)
        if (-not (Test-Path -LiteralPath $filePath -PathType Leaf)) {
            throw "产物引用了不存在的脚本资源：$rel"
        }
        if ($rel -match '^scripts/[0-9a-f]{12}-[A-Za-z0-9._-]+\.js$') { $hashedCount++ }
    }
    if ($hashedCount -eq 0) {
        throw "产物没有引用任何内容寻址脚本（scripts/<sha12>-<slug>.js）；检查构建器是否启用自托管资产。"
    }

    # Optional build manifest: must agree with the deployment address and bytes.
    $fingerprint = ""
    $manifestPath = Join-Path $Path ".build-manifest.json"
    if (Test-Path -LiteralPath $manifestPath -PathType Leaf) {
        try {
            $manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
        } catch {
            throw ".build-manifest.json 解析失败：$($_.Exception.Message)"
        }
        if ($null -ne $manifest.PSObject.Properties["fingerprint"]) { $fingerprint = [string]$manifest.fingerprint }
        if ($null -ne $manifest.PSObject.Properties["public_base"]) {
            $manifestBase = [string]$manifest.public_base
            if (-not [string]::IsNullOrWhiteSpace($manifestBase) -and $manifestBase -ne $Configuration.Base) {
                throw "构建清单 public_base=$manifestBase 与部署地址 $($Configuration.Base) 不一致。"
            }
        }
        $outputs = $null
        if ($null -ne $manifest.PSObject.Properties["outputs"]) { $outputs = $manifest.outputs }
        if ($null -eq $outputs) { throw ".build-manifest.json 缺少 outputs。" }
        $outputCount = 0
        foreach ($property in $outputs.PSObject.Properties) {
            $rel = [string]$property.Name
            if ($rel -match '(^|/)\.\.(/|$)' -or $rel.StartsWith("/")) { throw "构建清单包含不安全路径：$rel" }
            $expectedSha = ""
            if ($null -ne $property.Value.PSObject.Properties["sha256"]) { $expectedSha = [string]$property.Value.sha256 }
            if ($expectedSha -notmatch '^[0-9a-fA-F]{64}$') { throw "构建清单 outputs 缺少合法 sha256：$rel" }
            $filePath = Join-Path $Path ($rel -replace '/', [IO.Path]::DirectorySeparatorChar)
            if (-not (Test-Path -LiteralPath $filePath -PathType Leaf)) { throw "构建清单列出的文件不存在：$rel" }
            $actualSha = Get-FileSha256 -Path $filePath
            if ($actualSha -ne $expectedSha.ToLowerInvariant()) { throw "构建清单哈希不一致：$rel" }
            $outputCount++
        }
        if ($outputCount -eq 0) { throw ".build-manifest.json 的 outputs 为空。" }
        Write-Step "构建清单校验通过：fingerprint=$fingerprint"
    } else {
        Write-Host "[部署] WARN: 产物缺少 .build-manifest.json（旧版构建器），仅做结构校验。" -ForegroundColor Yellow
    }

    $allFiles = @(Get-ChildItem -LiteralPath $Path -File -Recurse -Force)
    return [pscustomobject]@{
        Path         = $Path
        FileCount    = $allFiles.Count
        Targets      = $targets.Count
        Overrides    = $overrides
        IndexHash    = Get-FileSha256 -Path $indexPath
        Fingerprint  = $fingerprint
        HashedAssets = $hashedCount
    }
}

function New-ExpectedSumsFile {
    param(
        [Parameter(Mandatory)][string]$Root,
        [Parameter(Mandatory)][string]$OutFile
    )
    $lines = New-Object System.Collections.Generic.List[string]
    $files = @(Get-ChildItem -LiteralPath $Root -File -Recurse -Force | Sort-Object FullName)
    foreach ($file in $files) {
        $relative = $file.FullName.Substring($Root.Length).TrimStart([char[]]@('\', '/')).Replace('\', '/')
        $lines.Add((Get-FileSha256 -Path $file.FullName) + "  " + $relative)
    }
    $text = ($lines -join "`n") + "`n"
    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [IO.File]::WriteAllText($OutFile, $text, $utf8NoBom)
}

# ---------------------------------------------------------------------------
# Build (local, no ssh)
# ---------------------------------------------------------------------------

function Invoke-LocalBuild {
    param(
        [Parameter(Mandatory)][string]$BuilderPath,
        [Parameter(Mandatory)][string[]]$BuilderArguments,
        [Parameter(Mandatory)]$Configuration
    )

    if (-not (Test-Path -LiteralPath $BuilderPath -PathType Leaf)) {
        throw "找不到构建入口：$BuilderPath"
    }

    $python = Get-NativeTool -Names @("python3", "python", "python.exe", "python3.exe")
    $savedBase = [Environment]::GetEnvironmentVariable("KELEE_PUBLIC_BASE")
    $savedMirror = [Environment]::GetEnvironmentVariable("KELEE_MIRROR_BASE")
    $savedHost = [Environment]::GetEnvironmentVariable("STASH_PUBLIC_HOST")
    try {
        [Environment]::SetEnvironmentVariable("KELEE_PUBLIC_BASE", $Configuration.Base)
        [Environment]::SetEnvironmentVariable("KELEE_MIRROR_BASE", $Configuration.MirrorBase)
        [Environment]::SetEnvironmentVariable("STASH_PUBLIC_HOST", $Configuration.Host)
        Write-Step "运行本地构建：$python $BuilderPath $($BuilderArguments -join ' ')"
        Write-Step "构建地址注入：KELEE_PUBLIC_BASE=$($Configuration.Base)"
        $buildOutput = @(& $python $BuilderPath @BuilderArguments 2>&1)
        $buildExitCode = $LASTEXITCODE
        foreach ($line in $buildOutput) { Write-Host ([string]$line) }
        if ($buildExitCode -notin @(0, 2)) {
            throw "构建失败（退出码 $buildExitCode）。构建器未写入完整产物时不会继续部署。"
        }
        Write-Step "本地构建完成（退出码 $buildExitCode；0=无变化，2=有更新）"
    } finally {
        [Environment]::SetEnvironmentVariable("KELEE_PUBLIC_BASE", $savedBase)
        [Environment]::SetEnvironmentVariable("KELEE_MIRROR_BASE", $savedMirror)
        [Environment]::SetEnvironmentVariable("STASH_PUBLIC_HOST", $savedHost)
    }
}

# ---------------------------------------------------------------------------
# Remote helpers (only reachable for !DryRun / !BuildOnly)
# ---------------------------------------------------------------------------

function Invoke-RemoteRaw {
    param(
        [Parameter(Mandatory)][string]$Command,
        [string]$Description = "远端命令"
    )

    Write-Host "[远端] $Description" -ForegroundColor DarkGray
    $output = @(& $script:SshTool -o BatchMode=yes -o ConnectTimeout=$ConnectTimeoutSec $SshTarget $Command 2>&1)
    $exitCode = $LASTEXITCODE
    $text = (($output | ForEach-Object { [string]$_ }) -join "`n")
    return [pscustomobject]@{ ExitCode = $exitCode; Output = $text }
}

function Invoke-Remote {
    param(
        [Parameter(Mandatory)][string]$Command,
        [Parameter(Mandatory)][string]$Description
    )
    $result = Invoke-RemoteRaw -Command $Command -Description $Description
    if ($result.ExitCode -ne 0) {
        $tail = ($result.Output -split "`n" | Select-Object -Last 8) -join "`n"
        throw "远端命令失败（退出码 $($result.ExitCode)）：$Description`n$tail"
    }
    return $result.Output
}

function Copy-ToRemote {
    param(
        [Parameter(Mandatory)][string]$LocalPath,
        [Parameter(Mandatory)][string]$RemotePath,
        [Parameter(Mandatory)][string]$Description
    )
    if (-not (Test-Path -LiteralPath $LocalPath -PathType Leaf)) { throw "待上传文件不存在：$LocalPath" }
    $destination = "{0}:{1}" -f $SshTarget, $RemotePath
    Write-Step "上传 $Description：$LocalPath -> $destination"
    $output = @(& $script:ScpTool -o BatchMode=yes -o ConnectTimeout=$ConnectTimeoutSec $LocalPath $destination 2>&1)
    $exitCode = $LASTEXITCODE
    foreach ($line in $output) { Write-Host ([string]$line) }
    if ($exitCode -ne 0) { throw "上传失败（退出码 $exitCode）：$LocalPath -> $destination" }
}

function Assert-RemoteFileHash {
    param(
        [Parameter(Mandatory)][string]$RemotePath,
        [Parameter(Mandatory)][string]$ExpectedHash,
        [Parameter(Mandatory)][string]$Description
    )
    $output = Invoke-Remote -Command "sha256sum '$RemotePath'" -Description "校验 $Description 上传哈希"
    $actual = ""
    foreach ($line in ($output -split "`r?`n")) {
        if ($line -match '^([0-9a-fA-F]{64})\s') { $actual = $Matches[1].ToLowerInvariant(); break }
    }
    if ($actual -ne $ExpectedHash) {
        throw "$Description 上传后哈希不一致：本地 $ExpectedHash，远端 $actual"
    }
    Write-Step "$Description 上传哈希校验通过"
}

function New-TarArchive {
    param(
        [Parameter(Mandatory)][string]$Root,
        [Parameter(Mandatory)][string]$Archive,
        [Parameter(Mandatory)][string[]]$Entries
    )
    & $script:TarTool -C $Root -cf $Archive @Entries
    if ($LASTEXITCODE -ne 0) { throw "tar 打包失败（退出码 $LASTEXITCODE）：$Root" }
}

function New-RemoteEnvironmentPrefix {
    param([Parameter(Mandatory)]$Configuration, [Parameter(Mandatory)][string[]]$ExtraPairs)

    $pairs = @(
        "STASH_REMOTE_ROOT='$script:RemoteRoot'",
        "STASH_UPDATER_ROOT='$script:RemoteUpdaterRoot'",
        "KELEE_PUBLIC_BASE='$($Configuration.Base)'",
        "KELEE_MIRROR_BASE='$($Configuration.MirrorBase)'",
        "STASH_PUBLIC_HOST='$($Configuration.Host)'",
        "STASH_HTTP_PORT='$HttpPort'",
        "STASH_CONTAINER_NAME='$ContainerName'",
        "STASH_KEEP_RELEASES='$KeepReleases'",
        "STASH_KEEP_ASSET_DAYS='$KeepAssetDays'"
    )
    $pairs += $ExtraPairs
    return ($pairs -join " ")
}

function Read-RemoteEnvironmentFile {
    param([Parameter(Mandatory)][string]$RemotePath)

    $result = Invoke-RemoteRaw -Command "if test -f '$RemotePath'; then cat '$RemotePath'; fi" -Description "读取远端 EnvironmentFile"
    $map = @{}
    foreach ($line in ($result.Output -split "`r?`n")) {
        if ($line -match '^([A-Za-z_][A-Za-z0-9_]*)=(.*)$') { $map[$Matches[1]] = $Matches[2] }
    }
    return $map
}

function Invoke-RemotePreflight {
    param([Parameter(Mandatory)]$Configuration)

    Invoke-Remote -Description "测试 SSH 连接（BatchMode）" -Command "true" | Out-Null
    Invoke-Remote -Description "检查远端命令" -Command (
        'for c in bash flock rsync sha256sum sed mv ln rm readlink realpath install find tar python3 curl docker systemctl; do ' +
        'command -v "$c" >/dev/null 2>&1 || { echo "MISSING:$c"; exit 1; }; done') | Out-Null
    Invoke-Remote -Description "检查 docker compose 可用" -Command "docker compose version >/dev/null" | Out-Null
    Invoke-Remote -Description "检查 python3 PyYAML" -Command "python3 -c 'import yaml'" | Out-Null

    if (-not (Test-Path -LiteralPath (Join-Path $PSScriptRoot "deploy/stash-updater/publish-release.sh") -PathType Leaf)) {
        throw "本地缺少 deploy/stash-updater/publish-release.sh，无法上传共享发布助手。"
    }

    $remoteCheck = @(
        "test -d '$script:RemoteRoot'",
        "test -f '$script:RemoteComposeFile'",
        "test ! -e '$script:RemoteCurrent' || test -L '$script:RemoteCurrent'",
        "test ! -e '$script:RemoteUpdaterCurrent' || test -L '$script:RemoteUpdaterCurrent'"
    )
    if (-not $InstallCaddyfile) {
        $remoteCheck += "test -f '$script:RemoteCaddyfile'"
    }
    foreach ($check in $remoteCheck) {
        Invoke-Remote -Command $check -Description "远端前置条件：$check" | Out-Null
    }

    # Existing EnvironmentFile must not silently flip the public address.
    $remoteEnvPath = "$script:RemoteUpdaterRoot/env"
    $remoteEnv = Read-RemoteEnvironmentFile -RemotePath $remoteEnvPath
    if ($remoteEnv.Count -gt 0) {
        if ($remoteEnv.ContainsKey("STASH_REMOTE_ROOT") -and $remoteEnv["STASH_REMOTE_ROOT"] -ne $script:RemoteRoot) {
            throw ("远端 EnvironmentFile 中的 STASH_REMOTE_ROOT=$($remoteEnv['STASH_REMOTE_ROOT']) " +
                "与本次 -RemoteRoot=$script:RemoteRoot 不一致；为避免误迁移，请先人工确认。")
        }
        foreach ($key in @("STASH_PUBLIC_HOST", "KELEE_PUBLIC_BASE")) {
            if ($remoteEnv.ContainsKey($key) -and -not [string]::IsNullOrWhiteSpace($remoteEnv[$key])) {
                $remoteHost = ""
                try { $remoteHost = (ConvertTo-NormalizedHost -Value $remoteEnv[$key] -Name "远端 $key").Host } catch { $remoteHost = "" }
                if ($remoteHost -ne "" -and $remoteHost -ne $Configuration.Host) {
                    if (-not $AllowBaseChange) {
                        throw ("远端 $key=$($remoteEnv[$key]) 与本次公共地址 $($Configuration.Base) 不一致。" +
                            "如确认要迁移公共域名，请加 -AllowBaseChange 并配合 -InstallCaddyfile。")
                    }
                    Write-Host "[部署] WARN: 远端正从 $($remoteEnv[$key]) 迁移到 $($Configuration.Base)（-AllowBaseChange）。" -ForegroundColor Yellow
                }
            }
        }
        if ($remoteEnv.ContainsKey("STASH_HTTP_PORT") -and $remoteEnv["STASH_HTTP_PORT"] -ne [string]$HttpPort) {
            Write-Host "[部署] WARN: 远端健康检查端口 $($remoteEnv['STASH_HTTP_PORT']) 将被更新为 $HttpPort。" -ForegroundColor Yellow
        }
    }

    # Caddy host matcher must not keep serving the old hostname.
    if (-not $SkipCaddyCheck -and -not $InstallCaddyfile) {
        $caddyResult = Invoke-RemoteRaw -Command "cat '$script:RemoteCaddyfile'" -Description "检查 Caddyfile host 匹配"
        if ($caddyResult.ExitCode -eq 0) {
            $hosts = @()
            foreach ($line in ($caddyResult.Output -split "`r?`n")) {
                if ($line -match '^\s*@[A-Za-z0-9_-]+\s+host\s+(.+)$') {
                    $hosts += ($Matches[1].Trim() -split '\s+')
                }
            }
            if ($hosts.Count -gt 0 -and ($hosts -notcontains $Configuration.Host)) {
                throw ("远端 Caddyfile 的 host 匹配器为 [$($hosts -join ', ')]，不包含本次公共地址 $($Configuration.Host)。" +
                    "请加 -InstallCaddyfile 渲染并安装匹配的 Caddyfile，或加 -SkipCaddyCheck 表示由你自行维护。")
            }
            if ($hosts.Count -eq 0 -and $caddyResult.Output -notmatch '\{\$') {
                Write-Host "[部署] WARN: 远端 Caddyfile 没有明确的 host 匹配器，无法核对公共地址 $($Configuration.Host)。" -ForegroundColor Yellow
            }
            if ($caddyResult.Output -notmatch 'immutable') {
                $cacheMessage = ("远端 Caddyfile 未包含 hashed 资产的 immutable 缓存规则；" +
                    "legacy 别名可能被错误地长期缓存。请加 -InstallCaddyfile 重新渲染 Caddyfile。")
                if ($StrictCacheHeaders) {
                    throw "$cacheMessage（已启用 -StrictCacheHeaders，拒绝在缓存策略错误的情况下发布）"
                }
                Write-Host "[部署] WARN: $cacheMessage" -ForegroundColor Yellow
            }
        }
    }
}

function Invoke-PublicCheck {
    param(
        [Parameter(Mandatory)]$Configuration,
        [Parameter(Mandatory)]$Bundle,
        [Parameter(Mandatory)][string[]]$Overrides
    )

    $url = "$($Configuration.Base)/"
    Write-Step "公网检查：GET $url"
    try {
        $response = Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 20
    } catch {
        throw "公网检查失败：$url -> $($_.Exception.Message)。远端发布可能已成功，请稍后手动检查。"
    }
    if ([int]$response.StatusCode -ne 200) { throw "公网首页返回 HTTP $($response.StatusCode)：$url" }
    $content = [string]$response.Content
    if (-not $content.Contains($Configuration.Base)) {
        throw "公网首页内容未包含部署地址 $($Configuration.Base)，可能仍由旧版本/旧 Caddy 提供。"
    }
    if (-not [string]::IsNullOrWhiteSpace($Bundle.Fingerprint) -and -not $content.Contains($Bundle.Fingerprint)) {
        Write-Host "[部署] WARN: 公网首页未找到本次构建指纹 $($Bundle.Fingerprint)（可能有 CDN 缓存或页面未展示指纹）。" -ForegroundColor Yellow
    }
    if ($Overrides.Count -gt 0) {
        $overrideUrl = "$($Configuration.Base)/$($Overrides[0])"
        $overrideResponse = Invoke-WebRequest -Uri $overrideUrl -UseBasicParsing -TimeoutSec 20
        if ([int]$overrideResponse.StatusCode -ne 200) { throw "公网覆写文件返回 HTTP $($overrideResponse.StatusCode)：$overrideUrl" }
    }
    Write-Step "公网检查通过：$($Configuration.Base)"
}

function Invoke-RollbackRemote {
    param([Parameter(Mandatory)]$Configuration)

    $helper = "$script:RemoteUpdaterRoot/current/publish-release.sh"
    Invoke-Remote -Description "确认远端共享发布助手存在" -Command "test -f '$helper'" | Out-Null
    $envPrefix = New-RemoteEnvironmentPrefix -Configuration $Configuration -ExtraPairs @()
    $command = "$envPrefix bash '$helper' --rollback-to '$RollbackTo'"
    $result = Invoke-RemoteRaw -Command $command -Description "执行共享发布事务回滚 --rollback-to $RollbackTo"
    Write-Host $result.Output
    if ($result.ExitCode -ne 0) {
        throw "远端回滚失败（退出码 $($result.ExitCode)）：请检查远端发布状态与容器日志。"
    }
    Write-Step "回滚完成：$RollbackTo"
}

# ---------------------------------------------------------------------------
# Execution plan (DryRun) — read-only, no converter / network / ssh / writes.
# ---------------------------------------------------------------------------

function Get-ExecutionPlan {
    param(
        [Parameter(Mandatory)]$Configuration,
        [Parameter(Mandatory)][string]$RepoRoot,
        [Parameter(Mandatory)][string]$SourcePath,
        [Parameter(Mandatory)][string]$BuilderPath,
        [Parameter(Mandatory)][string[]]$ResolvedBuilderArgs
    )

    $lines = New-Object System.Collections.Generic.List[string]
    $lines.Add("DryRun 执行计划（只读校验；不运行转换器、不联网、不写本地产物、不执行 ssh/scp/tar）")
    $lines.Add("模式：$(if ($BuildOnly) { 'BuildOnly（只构建，不部署）' } elseif ($RollbackTo) { "RollbackTo=$RollbackTo" } else { '完整部署' })$(if ($DryRun) { ' + DryRun' } else { '' })")
    $lines.Add("公共站点地址：$($Configuration.Base)（来源：$($Configuration.Sources)）；镜像地址：$($Configuration.MirrorBase)")
    $lines.Add("参数：PublicHost=$($Configuration.Host)；RemoteRoot=$script:RemoteRoot；UpdaterRoot=$script:RemoteUpdaterRoot；HttpPort=$HttpPort；KeepReleases=$KeepReleases；KeepAssetDays=$KeepAssetDays；StrictCacheHeaders=$(if ($StrictCacheHeaders) { '1' } else { '0' })")
    $lines.Add("SSH 目标：$SshTarget；RemoteRoot：$script:RemoteRoot；UpdaterRoot：$script:RemoteUpdaterRoot")
    $lines.Add("产物目录：$SourcePath；本地健康检查：http://127.0.0.1:$HttpPort（Host: $($Configuration.Host)）")
    $lines.Add("发布锁（手动部署与云端 timer 共用）：$script:RemoteUpdaterRoot/update.lock")

    if ($RollbackTo) {
        $lines.Add("步骤 1：ssh 预检（共享发布助手存在、docker/compose 可用）")
        $lines.Add("步骤 2：bash $script:RemoteUpdaterRoot/current/publish-release.sh --rollback-to $RollbackTo")
        $lines.Add("        同一事务逻辑：原子切换 current -> releases/$RollbackTo -> compose 重建 -> 健康检查 -> 失败自动恢复原版本")
        $lines.Add("步骤 3：记录 manual_rollback 到 $script:RemoteUpdaterRoot/state/publish.env 与 logs/publish.log")
        if (-not $SkipPublicCheck) { $lines.Add("步骤 4：公网检查 GET $($Configuration.Base)/（-SkipPublicCheck 可跳过）") }
        return $lines.ToArray()
    }

    if ($SkipBuild) {
        $lines.Add("步骤 1：跳过构建（-SkipBuild）；校验已有产物：$SourcePath")
    } else {
        $lines.Add("步骤 1：本地构建：$BuilderPath $($ResolvedBuilderArgs -join ' ')")
        $lines.Add("        注入环境变量：KELEE_PUBLIC_BASE=$($Configuration.Base)；KELEE_MIRROR_BASE=$($Configuration.MirrorBase)")
    }
    $lines.Add("步骤 2：校验产物（targets.json 覆盖、kelee-scripts.stoverride、内容寻址脚本引用、public_base 一致、无 `${...}` 占位符）")

    if ($BuildOnly) {
        $lines.Add("BuildOnly 结束：不打包、不 ssh/scp、不修改远端。")
        return $lines.ToArray()
    }

    $lines.Add("步骤 3：生成期望哈希清单（sha256sum -c 格式）并打包 tar（产物 / 更新器 / scripts）")
    $lines.Add("步骤 4：远端预检（SSH BatchMode、docker compose、flock/rsync/curl、EnvironmentFile 地址一致性、Caddyfile host、current 指针守卫）")
    $lines.Add("步骤 5：上传并校验 sha256：")
    $lines.Add("        - 发布包 -> $script:RemoteRoot/releases/.staging-<releaseId>")
    $lines.Add("        - 更新器应用 -> $script:RemoteUpdaterRoot/releases/<releaseId>/app")
    $lines.Add("        - 转换器/校验脚本 -> 更新器应用 scripts/")
    $lines.Add("步骤 6：渲染远端配置：bash <app>/render-config.sh --install-units")
    $lines.Add("        EnvironmentFile=$script:RemoteUpdaterRoot/env；systemd ExecStart=$script:RemoteUpdaterRoot/current/run-updater.sh")
    $lines.Add("步骤 7：共享发布事务：bash <app>/publish-release.sh --release-id <releaseId> --staging ... --expected-sums ... --source manual --install-app ...")
    $lines.Add("        - flock $script:RemoteUpdaterRoot/update.lock（并发部署/云端更新会明确失败或跳过）")
    $lines.Add("        - 校验 .build-manifest.json outputs + 期望哈希；copy-forward 仍在保留窗口内的内容寻址脚本")
    $lines.Add("        - staging 原子 mv 到 releases/<releaseId>；原子切换 current 符号链接")
    $lines.Add("        - docker compose -f $script:RemoteComposeFile up -d --force-recreate --no-build")
    $lines.Add("        - 健康检查 GET http://127.0.0.1:$HttpPort/ 并校验 HTTP 200 + sha256：首页/代表 .stoverride/代表哈希脚本/provider 合集")
    $lines.Add("        - 成功后原子同步更新状态到 $script:RemoteUpdaterRoot/data；失败自动恢复上一版本并复验，首次部署失败移除 current 指针")
    $lines.Add("        - 更新资产账本/发布状态/事件日志，并执行保留策略（STASH_KEEP_RELEASES=$KeepReleases，STASH_KEEP_ASSET_DAYS=$KeepAssetDays）")
    $lines.Add("步骤 8：安装并启用 systemd timer stash-kelee-update.timer（每日 03:20，带随机延迟）")
    if (-not $SkipPublicCheck) {
        $lines.Add("步骤 9：公网检查 GET $($Configuration.Base)/（-SkipPublicCheck 可跳过）")
    }
    return $lines.ToArray()
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

try {
    if ($DryRun -and $BuildOnly) {
        throw "不能同时使用 -DryRun 与 -BuildOnly：DryRun 只打印计划，BuildOnly 会真正构建；请二选一。"
    }
    if ($BuildOnly -and -not [string]::IsNullOrWhiteSpace($RollbackTo)) {
        throw "不能同时使用 -BuildOnly 与 -RollbackTo。"
    }
    if (-not [string]::IsNullOrWhiteSpace($RollbackTo)) {
        Assert-Pattern -Value $RollbackTo -Name "RollbackTo" -Pattern '^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$' -Message "只允许字母数字 . _ -（且不能以符号开头）"
    }
    if ($HttpPort -lt 1 -or $HttpPort -gt 65535) { throw "-HttpPort 必须位于 1..65535：$HttpPort" }
    if ($KeepReleases -lt 1 -or $KeepReleases -gt 100) { throw "-KeepReleases 必须位于 1..100：$KeepReleases" }
    if ($KeepAssetDays -lt 1 -or $KeepAssetDays -gt 3650) { throw "-KeepAssetDays 必须位于 1..3650：$KeepAssetDays" }
    if ($ConnectTimeoutSec -lt 1 -or $ConnectTimeoutSec -gt 120) { throw "-ConnectTimeoutSec 必须位于 1..120：$ConnectTimeoutSec" }
    Assert-Pattern -Value $SshTarget -Name "SshTarget" -Pattern '^[A-Za-z0-9][A-Za-z0-9._@:-]*$' -Message "含有不允许的字符"
    Assert-Pattern -Value $ContainerName -Name "ContainerName" -Pattern '^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$' -Message "含有不允许的字符"

    $repoRoot = (Resolve-Path -LiteralPath $PSScriptRoot).Path
    $script:RemoteRoot = Resolve-NormalizedRemoteRoot -Value $RemoteRoot
    $script:RemoteUpdaterRoot = "$script:RemoteRoot/updater"
    $script:RemoteReleases = "$script:RemoteRoot/releases"
    $script:RemoteCurrent = "$script:RemoteRoot/current"
    $script:RemoteComposeFile = "$script:RemoteRoot/docker-compose.yml"
    $script:RemoteCaddyfile = "$script:RemoteRoot/Caddyfile"
    $script:RemoteUpdaterCurrent = "$script:RemoteUpdaterRoot/current"
    $caddyRoot = $CaddySiteRoot.Trim()
    if (-not $caddyRoot.StartsWith("/") -or $caddyRoot -eq "/" -or $caddyRoot -match '(^|/)\.\.?(/|$)' -or $caddyRoot -match '//') {
        throw "-CaddySiteRoot 必须是非根绝对路径，且不能包含 . / .. / 连续斜杠：$CaddySiteRoot"
    }

    $configuration = Resolve-PublicConfiguration -PublicHostParam $PublicHost

    $sourcePath = Resolve-RepoPath -RepoRoot $repoRoot -Path $SourceDir
    $builderPath = Resolve-RepoPath -RepoRoot $repoRoot -Path $BuilderScript
    $updaterSource = Join-Path $repoRoot "deploy/stash-updater"
    $scriptsRoot = Join-Path $repoRoot "scripts"

    $requiredUpdaterFiles = @(
        "run-updater.sh",
        "publish-release.sh",
        "health-check.sh",
        "retention.sh",
        "render-config.sh",
        "validate.py",
        "lib/common.sh",
        "templates/env.in",
        "templates/stash-kelee-update.service.in",
        "templates/stash-kelee-update.timer.in",
        "templates/Caddyfile.in"
    )
    foreach ($file in $requiredUpdaterFiles) {
        $path = Join-Path $updaterSource $file
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw "缺少云端更新组件：$path" }
    }
    $scriptFiles = @(Get-ChildItem -LiteralPath $scriptsRoot -Filter "*.py" -File | Sort-Object Name)
    if ($scriptFiles.Count -eq 0) { throw "缺少转换器/校验脚本：$scriptsRoot/*.py" }
    foreach ($requiredScript in @("check_kelee_update.py", "convert_kelee_lpx.py", "validate_stash.py")) {
        if (-not (Test-Path -LiteralPath (Join-Path $scriptsRoot $requiredScript) -PathType Leaf)) {
            throw "缺少构建/校验脚本：$(Join-Path $scriptsRoot $requiredScript)"
        }
    }
    if (-not (Test-Path -LiteralPath $builderPath -PathType Leaf)) { throw "找不到构建入口：$builderPath" }

    $resolvedBuilderArgs = @()
    $resolvedBuilderArgs += $BuilderArgs
    $defaultSourcePath = [IO.Path]::GetFullPath((Join-Path $repoRoot "stash/overrides/kelee"))
    if ($sourcePath -ne $defaultSourcePath -and ($resolvedBuilderArgs -notcontains "--out-dir")) {
        $resolvedBuilderArgs += @("--out-dir", $sourcePath)
    }

    if ($DryRun) {
        Write-Host "`n===== KeLee Stash 部署执行计划 =====" -ForegroundColor Cyan
        $plan = Get-ExecutionPlan -Configuration $configuration -RepoRoot $repoRoot -SourcePath $sourcePath `
            -BuilderPath $builderPath -ResolvedBuilderArgs $resolvedBuilderArgs
        foreach ($line in $plan) { Write-PlanLine $line }
        Write-Host "`nDryRun 完成：未执行构建、未联网、未写本地产物、未执行 ssh/scp。使用 -BuildOnly 可真正构建，去掉开关即执行部署。" -ForegroundColor Green
        exit 0
    }

    if ($BuildOnly) {
        if (-not $SkipBuild) {
            Invoke-LocalBuild -BuilderPath $builderPath -BuilderArguments $resolvedBuilderArgs -Configuration $configuration
        }
        $bundle = Test-BundleArtifacts -Path $sourcePath -Configuration $configuration
        Write-Host "`nBuildOnly 完成（未部署）：" -ForegroundColor Green
        Write-Host "  产物目录：$sourcePath"
        Write-Host "  targets：$($bundle.Targets)；文件数：$($bundle.FileCount)；内容寻址脚本引用：$($bundle.HashedAssets)"
        Write-Host "  index.html SHA-256：$($bundle.IndexHash)"
        Write-Host "  构建指纹：$(if ($bundle.Fingerprint) { $bundle.Fingerprint } else { '<无构建清单>' })"
        Write-Host "  部署地址：$($configuration.Base)"
        exit 0
    }

    # ---- real deployment (or rollback) from here on ----
    Write-Host "`n===== KeLee Stash 部署 =====" -ForegroundColor Cyan
    Write-Step "公共站点地址：$($configuration.Base)（来源：$($configuration.Sources)）"

    $script:SshTool = Get-NativeTool -Names @("ssh", "ssh.exe")
    if (-not [string]::IsNullOrWhiteSpace($RollbackTo)) {
        Invoke-RemotePreflight -Configuration $configuration
        Invoke-RollbackRemote -Configuration $configuration
        if (-not $SkipPublicCheck) { Invoke-PublicCheck -Configuration $configuration -Bundle ([pscustomobject]@{ Fingerprint = "" }) -Overrides @() }
        Write-Host "`n回滚完成：$RollbackTo" -ForegroundColor Green
        exit 0
    }

    $script:ScpTool = Get-NativeTool -Names @("scp", "scp.exe")
    $script:TarTool = Get-NativeTool -Names @("tar", "tar.exe")

    if (-not $SkipBuild) {
        Invoke-LocalBuild -BuilderPath $builderPath -BuilderArguments $resolvedBuilderArgs -Configuration $configuration
    } else {
        Write-Step "跳过构建（-SkipBuild），使用已有产物：$sourcePath"
    }
    $bundle = Test-BundleArtifacts -Path $sourcePath -Configuration $configuration
    Write-Step ("产物校验通过：targets=$($bundle.Targets) files=$($bundle.FileCount) hashedRefs=$($bundle.HashedAssets) " +
        "index=$($bundle.IndexHash) fingerprint=$(if ($bundle.Fingerprint) { $bundle.Fingerprint } else { '<none>' })")

    $releaseId = (Get-Date).ToUniversalTime().ToString("yyyyMMdd-HHmmss-fff")
    $remoteStaging = "$script:RemoteReleases/.staging-$releaseId"
    $remoteSums = "$script:RemoteReleases/.staging-$releaseId.sums"
    $remoteReleaseTar = "$script:RemoteReleases/.staging-$releaseId.tar"
    $remoteUpdaterReleaseDir = "$script:RemoteUpdaterRoot/releases/$releaseId"
    $remoteUpdaterApp = "$remoteUpdaterReleaseDir/app"
    $remoteAppTar = "$remoteUpdaterReleaseDir/app.tar"
    $remoteScriptsTar = "$remoteUpdaterReleaseDir/scripts.tar"

    $localTemp = Join-Path ([IO.Path]::GetTempPath()) ("stash-deploy-$releaseId")
    New-Item -ItemType Directory -Path $localTemp -Force | Out-Null
    try {
        $localSums = Join-Path $localTemp "release.sums"
        $localReleaseTar = Join-Path $localTemp "release.tar"
        $localAppTar = Join-Path $localTemp "app.tar"
        $localScriptsTar = Join-Path $localTemp "scripts.tar"

        New-ExpectedSumsFile -Root $sourcePath -OutFile $localSums
        New-TarArchive -Root $sourcePath -Archive $localReleaseTar -Entries @(".")
        New-TarArchive -Root $updaterSource -Archive $localAppTar -Entries @(".")
        $scriptEntryNames = @($scriptFiles | ForEach-Object { $_.Name })
        New-TarArchive -Root $scriptsRoot -Archive $localScriptsTar -Entries $scriptEntryNames

        $releaseTarHash = Get-FileSha256 -Path $localReleaseTar
        $appTarHash = Get-FileSha256 -Path $localAppTar
        $scriptsTarHash = Get-FileSha256 -Path $localScriptsTar
        $sumsHash = Get-FileSha256 -Path $localSums

        Invoke-RemotePreflight -Configuration $configuration

        Invoke-Remote -Description "创建远端目录" -Command (
            "mkdir -p '$remoteStaging' '$remoteUpdaterApp' '$script:RemoteUpdaterRoot/state' " +
            "'$script:RemoteUpdaterRoot/logs' '$script:RemoteReleases'") | Out-Null

        Copy-ToRemote -LocalPath $localReleaseTar -RemotePath $remoteReleaseTar -Description "发布包（$($bundle.FileCount) 个文件）"
        Assert-RemoteFileHash -RemotePath $remoteReleaseTar -ExpectedHash $releaseTarHash -Description "发布包"

        Copy-ToRemote -LocalPath $localAppTar -RemotePath $remoteAppTar -Description "云端更新器应用"
        Assert-RemoteFileHash -RemotePath $remoteAppTar -ExpectedHash $appTarHash -Description "更新器应用"

        Copy-ToRemote -LocalPath $localScriptsTar -RemotePath $remoteScriptsTar -Description "转换器/校验脚本"
        Assert-RemoteFileHash -RemotePath $remoteScriptsTar -ExpectedHash $scriptsTarHash -Description "转换器脚本"

        Copy-ToRemote -LocalPath $localSums -RemotePath $remoteSums -Description "期望哈希清单"
        Assert-RemoteFileHash -RemotePath $remoteSums -ExpectedHash $sumsHash -Description "期望哈希清单"

        Invoke-Remote -Description "解包发布包到暂存目录" -Command (
            "tar -C '$remoteStaging' -xf '$remoteReleaseTar' && rm -f '$remoteReleaseTar'") | Out-Null
        Invoke-Remote -Description "解包更新器应用" -Command (
            "tar -C '$remoteUpdaterApp' -xf '$remoteAppTar' && rm -f '$remoteAppTar'") | Out-Null
        Invoke-Remote -Description "解包转换器脚本" -Command (
            "mkdir -p '$remoteUpdaterApp/scripts' && tar -C '$remoteUpdaterApp/scripts' -xf '$remoteScriptsTar' && " +
            "rm -f '$remoteScriptsTar'") | Out-Null

        # Windows checkouts may have CRLF; normalise the uploaded runtime files
        # (never the release payload, whose bytes are pinned by the hash list).
        Invoke-Remote -Description "规范化更新器脚本换行并设置权限" -Command (
            "find '$remoteUpdaterApp' \\( -name '*.sh' -o -name '*.in' -o -name '*.py' \\) -print0 | " +
            "xargs -0 -r sed -i 's/\r`$//' && " +
            "chmod 0755 '$remoteUpdaterApp/run-updater.sh' '$remoteUpdaterApp/publish-release.sh' " +
            "'$remoteUpdaterApp/health-check.sh' '$remoteUpdaterApp/retention.sh' '$remoteUpdaterApp/render-config.sh' && " +
            "chmod 0644 '$remoteUpdaterApp/validate.py' '$remoteUpdaterApp/lib/common.sh' '$remoteUpdaterApp/templates/'*.in") | Out-Null

        $renderArgs = @(
            "--remote-root", $script:RemoteRoot,
            "--updater-root", $script:RemoteUpdaterRoot,
            "--public-base", $configuration.Base,
            "--public-host", $configuration.Host,
            "--mirror-base", $configuration.MirrorBase,
            "--http-port", [string]$HttpPort,
            "--container-name", $ContainerName,
            "--keep-releases", [string]$KeepReleases,
            "--keep-asset-days", [string]$KeepAssetDays,
            "--caddy-site-root", $caddyRoot,
            "--install-units"
        )
        if ($InstallCaddyfile) { $renderArgs += "--install-caddyfile" }
        $renderCommand = "bash '$remoteUpdaterApp/render-config.sh'"
        foreach ($arg in $renderArgs) {
            if ($arg.StartsWith("--")) { $renderCommand += " $arg" } else { $renderCommand += " '$arg'" }
        }
        $renderResult = Invoke-RemoteRaw -Command $renderCommand -Description "渲染云端 EnvironmentFile / systemd 单元$(if ($InstallCaddyfile) { ' / Caddyfile' })"
        Write-Host $renderResult.Output
        if ($renderResult.ExitCode -ne 0) {
            throw "远端配置渲染失败（退出码 $($renderResult.ExitCode)）：站点未切换。"
        }

        $publishExtraPairs = @()
        if ($InstallCaddyfile -or $StrictCacheHeaders) {
            $publishExtraPairs += "STASH_STRICT_CACHE_HEADERS='1'"
        }
        $publishPrefix = New-RemoteEnvironmentPrefix -Configuration $configuration -ExtraPairs $publishExtraPairs
        $publishCommand = "$publishPrefix bash '$remoteUpdaterApp/publish-release.sh' " +
            "--release-id '$releaseId' --staging '$remoteStaging' --expected-sums '$remoteSums' " +
            "--source manual --install-app '$remoteUpdaterApp'"
        $publishResult = Invoke-RemoteRaw -Command $publishCommand -Description "执行共享发布事务（锁/切换/健康检查/自动回滚）"
        Write-Host $publishResult.Output
        if ($publishResult.ExitCode -ne 0) {
            $hint = switch ($publishResult.ExitCode) {
                75 { "另一个发布/更新任务正在运行（共享锁被占用）；请稍后重试。" }
                40 { "新版本健康检查失败，远端已按事务回滚到上一版本。" }
                30 { "容器重建失败，远端已按事务回滚到上一版本。" }
                50 { "更新状态同步失败，远端已按事务回滚到上一版本。" }
                60 { "自动回滚也失败，需要人工检查远端 current 指针与容器。" }
                default { "远端发布事务失败。" }
            }
            throw "远端发布事务失败（退出码 $($publishResult.ExitCode)）：$hint"
        }

        Invoke-Remote -Description "启用云端更新 timer" -Command (
            "systemctl daemon-reload && systemctl enable --now stash-kelee-update.timer && " +
            "systemctl is-enabled --quiet stash-kelee-update.timer && " +
            "systemctl is-active --quiet stash-kelee-update.timer") | Out-Null
        Invoke-Remote -Description "清理远端临时哈希清单" -Command "rm -f '$remoteSums'" | Out-Null

        if (-not $SkipPublicCheck) {
            Invoke-PublicCheck -Configuration $configuration -Bundle $bundle -Overrides $bundle.Overrides
        }

        Write-Host "`n部署完成：$releaseId" -ForegroundColor Green
        Write-Host "  公共地址：$($configuration.Base)"
        Write-Host "  当前版本：$remoteStaging -> $script:RemoteCurrent（由远端事务切换）"
        Write-Host "  发布状态：$script:RemoteUpdaterRoot/state/publish.env；事件日志：$script:RemoteUpdaterRoot/logs/publish.log"
        Write-Host "  云端更新：systemd timer stash-kelee-update.timer（每日 03:20，带随机延迟；共用发布锁）"
        Write-Host "  手工回滚：ssh $SshTarget 后执行 bash $script:RemoteUpdaterRoot/current/publish-release.sh --rollback-to <上一个版本>"
        exit 0
    } finally {
        if (Test-Path -LiteralPath $localTemp) {
            Remove-Item -LiteralPath $localTemp -Recurse -Force -ErrorAction SilentlyContinue
        }
    }
} catch {
    Write-Error $_.Exception.Message
    exit 1
}
