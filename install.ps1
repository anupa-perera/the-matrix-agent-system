param(
    [string] $Source = "https://github.com/anupa-perera/the-matrix-agent-system/archive/refs/heads/main.zip",
    [string] $Python = "3.13",
    [switch] $SkipStart,
    [switch] $NoForce
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

function Write-Step {
    param([string] $Message)
    Write-Host ""
    Write-Host "==> $Message" -ForegroundColor Cyan
}

function Find-Uv {
    $command = Get-Command uv -ErrorAction SilentlyContinue
    if ($command) {
        return $command.Source
    }

    $candidates = @(
        (Join-Path $HOME ".local\bin\uv.exe"),
        (Join-Path $env:LOCALAPPDATA "Programs\uv\uv.exe"),
        (Join-Path $env:USERPROFILE ".cargo\bin\uv.exe")
    )

    foreach ($candidate in $candidates) {
        if (Test-Path $candidate) {
            return $candidate
        }
    }

    return $null
}

function Add-UvToCurrentPath {
    $paths = @(
        (Join-Path $HOME ".local\bin"),
        (Join-Path $env:LOCALAPPDATA "Programs\uv"),
        (Join-Path $env:USERPROFILE ".cargo\bin")
    )

    foreach ($path in $paths) {
        if ((Test-Path $path) -and (($env:PATH -split ";") -notcontains $path)) {
            $env:PATH = "$path;$env:PATH"
        }
    }
}

function Install-UvIfMissing {
    $uv = Find-Uv
    if ($uv) {
        Write-Host "uv found: $uv"
        return $uv
    }

    Write-Step "Installing uv"
    Write-Host "This downloads uv from the official Astral installer."
    powershell -ExecutionPolicy ByPass -NoProfile -Command "irm https://astral.sh/uv/install.ps1 | iex"
    Add-UvToCurrentPath

    $uv = Find-Uv
    if (-not $uv) {
        throw "uv was installed, but uv.exe was not found on PATH. Close and reopen PowerShell, then run this script again."
    }
    return $uv
}

function Find-TheMatrix {
    $command = Get-Command the-matrix -ErrorAction SilentlyContinue
    if ($command) {
        return $command.Source
    }

    $candidates = @(
        (Join-Path $HOME ".local\bin\the-matrix.exe")
    )

    foreach ($candidate in $candidates) {
        if (Test-Path $candidate) {
            return $candidate
        }
    }

    return $null
}

function Add-ToolBinToCurrentPath {
    $toolBin = Join-Path $HOME ".local\bin"
    if ((Test-Path $toolBin) -and (($env:PATH -split ";") -notcontains $toolBin)) {
        $env:PATH = "$toolBin;$env:PATH"
    }
}

Write-Host "The Matrix installer"
Write-Host "This installs the CLI for the current Windows user. Admin rights are not required."

$uvPath = Install-UvIfMissing

Write-Step "Installing The Matrix Agent System"
$installArgs = @("tool", "install", "--python", $Python)
if (-not $NoForce) {
    $installArgs += "--force"
}
$installArgs += $Source

Write-Host "Using uv: $uvPath"
Write-Host "Source: $Source"
& $uvPath @installArgs

Add-ToolBinToCurrentPath
$matrix = Find-TheMatrix
if (-not $matrix) {
    throw "The Matrix was installed, but the-matrix.exe was not found. Close and reopen PowerShell, then run: the-matrix start"
}

Write-Step "Installation complete"
Write-Host "Command: $matrix"

if (-not $SkipStart) {
    Write-Step "Starting guided setup"
    & $matrix start
} else {
    Write-Host "Run this when ready:"
    Write-Host "  the-matrix start"
}
