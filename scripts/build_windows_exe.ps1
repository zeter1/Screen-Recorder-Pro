param(
    [string]$OutputName = "Screen-Recorder-Pro"
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $projectRoot

$ffmpegCommand = Get-Command ffmpeg -ErrorAction Stop
$ffprobeCommand = Get-Command ffprobe -ErrorAction Stop
$ffmpegPath = $ffmpegCommand.Source
$ffprobePath = $ffprobeCommand.Source

Write-Host "Source root: $projectRoot"
Write-Host "FFmpeg: $ffmpegPath"
Write-Host "FFprobe: $ffprobePath"
python --version
python -m PyInstaller --version
& $ffmpegPath -version | Select-Object -First 1
& $ffprobePath -version | Select-Object -First 1

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
