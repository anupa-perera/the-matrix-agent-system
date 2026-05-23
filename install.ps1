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

    if (-not [string]::IsNullOrWhiteSpace($env:MATRIX_SOURCE)) {
        return $env:MATRIX_SOURCE
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

function New-RoundedRectanglePath {
    param(
        [float] $X,
        [float] $Y,
        [float] $Width,
        [float] $Height,
        [float] $Radius
    )

    $path = New-Object System.Drawing.Drawing2D.GraphicsPath
    $diameter = $Radius * 2
    $path.AddArc($X, $Y, $diameter, $diameter, 180, 90)
    $path.AddArc($X + $Width - $diameter, $Y, $diameter, $diameter, 270, 90)
    $path.AddArc($X + $Width - $diameter, $Y + $Height - $diameter, $diameter, $diameter, 0, 90)
    $path.AddArc($X, $Y + $Height - $diameter, $diameter, $diameter, 90, 90)
    $path.CloseFigure()
    return $path
}

function New-MatrixIcon {
    $iconDir = Join-Path $HOME ".thematrix"
    $iconPath = Join-Path $iconDir "the-matrix.ico"

    try {
        if (-not (Test-Path $iconDir)) {
            New-Item -ItemType Directory -Path $iconDir -Force | Out-Null
        }

        Add-Type -AssemblyName System.Drawing
        if (-not ("MatrixNativeMethods" -as [type])) {
            Add-Type @"
using System;
using System.Runtime.InteropServices;
public static class MatrixNativeMethods {
    [DllImport("user32.dll", CharSet = CharSet.Auto)]
    public static extern bool DestroyIcon(IntPtr handle);
}
"@
        }

        $bitmap = $null
        $graphics = $null
        $handle = [IntPtr]::Zero
        $stream = $null
        $icon = $null
        $outer = $null
        $inner = $null
        $background = $null
        $innerBrush = $null
        $border = $null
        $glow = $null
        $mark = $null
        $underscore = $null
        try {
            $bitmap = New-Object System.Drawing.Bitmap -ArgumentList @(64, 64, [System.Drawing.Imaging.PixelFormat]::Format32bppArgb)
            $graphics = [System.Drawing.Graphics]::FromImage($bitmap)
            $graphics.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::AntiAlias
            $graphics.Clear([System.Drawing.Color]::Transparent)

            $outer = New-RoundedRectanglePath -X 2 -Y 2 -Width 60 -Height 60 -Radius 10
            $inner = New-RoundedRectanglePath -X 7 -Y 7 -Width 50 -Height 50 -Radius 6

            $backgroundRect = New-Object System.Drawing.Rectangle -ArgumentList @(0, 0, 64, 64)
            $background = New-Object System.Drawing.Drawing2D.LinearGradientBrush -ArgumentList @(
                $backgroundRect,
                [System.Drawing.Color]::FromArgb(255, 0, 24, 7),
                [System.Drawing.Color]::FromArgb(255, 0, 0, 0),
                [System.Drawing.Drawing2D.LinearGradientMode]::ForwardDiagonal
            )
            $graphics.FillPath($background, $outer)
            $innerBrush = New-Object System.Drawing.SolidBrush -ArgumentList ([System.Drawing.Color]::FromArgb(210, 0, 20, 5))
            $graphics.FillPath($innerBrush, $inner)

            $border = New-Object System.Drawing.Pen -ArgumentList @([System.Drawing.Color]::FromArgb(255, 0, 255, 65), 3)
            $graphics.DrawPath($border, $inner)

            $glow = New-Object System.Drawing.Pen -ArgumentList @([System.Drawing.Color]::FromArgb(80, 0, 255, 65), 11)
            $glow.StartCap = [System.Drawing.Drawing2D.LineCap]::Round
            $glow.EndCap = [System.Drawing.Drawing2D.LineCap]::Round
            $glow.LineJoin = [System.Drawing.Drawing2D.LineJoin]::Round
            $graphics.DrawLines($glow, @(
                (New-Object System.Drawing.Point -ArgumentList @(18, 18)),
                (New-Object System.Drawing.Point -ArgumentList @(31, 32)),
                (New-Object System.Drawing.Point -ArgumentList @(18, 46))
            ))
            $graphics.DrawLine($glow, 37, 44, 50, 44)

            $mark = New-Object System.Drawing.Pen -ArgumentList @([System.Drawing.Color]::FromArgb(255, 0, 255, 65), 6)
            $mark.StartCap = [System.Drawing.Drawing2D.LineCap]::Round
            $mark.EndCap = [System.Drawing.Drawing2D.LineCap]::Round
            $mark.LineJoin = [System.Drawing.Drawing2D.LineJoin]::Round
            $graphics.DrawLines($mark, @(
                (New-Object System.Drawing.Point -ArgumentList @(18, 18)),
                (New-Object System.Drawing.Point -ArgumentList @(31, 32)),
                (New-Object System.Drawing.Point -ArgumentList @(18, 46))
            ))

            $underscore = New-Object System.Drawing.Pen -ArgumentList @([System.Drawing.Color]::FromArgb(255, 124, 255, 157), 6)
            $underscore.StartCap = [System.Drawing.Drawing2D.LineCap]::Round
            $underscore.EndCap = [System.Drawing.Drawing2D.LineCap]::Round
            $graphics.DrawLine($underscore, 37, 44, 50, 44)

            $handle = $bitmap.GetHicon()
            $icon = [System.Drawing.Icon]::FromHandle($handle)
            $stream = [System.IO.File]::Create($iconPath)
            $icon.Save($stream)
        } finally {
            foreach ($resource in @($stream, $icon, $underscore, $mark, $glow, $border, $innerBrush, $background, $inner, $outer, $graphics, $bitmap)) {
                if ($null -ne $resource) {
                    $resource.Dispose()
                }
            }
            if ($handle -ne [IntPtr]::Zero) { [MatrixNativeMethods]::DestroyIcon($handle) | Out-Null }
        }

        return $iconPath
    } catch {
        Write-Host "Shortcut icon could not be created. Using the default Windows icon." -ForegroundColor Yellow
        return $null
    }
}

function New-MatrixShortcut {
    param(
        [string] $ShortcutPath,
        [string] $MatrixCommand,
        [string] $IconPath
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
    if (-not [string]::IsNullOrWhiteSpace($IconPath) -and (Test-Path $IconPath)) {
        $shortcut.IconLocation = "$IconPath,0"
    }
    $shortcut.Save()
}

function Install-MatrixShortcuts {
    param([string] $MatrixCommand)

    Write-Step "Creating shortcuts"
    $iconPath = New-MatrixIcon
    $desktop = [Environment]::GetFolderPath("Desktop")
    $startMenu = Join-Path ([Environment]::GetFolderPath("StartMenu")) "Programs"
    $shortcuts = @(
        (Join-Path $desktop "The Matrix.lnk"),
        (Join-Path $startMenu "The Matrix.lnk")
    )

    foreach ($shortcut in $shortcuts) {
        New-MatrixShortcut -ShortcutPath $shortcut -MatrixCommand $MatrixCommand -IconPath $iconPath
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
