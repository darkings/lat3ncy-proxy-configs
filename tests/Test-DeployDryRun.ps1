#Requires -Version 5.1

<#
.SYNOPSIS
Offline contract tests for deploy-stash.ps1 -DryRun / -BuildOnly.

.DESCRIPTION
The test prepends a fake bin directory to PATH containing wrappers for
ssh/scp/tar/python/sha256sum/shasum that create marker files. A fake builder
(the python wrapper executes fake-builder.py with the real interpreter) writes a
small valid bundle into $env:FAKE_BUILD_OUT.

Assertions:
  * -DryRun: exit 0, plan text printed, ZERO fake tool markers (nothing was
    executed), the artifact dir was not created, the repo artifact dir was not
    mutated.
  * bad argument combinations fail early (nonzero) with explicit messages.
  * -BuildOnly: fake python marker appears, ssh/scp/tar markers do NOT, bundle
    is created and validated, exit 0. A corrupt manifest / failing builder is
    rejected (nonzero).
  * cross-platform: works on Windows pwsh and Linux pwsh (CI).

Run: pwsh -File tests/Test-DeployDryRun.ps1
#>

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$script:RepoRoot = Split-Path -Parent $PSScriptRoot
$script:DeployScript = Join-Path $script:RepoRoot "deploy-stash.ps1"
if (-not (Test-Path -LiteralPath $script:DeployScript -PathType Leaf)) {
    throw "missing deploy-stash.ps1: $script:DeployScript"
}

$script:Failures = 0
$script:Checks = 0
$script:Passed = 0
$script:IsWindowsHost = [IO.Path]::DirectorySeparatorChar -eq '\'
$script:TestRoot = Join-Path ([IO.Path]::GetTempPath()) ("deploy-dryrun-test-" + [guid]::NewGuid().ToString("N"))
$script:FakeBin = Join-Path $script:TestRoot "fake-bin"
$script:Markers = Join-Path $script:TestRoot "markers"
$script:Artifacts = Join-Path $script:TestRoot "artifacts"
$script:ArtifactSnapshotRoot = Join-Path $script:RepoRoot "stash/overrides/kelee"
$script:SavedPath = $env:PATH
$script:FailExit = 1

function Write-LfFile {
    param([string]$Path, [string]$Content)
    $utf8 = New-Object System.Text.UTF8Encoding($false)
    [IO.File]::WriteAllText($Path, ($Content -replace "`r`n", "`n"), $utf8)
}

function Write-TextFile {
    param([string]$Path, [string]$Content)
    $encoding = New-Object System.Text.UTF8Encoding($false)
    [IO.File]::WriteAllText($Path, $Content, $encoding)
}

function Add-Pass {
    param([string]$Description)
    $script:Checks++
    $script:Passed++
    Write-Host "PASS: $Description"
}

function Add-Fail {
    param([string]$Description, [string]$Detail = "")
    $script:Checks++
    $script:Failures++
    Write-Host "FAIL: $Description $Detail" -ForegroundColor Red
}

function Assert-True {
    param([string]$Description, [bool]$Condition, [string]$Detail = "")
    if ($Condition) { Add-Pass $Description } else { Add-Fail $Description $Detail }
}

function Assert-ExitCode {
    param([string]$Description, $Result, [int]$Expected)
    Assert-True -Description $Description -Condition ($Result.ExitCode -eq $Expected) `
        -Detail "(exit=$($Result.ExitCode), expected=$Expected)`n$($Result.Output)"
}

function Assert-ExitNonZero {
    param([string]$Description, $Result)
    Assert-True -Description $Description -Condition ($Result.ExitCode -ne 0) `
        -Detail "(unexpected exit=0)`n$($Result.Output)"
}

function Assert-OutputContains {
    param([string]$Description, $Result, [string]$Needle)
    # ASCII-only needles: native child output may be decoded with a legacy
    # console code page on Windows, so Chinese messages are not asserted.
    Assert-True -Description $Description -Condition ($Result.Output.Contains($Needle)) `
        -Detail "(output missing '$Needle')`n$($Result.Output)"
}

function Assert-OutputNotContains {
    param([string]$Description, $Result, [string]$Needle)
    Assert-True -Description $Description -Condition (-not $Result.Output.Contains($Needle)) `
        -Detail "(output unexpectedly contains '$Needle')"
}

function Get-MarkerFiles {
    if (-not (Test-Path -LiteralPath $script:Markers)) { return @() }
    return @(Get-ChildItem -LiteralPath $script:Markers -File -Recurse -Force -ErrorAction SilentlyContinue)
}

function Clear-Markers {
    if (Test-Path -LiteralPath $script:Markers) {
        Get-ChildItem -LiteralPath $script:Markers -File -Recurse -Force -ErrorAction SilentlyContinue |
            Remove-Item -Force -ErrorAction SilentlyContinue
    }
}

function Get-TreeFingerprint {
    param([string]$Root)
    if (-not (Test-Path -LiteralPath $Root -PathType Container)) { return "<missing>" }
    $files = @(Get-ChildItem -LiteralPath $Root -Recurse -Force -File -ErrorAction SilentlyContinue | Sort-Object FullName)
    $lines = foreach ($file in $files) {
        $relative = $file.FullName.Substring($Root.Length)
        "$relative|$($file.Length)|$((Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash)"
    }
    return ($lines -join "`n")
}

function Find-RealPython {
    foreach ($name in @("python", "python3", "py")) {
        $command = Get-Command -Name $name -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($null -eq $command) { continue }
        $candidate = $null
        if ($command.PSObject.Properties.Name -contains "Source" -and $command.Source) { $candidate = $command.Source }
        if (-not $candidate) { continue }
        try {
            $probe = & $candidate -c "import sys; print(sys.executable)" 2>&1
            if ($LASTEXITCODE -eq 0) {
                $path = ([string]($probe | Select-Object -First 1)).Trim()
                if ($path -and (Test-Path -LiteralPath $path -PathType Leaf)) { return $path }
            }
        } catch {
            continue
        }
    }
    return $null
}

function New-FakeTool {
    param([string]$Name, [string]$MarkerPath)
    if ($script:IsWindowsHost) {
        $path = Join-Path $script:FakeBin "$Name.cmd"
        $content = "@echo off`r`necho invoked>`"$MarkerPath`"`r`nexit /b 1`r`n"
        [IO.File]::WriteAllText($path, $content, [System.Text.Encoding]::ASCII)
    } else {
        $path = Join-Path $script:FakeBin $Name
        Write-LfFile -Path $path -Content "#!/usr/bin/env bash`nmarker='$MarkerPath'`nprintf 'invoked\n' >`"`$marker`"`nexit 1`n"
        & chmod +x $path
    }
}

function New-FakePythonWrapper {
    param([string]$RealPython)
    $fakeBuilder = Join-Path $script:FakeBin "fake-builder.py"
    $builderText = @'
"""Fake build orchestrator used by tests/Test-DeployDryRun.ps1."""
import hashlib
import json
import os
import pathlib
import sys

marker_dir = os.environ.get("FAKE_MARKER_DIR", "")
if marker_dir:
    pathlib.Path(marker_dir).mkdir(parents=True, exist_ok=True)
    (pathlib.Path(marker_dir) / "python.marker").write_text("invoked", encoding="utf-8")

mode = os.environ.get("FAKE_BUILD_MODE", "ok")
if mode == "fail":
    print("fake builder: requested failure", file=sys.stderr)
    sys.exit(1)

out = os.environ.get("FAKE_BUILD_OUT", "")
base = os.environ.get("KELEE_PUBLIC_BASE", "")
if not out or not base:
    print("fake builder: FAKE_BUILD_OUT/KELEE_PUBLIC_BASE missing", file=sys.stderr)
    sys.exit(3)

root = pathlib.Path(out)
(root / "scripts").mkdir(parents=True, exist_ok=True)
outputs = {}


def write(rel, text):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    outputs[rel] = {
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "size": path.stat().st_size,
        "counts": {},
    }


write("index.html", f'<!DOCTYPE html><html><body>KeLee fake build <a href="{base}/A_Test.stoverride">A</a></body></html>\n')
write("targets.json", json.dumps([{"filename": "A_Test.lpx", "name": "A_Test"}], ensure_ascii=False) + "\n")
write("A_Test.stoverride", f"name: A_Test\ndesc: fake build artifact\nurl: {base}/scripts/0123456789ab-test.js\n")
write(
    "kelee-scripts.stoverride",
    "name: kelee-scripts\ndesc: fake build artifact\nscript-providers:\n"
    f"  test:\n    url: {base}/scripts/0123456789ab-test.js\n    interval: 86400\n",
)
write("scripts/0123456789ab-test.js", 'console.log("fake hashed asset");\n')
write("scripts/atest.js", 'console.log("fake legacy alias");\n')

if mode == "corrupt-manifest":
    outputs["index.html"]["sha256"] = "0" * 64
elif mode == "no-manifest":
    sys.exit(0)

manifest = {
    "schema": 2,
    "public_base": base,
    "mirror_base": base,
    "fingerprint": "fake-fingerprint",
    "outputs": outputs,
    "resources": {},
    "errors": [],
    "warnings": [],
}
(root / ".build-manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
sys.exit(0)
'@
    Write-TextFile -Path $fakeBuilder -Content $builderText

    foreach ($name in @("python", "python3")) {
        if ($script:IsWindowsHost) {
            $path = Join-Path $script:FakeBin "$name.cmd"
            $content = "@echo off`r`n`"$RealPython`" `"$fakeBuilder`" %*`r`nexit /b %ERRORLEVEL%`r`n"
            [IO.File]::WriteAllText($path, $content, [System.Text.Encoding]::ASCII)
        } else {
            $path = Join-Path $script:FakeBin $name
            Write-LfFile -Path $path -Content "#!/usr/bin/env bash`nexec `"$RealPython`" `"$fakeBuilder`" `"`$@`"`n"
            & chmod +x $path
        }
    }
}

function New-FakeToolchain {
    New-Item -ItemType Directory -Path $script:FakeBin -Force | Out-Null
    New-Item -ItemType Directory -Path $script:Markers -Force | Out-Null
    foreach ($tool in @("ssh", "scp", "tar", "sha256sum", "shasum")) {
        New-FakeTool -Name $tool -MarkerPath (Join-Path $script:Markers "$tool.marker")
    }
    $realPython = Find-RealPython
    if (-not $realPython) { throw "找不到可用的 Python 解释器，无法运行 -BuildOnly 测试。" }
    New-FakePythonWrapper -RealPython $realPython
}

function Invoke-DeployScript {
    param(
        [string[]]$Arguments,
        [hashtable]$Environment = @{}
    )

    $managedKeys = @("KELEE_PUBLIC_BASE", "KELEE_MIRROR_BASE", "STASH_PUBLIC_HOST", "STASH_PUBLIC_SCHEME",
        "FAKE_BUILD_OUT", "FAKE_BUILD_MODE", "FAKE_MARKER_DIR")
    $saved = @{}
    foreach ($key in $managedKeys) {
        $saved[$key] = [Environment]::GetEnvironmentVariable($key)
        [Environment]::SetEnvironmentVariable($key, $null)
    }
    foreach ($key in $Environment.Keys) {
        [Environment]::SetEnvironmentVariable($key, [string]$Environment[$key])
    }
    try {
        $output = (& $script:PsExe -NoProfile -File $script:DeployScript @Arguments 2>&1 | Out-String)
        $exitCode = $LASTEXITCODE
    } finally {
        foreach ($key in $managedKeys) {
            [Environment]::SetEnvironmentVariable($key, $saved[$key])
        }
    }
    return [pscustomobject]@{ ExitCode = $exitCode; Output = $output }
}

try {
    $script:PsExe = (Get-Process -Id $PID).Path
    if (-not $script:PsExe) { throw "无法定位当前 PowerShell 可执行文件" }
    New-Item -ItemType Directory -Path $script:TestRoot -Force | Out-Null
    New-FakeToolchain

    $artifactSnapshot = Get-TreeFingerprint -Root $script:ArtifactSnapshotRoot

    # Keep the fake tools first on PATH for every child invocation.
    $env:PATH = "$($script:FakeBin)$([IO.Path]::PathSeparator)$($script:SavedPath)"

    Write-Host "== 场景 1：-DryRun 只打印计划，不执行任何外部工具 =="
    Clear-Markers
    if (Test-Path -LiteralPath $script:Artifacts) { Remove-Item -LiteralPath $script:Artifacts -Recurse -Force }
    $dryRun = Invoke-DeployScript -Arguments @(
        "-DryRun", "-PublicHost", "example.test", "-SourceDir", $script:Artifacts, "-SkipPublicCheck"
    )
    Assert-ExitCode "DryRun 退出码为 0" $dryRun 0
    Assert-OutputContains "DryRun 输出包含 DryRun 标识" $dryRun "DryRun"
    Assert-OutputContains "DryRun 输出包含共享发布事务入口" $dryRun "publish-release.sh"
    Assert-OutputContains "DryRun 输出包含共享发布锁" $dryRun "update.lock"
    Assert-OutputContains "DryRun 输出包含统一地址注入" $dryRun "KELEE_PUBLIC_BASE=https://example.test"
    Assert-OutputContains "DryRun 输出包含健康检查步骤" $dryRun "sha256"
    Assert-OutputContains "DryRun 输出包含 RemoteRoot 计划" $dryRun "RemoteRoot"
    Assert-OutputContains "DryRun 输出包含解析后的公共域名" $dryRun "PublicHost=example.test"
    Assert-OutputContains "DryRun 输出包含保留策略参数" $dryRun "KeepReleases=3"
    Assert-OutputContains "DryRun 输出包含保留窗口参数" $dryRun "KeepAssetDays=30"
    Assert-OutputContains "DryRun 输出包含真实构建入口" $dryRun "check_kelee_update.py"
    Assert-OutputContains "DryRun 输出包含构建参数" $dryRun "--force"
    $markerCount = @(Get-MarkerFiles).Count
    Assert-True "DryRun 未执行任何伪造外部命令（marker=0）" ($markerCount -eq 0) "(marker=$markerCount)"
    Assert-True "DryRun 未创建产物目录" (-not (Test-Path -LiteralPath $script:Artifacts)) "(exists=$script:Artifacts)"
    Assert-True "DryRun 未修改仓库产物目录" ($artifactSnapshot -eq (Get-TreeFingerprint -Root $script:ArtifactSnapshotRoot))

    Write-Host "== 场景 2：非法参数在早期失败 =="
    $cases = @(
        @{ Name = "DryRun 与 BuildOnly 互斥"; Args = @("-DryRun", "-BuildOnly", "-PublicHost", "example.test"); Needle = "-BuildOnly" },
        @{ Name = "RemoteRoot 不能是根目录"; Args = @("-DryRun", "-PublicHost", "example.test", "-RemoteRoot", "/"); Needle = "RemoteRoot" },
        @{ Name = "RemoteRoot 必须是绝对路径"; Args = @("-DryRun", "-PublicHost", "example.test", "-RemoteRoot", "../evil"); Needle = "RemoteRoot" },
        @{ Name = "RemoteRoot 拒绝 .. 路径段"; Args = @("-DryRun", "-PublicHost", "example.test", "-RemoteRoot", "/opt/../etc"); Needle = "RemoteRoot" },
        @{ Name = "PublicHost 拒绝路径"; Args = @("-DryRun", "-PublicHost", "https://bad.test/path"); Needle = "-PublicHost" },
        @{ Name = "PublicHost 拒绝端口"; Args = @("-DryRun", "-PublicHost", "https://bad.test:8443"); Needle = "-PublicHost" },
        @{ Name = "HttpPort 范围校验"; Args = @("-DryRun", "-PublicHost", "example.test", "-HttpPort", "70000"); Needle = "-HttpPort" },
        @{ Name = "KeepReleases 范围校验"; Args = @("-DryRun", "-PublicHost", "example.test", "-KeepReleases", "0"); Needle = "-KeepReleases" },
        @{ Name = "RollbackTo 版本号校验"; Args = @("-DryRun", "-PublicHost", "example.test", "-RollbackTo", "../bad"); Needle = "RollbackTo" }
    )
    foreach ($case in $cases) {
        Clear-Markers
        $result = Invoke-DeployScript -Arguments $case.Args
        Assert-ExitNonZero "$($case.Name)：退出码非 0" $result
        Assert-OutputContains "$($case.Name)：给出明确错误" $result $case.Needle
    }
    Clear-Markers
    $missingBase = Invoke-DeployScript -Arguments @("-DryRun")
    Assert-ExitNonZero "缺少公共地址时失败" $missingBase
    Assert-OutputContains "缺少公共地址时提示来源" $missingBase "KELEE_PUBLIC_BASE"

    Clear-Markers
    $inconsistent = Invoke-DeployScript -Arguments @("-DryRun", "-PublicHost", "b.example") `
        -Environment @{ KELEE_PUBLIC_BASE = "https://a.example" }
    Assert-ExitNonZero "多个地址来源不一致时失败" $inconsistent
    Assert-OutputContains "地址不一致错误列出 KELEE_PUBLIC_BASE" $inconsistent "KELEE_PUBLIC_BASE"
    Assert-OutputContains "地址不一致错误列出 -PublicHost" $inconsistent "-PublicHost"

    Write-Host "== 场景 3：-BuildOnly 只构建校验，不接触远端 =="
    Clear-Markers
    if (Test-Path -LiteralPath $script:Artifacts) { Remove-Item -LiteralPath $script:Artifacts -Recurse -Force }
    $buildOnly = Invoke-DeployScript -Arguments @(
        "-BuildOnly", "-PublicHost", "example.test", "-SourceDir", $script:Artifacts
    ) -Environment @{ FAKE_BUILD_OUT = $script:Artifacts; FAKE_MARKER_DIR = $script:Markers }
    Assert-ExitCode "BuildOnly 退出码为 0" $buildOnly 0
    Assert-OutputContains "BuildOnly 输出包含 BuildOnly 标识" $buildOnly "BuildOnly"
    Assert-OutputContains "BuildOnly 输出包含构建指纹校验" $buildOnly "fingerprint=fake-fingerprint"
    Assert-True "BuildOnly 产物 index.html 已生成" (Test-Path -LiteralPath (Join-Path $script:Artifacts "index.html"))
    $pythonMarkers = @(Get-MarkerFiles | Where-Object { $_.Name -like "python*" })
    Assert-True "BuildOnly 调用了伪造 python（marker 存在）" ($pythonMarkers.Count -ge 1) "(python markers=$($pythonMarkers.Count))"
    $remoteMarkers = @(Get-MarkerFiles | Where-Object { $_.Name -in @("ssh.marker", "scp.marker", "tar.marker") })
    Assert-True "BuildOnly 未执行 ssh/scp/tar（marker=0）" ($remoteMarkers.Count -eq 0) "(markers=$($remoteMarkers -join ','))"
    Assert-True "BuildOnly 未修改仓库产物目录" ($artifactSnapshot -eq (Get-TreeFingerprint -Root $script:ArtifactSnapshotRoot))

    Write-Host "== 场景 4：构建失败 / 构建清单损坏都会阻断 =="
    Clear-Markers
    if (Test-Path -LiteralPath $script:Artifacts) { Remove-Item -LiteralPath $script:Artifacts -Recurse -Force }
    $builderFail = Invoke-DeployScript -Arguments @(
        "-BuildOnly", "-PublicHost", "example.test", "-SourceDir", $script:Artifacts
    ) -Environment @{ FAKE_BUILD_OUT = $script:Artifacts; FAKE_MARKER_DIR = $script:Markers; FAKE_BUILD_MODE = "fail" }
    Assert-ExitNonZero "构建器失败时 BuildOnly 失败" $builderFail
    Assert-OutputContains "构建失败错误码为 1" $builderFail "1"

    Clear-Markers
    if (Test-Path -LiteralPath $script:Artifacts) { Remove-Item -LiteralPath $script:Artifacts -Recurse -Force }
    $corrupt = Invoke-DeployScript -Arguments @(
        "-BuildOnly", "-PublicHost", "example.test", "-SourceDir", $script:Artifacts
    ) -Environment @{ FAKE_BUILD_OUT = $script:Artifacts; FAKE_MARKER_DIR = $script:Markers; FAKE_BUILD_MODE = "corrupt-manifest" }
    Assert-ExitNonZero "构建清单哈希不一致时失败" $corrupt
    Assert-OutputContains "构建清单哈希错误指向 index.html" $corrupt "index.html"

    Write-Host ""
    Write-Host "== 汇总 =="
    Write-Host "通过 $($script:Passed)/$($script:Checks) 项检查"
    if ($script:Failures -gt 0) {
        Write-Host "失败 $($script:Failures) 项" -ForegroundColor Red
        exit 1
    }
    Write-Host "PASS: deploy DryRun/BuildOnly tests"
    exit 0
} catch {
    Write-Host "FAIL: 测试运行异常：$($_.Exception.Message)" -ForegroundColor Red
    Write-Host $_.ScriptStackTrace
    exit 1
} finally {
    $env:PATH = $script:SavedPath
    if (Test-Path -LiteralPath $script:TestRoot) {
        Remove-Item -LiteralPath $script:TestRoot -Recurse -Force -ErrorAction SilentlyContinue
    }
}
