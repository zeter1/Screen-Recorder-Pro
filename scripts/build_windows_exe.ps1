param(
    [string]$OutputName = "Screen-Recorder-Pro"
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $projectRoot

function Resolve-NativeFfmpegTool {
    param([Parameter(Mandatory = $true)][string]$Name)

    $command = Get-Command $Name -ErrorAction Stop
    $resolved = [System.IO.Path]::GetFullPath($command.Source)

    # Chocolatey exposes tiny shim executables in its global bin directory.
    # Those shims work only while Chocolatey metadata is present and therefore
    # must never be embedded into a portable PyInstaller EXE.
    if ($env:ChocolateyInstall) {
        $chocoBin = [System.IO.Path]::GetFullPath((Join-Path $env:ChocolateyInstall "bin"))
        if ($resolved.StartsWith($chocoBin, [System.StringComparison]::OrdinalIgnoreCase)) {
            $libRoot = Join-Path $env:ChocolateyInstall "lib"
            $packageRoots = @(Get-ChildItem -LiteralPath $libRoot -Directory -Filter "ffmpeg*" -ErrorAction SilentlyContinue)
            $nativeCandidates = @(
                foreach ($packageRoot in $packageRoots) {
                    Get-ChildItem -LiteralPath $packageRoot.FullName -File -Filter "$Name.exe" -Recurse -ErrorAction SilentlyContinue
                }
            )
            if ($nativeCandidates.Count -gt 0) {
                # Real FFmpeg binaries are much larger than Chocolatey shims.
                $resolved = ($nativeCandidates | Sort-Object Length -Descending | Select-Object -First 1).FullName
            }
        }
    }

    if (-not (Test-Path -LiteralPath $resolved -PathType Leaf)) {
        throw "Unable to resolve native $Name executable: $resolved"
    }
    return $resolved
}

function Assert-NativeToolWorks {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][string]$Path
    )

    $output = @(& $Path -version 2>&1)
    if ($LASTEXITCODE -ne 0) {
        throw "$Name failed before packaging with exit code $LASTEXITCODE. Path: $Path"
    }
    if ($output.Count -gt 0) {
        Write-Host $output[0]
    }
}

$ffmpegPath = Resolve-NativeFfmpegTool -Name "ffmpeg"
$ffprobePath = Resolve-NativeFfmpegTool -Name "ffprobe"

Write-Host "Source root: $projectRoot"
Write-Host "Native FFmpeg: $ffmpegPath"
Write-Host "Native FFprobe: $ffprobePath"
python --version
python -m PyInstaller --version
Assert-NativeToolWorks -Name "ffmpeg" -Path $ffmpegPath
Assert-NativeToolWorks -Name "ffprobe" -Path $ffprobePath

Remove-Item -Recurse -Force build, dist -ErrorAction SilentlyContinue
Remove-Item -Force "$OutputName.spec" -ErrorAction SilentlyContinue

$pyInstallerArgs = @(
    "-m", "PyInstaller",
    "--noconfirm",
    "--clean",
    "--onefile",
    "--windowed",
    "--name", $OutputName,
    "--add-binary", "$ffmpegPath;.",
    "--add-binary", "$ffprobePath;.",
    "--add-data", "$projectRoot\screen_recorder;embedded_source\screen_recorder",
    "--add-data", "$projectRoot\main.py;embedded_source",
    "--add-data", "$projectRoot\Screen Recorder Pro.py;embedded_source",
    "Screen Recorder Pro.py"
)

Write-Host ("Build command: python " + ($pyInstallerArgs -join " "))
& python @pyInstallerArgs
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller failed with exit code $LASTEXITCODE"
}

$exePath = Join-Path $projectRoot "dist\$OutputName.exe"
if (-not (Test-Path -LiteralPath $exePath -PathType Leaf)) {
    throw "Expected EXE was not created: $exePath"
}

Write-Host "Running packaged smoke check against the exact EXE..."
$smoke = Start-Process -FilePath $exePath -ArgumentList "--packaging-smoke" -PassThru -Wait
if ($smoke.ExitCode -ne 0) {
    throw "Packaged smoke check failed with exit code $($smoke.ExitCode)"
}

$hash = Get-FileHash -Algorithm SHA256 -LiteralPath $exePath
$size = (Get-Item -LiteralPath $exePath).Length
$manifestPath = Join-Path $projectRoot "dist\$OutputName.sha256.txt"
@(
    "artifact=$OutputName.exe"
    "size_bytes=$size"
    "sha256=$($hash.Hash.ToLowerInvariant())"
) | Set-Content -LiteralPath $manifestPath -Encoding utf8

Write-Host "PACKAGED_EXE_OK: $exePath"
Write-Host "SIZE_BYTES: $size"
Write-Host "SHA256: $($hash.Hash.ToLowerInvariant())"
