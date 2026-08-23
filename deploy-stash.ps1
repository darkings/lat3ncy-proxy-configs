#Requires -Version 5.1

<#[
零凭据部署脚本

本脚本只负责部署：打包本地已有产物、上传、切换版本、安装/更新云端定时任务，
以及重建并验证 Stash 静态站点。它不会检查 KeLee 上游，也不会执行 Loon→Stash
转换；检查和转换由云端的 systemd timer 定时完成。

本脚本不保存密码、私钥、Token 或其他认证材料。SSH 认证完全交给本机的
~/.ssh/config、ssh-agent 或系统 SSH 凭据管理器；默认只使用已配置的 Host
别名 `jie`，并强制使用 BatchMode，认证不可用时直接失败而不会在脚本中询问或
记录凭据。

默认前提：云端已经初始化 /opt/stash/docker-compose.yml 和 /opt/stash/Caddyfile，
并且云端有 Python 3、PyYAML、rsync、flock 和 systemd。首次部署会安装每日定时
更新服务；服务运行在云端，不依赖本机在线。

常用用法：
  .\deploy-stash.ps1
  .\deploy-stash.ps1 -SkipPublicCheck
  .\deploy-stash.ps1 -DryRun
  .\deploy-stash.ps1 -SshTarget jie -RemoteRoot /opt/stash
#>

[CmdletBinding()]
param(
    [string]$SshTarget = "jie",
    [string]$RemoteRoot = "/opt/stash",
    [string]$PublicHost = "stash.ponyo.fun",
    [string]$SourceDir = "stash\overrides\kelee",
    [switch]$SkipPublicCheck,
    [switch]$DryRun
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Get-NativeTool {
    param([Parameter(Mandatory)][string[]]$Names)

    foreach ($name in $Names) {
        $command = Get-Command -Name $name -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($null -ne $command) {
            if ($command.PSObject.Properties.Name -contains "Path" -and $command.Path) {
                return $command.Path
            }
            if ($command.PSObject.Properties.Name -contains "Source" -and $command.Source) {
                return $command.Source
            }
        }
    }
    throw "找不到所需命令：$($Names -join ', ')。请先安装 OpenSSH 和 tar。"
}

function Assert-SafeValue {
    param(
        [Parameter(Mandatory)][string]$Value,
        [Parameter(Mandatory)][string]$Name,
        [Parameter(Mandatory)][string]$Pattern
    )

    if ($Value -notmatch $Pattern) {
        throw "$Name 含有不允许的字符：$Value"
    }
}

function Invoke-Remote {
    param([Parameter(Mandatory)][string]$Command)

    if ($DryRun) {
        Write-Host "[干跑] ssh $SshTarget $Command" -ForegroundColor DarkGray
        return ""
    }

    Write-Host "[远端] $Command" -ForegroundColor DarkCyan
    $output = @(& $script:SshTool -o BatchMode=yes -o ConnectTimeout=15 $SshTarget $Command 2>&1)
    $exitCode = $LASTEXITCODE
    foreach ($line in $output) {
        Write-Host ([string]$line)
    }
    if ($exitCode -ne 0) {
        throw "远端命令失败（退出码 $exitCode）：$Command"
    }
    return (($output | ForEach-Object { [string]$_ }) -join "`n")
}

function Copy-ToRemote {
    param(
        [Parameter(Mandatory)][string]$LocalPath,
        [Parameter(Mandatory)][string]$RemotePath
    )

    $destination = "{0}:{1}" -f $SshTarget, $RemotePath
    if ($DryRun) {
        Write-Host "[干跑] scp $LocalPath $destination" -ForegroundColor DarkGray
        return
    }

    Write-Host "[上传] $LocalPath -> $destination" -ForegroundColor DarkCyan
    $output = @(& $script:ScpTool -o BatchMode=yes -o ConnectTimeout=15 $LocalPath $destination 2>&1)
    $exitCode = $LASTEXITCODE
    foreach ($line in $output) {
        Write-Host ([string]$line)
    }
    if ($exitCode -ne 0) {
        throw "scp 上传失败（退出码 $exitCode）：$destination"
    }
}

function New-TarArchive {
    param(
        [Parameter(Mandatory)][string]$Root,
        [Parameter(Mandatory)][string]$Archive,
        [Parameter(Mandatory)][string[]]$Entries
    )

    if ($DryRun) {
        Write-Host "[干跑] tar -C $Root -cf $Archive $($Entries -join ' ')" -ForegroundColor DarkGray
        return
    }

    & $script:TarTool -C $Root -cf $Archive @Entries
    if ($LASTEXITCODE -ne 0) {
        throw "tar 打包失败（退出码 $LASTEXITCODE）：$Root"
    }
}

try {
    $repoRoot = (Resolve-Path -LiteralPath $PSScriptRoot).Path
    $remoteRoot = $RemoteRoot.TrimEnd("/")
    if ([string]::IsNullOrWhiteSpace($remoteRoot) -or $remoteRoot -eq "/") {
        throw "RemoteRoot 不能是根目录。"
    }

    Assert-SafeValue -Value $SshTarget -Name "SshTarget" -Pattern '^[A-Za-z0-9][A-Za-z0-9._@:-]*$'
    Assert-SafeValue -Value $remoteRoot -Name "RemoteRoot" -Pattern '^/[A-Za-z0-9._/-]+$'
    Assert-SafeValue -Value $PublicHost -Name "PublicHost" -Pattern '^[A-Za-z0-9][A-Za-z0-9.-]*$'

    $sourceCandidate = if ([IO.Path]::IsPathRooted($SourceDir)) {
        $SourceDir
    } else {
        Join-Path $repoRoot $SourceDir
    }
    if (-not (Test-Path -LiteralPath $sourceCandidate -PathType Container)) {
        throw "找不到资源目录：$sourceCandidate"
    }
    $sourcePath = (Resolve-Path -LiteralPath $sourceCandidate).Path
    $updaterSource = Join-Path $repoRoot "deploy\stash-updater"
    $requiredUpdaterFiles = @(
        "run-updater.sh",
        "validate.py",
        "stash-kelee-update.service",
        "stash-kelee-update.timer"
    )
    foreach ($file in $requiredUpdaterFiles) {
        $path = Join-Path $updaterSource $file
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
            throw "缺少云端更新组件：$path"
        }
    }
    $scriptsRoot = Join-Path $repoRoot "scripts"
    $requiredScripts = @(
        "convert_kelee_lpx.py",
        "check_kelee_update.py",
        "generate_kelee_html.py"
    )
    foreach ($file in $requiredScripts) {
        if (-not (Test-Path -LiteralPath (Join-Path $scriptsRoot $file) -PathType Leaf)) {
            throw "缺少云端更新脚本：$(Join-Path $scriptsRoot $file)"
        }
    }

    $script:SshTool = Get-NativeTool -Names @("ssh.exe", "ssh")
    $script:ScpTool = Get-NativeTool -Names @("scp.exe", "scp")
    $script:TarTool = Get-NativeTool -Names @("tar.exe", "tar")

    $releaseId = (Get-Date).ToUniversalTime().ToString("yyyyMMdd-HHmmss-fff")
    $remoteRelease = "$remoteRoot/releases/$releaseId"
    $remoteCurrent = "$remoteRoot/current"
    $remoteUpdaterRoot = "$remoteRoot/updater"
    $remoteUpdaterRelease = "$remoteUpdaterRoot/releases/$releaseId"
    $remoteUpdaterApp = "$remoteUpdaterRelease/app"
    $remoteUpdaterData = "$remoteUpdaterRoot/data/stash/overrides/kelee"
    $remoteUpdaterCurrent = "$remoteUpdaterRoot/current"
    $archiveBase = Join-Path ([IO.Path]::GetTempPath()) "stash-$releaseId"
    $liveArchive = "$archiveBase-live.tar"
    $updaterAppArchive = "$archiveBase-updater-app.tar"
    $updaterScriptsArchive = "$archiveBase-updater-scripts.tar"
    $archivePaths = @($liveArchive, $updaterAppArchive, $updaterScriptsArchive)

    $fileCount = @(Get-ChildItem -LiteralPath $sourcePath -File -Recurse -Force).Count
    if ($fileCount -eq 0) {
        throw "资源目录为空：$sourcePath"
    }
    $indexPath = Join-Path $sourcePath "index.html"
    if (-not (Test-Path -LiteralPath $indexPath -PathType Leaf)) {
        throw "资源目录缺少 index.html：$indexPath"
    }

    $localHash = (Get-FileHash -LiteralPath $indexPath -Algorithm SHA256).Hash.ToLowerInvariant()
    Write-Host "资源：$fileCount 个文件，index.html SHA-256：$localHash" -ForegroundColor DarkGray
    Write-Host "发布版本：$releaseId" -ForegroundColor Cyan

    # BatchMode 确保认证由外部 SSH 配置提供，脚本不会提示或保存凭据。
    Invoke-Remote -Command "true" | Out-Null
    Invoke-Remote -Command "test -f '$remoteRoot/docker-compose.yml'"
    Invoke-Remote -Command "test -f '$remoteRoot/Caddyfile'"
    Invoke-Remote -Command "command -v python3 >/dev/null && command -v rsync >/dev/null && command -v flock >/dev/null && command -v systemctl >/dev/null"
    Invoke-Remote -Command "python3 -c 'import yaml'"
    Invoke-Remote -Command "mkdir -p '$remoteRelease' '$remoteUpdaterApp' '$remoteUpdaterData'"

    try {
        New-TarArchive -Root $sourcePath -Archive $liveArchive -Entries @(".")
        Copy-ToRemote -LocalPath $liveArchive -RemotePath "$remoteRelease/.payload.tar"
        Invoke-Remote -Command "tar -C '$remoteRelease' -xf '$remoteRelease/.payload.tar'"
        Invoke-Remote -Command "rm -f '$remoteRelease/.payload.tar'"

        New-TarArchive -Root $updaterSource -Archive $updaterAppArchive -Entries @(".")
        Copy-ToRemote -LocalPath $updaterAppArchive -RemotePath "$remoteUpdaterApp/.app.tar"
        Invoke-Remote -Command "tar -C '$remoteUpdaterApp' -xf '$remoteUpdaterApp/.app.tar'"
        Invoke-Remote -Command "rm -f '$remoteUpdaterApp/.app.tar'"

        New-TarArchive -Root $scriptsRoot -Archive $updaterScriptsArchive -Entries $requiredScripts
        Copy-ToRemote -LocalPath $updaterScriptsArchive -RemotePath "$remoteUpdaterApp/scripts.tar"
        Invoke-Remote -Command "mkdir -p '$remoteUpdaterApp/scripts'; tar -C '$remoteUpdaterApp/scripts' -xf '$remoteUpdaterApp/scripts.tar'; rm -f '$remoteUpdaterApp/scripts.tar'"
        Invoke-Remote -Command "chmod 0755 '$remoteUpdaterApp/run-updater.sh' '$remoteUpdaterApp/validate.py'"
    } finally {
        foreach ($archivePath in $archivePaths) {
            if (Test-Path -LiteralPath $archivePath) {
                Remove-Item -LiteralPath $archivePath -Force -ErrorAction SilentlyContinue
            }
        }
    }

    # 写入的只是公开站点地址和目录，不包含任何认证材料。
    $envCommand = "printf '%s\n' 'STASH_REMOTE_ROOT=$remoteRoot' 'STASH_UPDATER_ROOT=$remoteUpdaterRoot' 'STASH_PUBLIC_HOST=$PublicHost' > '$remoteUpdaterRoot/env'"
    Invoke-Remote -Command $envCommand

    # 仅在首次部署时用当前发布内容初始化云端更新状态；后续由云端自己维护。
    $seedCommand = "if test ! -f '$remoteUpdaterData/targets.json'; then rsync -a --delete '$remoteRelease/' '$remoteUpdaterData/'; fi"
    Invoke-Remote -Command $seedCommand
    Invoke-Remote -Command "test ! -e '$remoteUpdaterCurrent' || test -L '$remoteUpdaterCurrent'"
    Invoke-Remote -Command "ln -sfn '$remoteUpdaterApp' '$remoteUpdaterCurrent'"
    Invoke-Remote -Command "install -m 0644 '$remoteUpdaterApp/stash-kelee-update.service' /etc/systemd/system/stash-kelee-update.service; install -m 0644 '$remoteUpdaterApp/stash-kelee-update.timer' /etc/systemd/system/stash-kelee-update.timer"

    # current 必须是符号链接或不存在，避免覆盖云端意外出现的普通目录。
    Invoke-Remote -Command "test ! -e '$remoteCurrent' || test -L '$remoteCurrent'"
    Invoke-Remote -Command "ln -sfn '$remoteRelease' '$remoteCurrent'"
    Invoke-Remote -Command "docker compose -f '$remoteRoot/docker-compose.yml' up -d --force-recreate --no-build"

    Invoke-Remote -Command "test -f '$remoteCurrent/index.html'"
    $currentTarget = (Invoke-Remote -Command "readlink -f '$remoteCurrent'").Trim()
    # current 是符号链接，find 默认不会遍历链接目标，因此必须显式跟随链接。
    $remoteFileCountText = Invoke-Remote -Command "find -L '$remoteCurrent' -type f | wc -l"
    $remoteHashLine = Invoke-Remote -Command "sha256sum '$remoteCurrent/index.html'"
    $remoteHash = ""
    foreach ($line in ($remoteHashLine -split "`r?`n")) {
        if ($line -match '^([0-9a-fA-F]{64})\s') {
            $remoteHash = $Matches[1].ToLowerInvariant()
            break
        }
    }
    $containerState = (Invoke-Remote -Command "docker inspect --format '{{.State.Status}}' stash").Trim()

    if (-not $DryRun) {
        if ($currentTarget -ne $remoteRelease) {
            throw "远端 current 未指向本次版本：$currentTarget"
        }
        if ($remoteHash -ne $localHash) {
            throw "index.html 哈希不一致：本地 $localHash，远端 $remoteHash"
        }
        $remoteFileCount = 0
        if (-not [int]::TryParse($remoteFileCountText.Trim(), [ref]$remoteFileCount) -or $remoteFileCount -ne $fileCount) {
            throw "远端文件数不一致：本地 $fileCount，远端 $remoteFileCount"
        }
        if ($containerState -ne "running") {
            throw "stash 容器状态不是 running：$containerState"
        }
    }

    # 站点验证完成后才启用云端定时器，避免首次安装过程中并发更新。
    Invoke-Remote -Command "systemctl daemon-reload; systemctl enable --now stash-kelee-update.timer; systemctl is-enabled --quiet stash-kelee-update.timer; systemctl is-active --quiet stash-kelee-update.timer"

    if (-not $SkipPublicCheck) {
        if ($DryRun) {
            Write-Host "[干跑] GET https://$PublicHost/" -ForegroundColor DarkGray
        } else {
            Write-Host "[公网] 检查 https://$PublicHost/" -ForegroundColor DarkCyan
            $response = Invoke-WebRequest -Uri "https://$PublicHost/" -UseBasicParsing -TimeoutSec 20
            if ([int]$response.StatusCode -ne 200) {
                throw "公网首页返回 HTTP $($response.StatusCode)"
            }
        }
    }

    Write-Host "`n部署完成：$releaseId" -ForegroundColor Green
    Write-Host "远端：ssh $SshTarget $remoteRoot" -ForegroundColor Gray
    Write-Host "当前版本：$remoteRelease" -ForegroundColor Gray
    Write-Host "云端更新：systemd timer stash-kelee-update.timer（每日 03:20，带随机延迟）" -ForegroundColor Gray
    Write-Host "回滚时可将 current 重新指向 releases 下的目标版本，再执行 compose up -d --force-recreate。" -ForegroundColor Gray
} catch {
    Write-Error $_.Exception.Message
    exit 1
}
