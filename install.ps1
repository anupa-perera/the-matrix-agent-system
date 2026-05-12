param(
    [string] $Source = "",
    [string] $Python = "3.13",
    [switch] $SkipStart,
    [switch] $NoForce,
    [switch] $NoShortcuts
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

function Write-Step {
    param([string] $Message)
    Write-Host ""
    Write-Host "==> $Message" -ForegroundColor Cyan
}

function Resolve-InstallSource {
    param([string] $RequestedSource)

    if (-not [string]::IsNullOrWhiteSpace($RequestedSource)) {
        return $RequestedSource
    }

    $scriptRoot = $PSScriptRoot
    if ([string]::IsNullOrWhiteSpace($scriptRoot)) {
        $scriptRoot = (Get-Location).Path
    }

    $localProject = Join-Path $scriptRoot "pyproject.toml"
    if (Test-Path $localProject) {
        return $scriptRoot
    }

    return "https://github.com/anupa-perera/the-matrix-agent-system/archive/refs/heads/main.zip"
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

function New-MatrixShortcut {
    param(
        [string] $ShortcutPath,
        [string] $MatrixCommand
    )

    $parent = Split-Path -Parent $ShortcutPath
    if (-not (Test-Path $parent)) {
        New-Item -ItemType Directory -Path $parent -Force | Out-Null
    }

    $shell = New-Object -ComObject WScript.Shell
    $shortcut = $shell.CreateShortcut($ShortcutPath)
    $shortcut.TargetPath = $env:ComSpec
    $shortcut.Arguments = "/k `"$MatrixCommand`" start"
    $shortcut.WorkingDirectory = Split-Path -Parent $MatrixCommand
    $shortcut.Description = "Start The Matrix local agent system"
    $shortcut.Save()
}

function Install-MatrixShortcuts {
    param([string] $MatrixCommand)

    Write-Step "Creating shortcuts"
    $desktop = [Environment]::GetFolderPath("Desktop")
    $startMenu = Join-Path ([Environment]::GetFolderPath("StartMenu")) "Programs"
    $shortcuts = @(
        (Join-Path $desktop "The Matrix.lnk"),
        (Join-Path $startMenu "The Matrix.lnk")
    )

    foreach ($shortcut in $shortcuts) {
        New-MatrixShortcut -ShortcutPath $shortcut -MatrixCommand $MatrixCommand
        Write-Host "Shortcut: $shortcut"
    }
}

Write-Host "The Matrix installer"
Write-Host "This installs the CLI for the current Windows user. Admin rights are not required."

$Source = Resolve-InstallSource -RequestedSource $Source
$uvPath = Install-UvIfMissing

Write-Step "Installing The Matrix Agent System"
$installArgs = @("--system-certs", "tool", "install", "--python", $Python)
if (-not $NoForce) {
    $installArgs += "--force"
}
$installArgs += $Source

Write-Host "Using uv: $uvPath"
Write-Host "Source: $Source"
& $uvPath @installArgs
if ($LASTEXITCODE -ne 0) {
    throw "The Matrix installation failed. Close any open Matrix windows, check the error above, and run the installer again."
}

Add-ToolBinToCurrentPath
$matrix = Find-TheMatrix
if (-not $matrix) {
    throw "The Matrix was installed, but the-matrix.exe was not found. Close and reopen PowerShell, then run: the-matrix start"
}

Write-Step "Installation complete"
Write-Host "Command: $matrix"

if (-not $NoShortcuts) {
    Install-MatrixShortcuts -MatrixCommand $matrix
}

if (-not $SkipStart) {
    Write-Step "Starting guided setup"
    & $matrix start
} else {
    Write-Host "Run this when ready:"
    Write-Host "  the-matrix start"
}
