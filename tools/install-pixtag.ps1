<#
.SYNOPSIS
  Register the "Tag with pix" Explorer context-menu entry (current user).

.DESCRIPTION
  Writes HKCU registry keys so right-clicking selected files and/or folders
  shows "Tag with pix", which launches tools\pixtag.ps1. HKCU only — no admin
  rights needed, and it affects just the current user. Re-run any time to
  refresh the command (idempotent). Uninstall with uninstall-pixtag.ps1.

  Multi-select works: the launcher aggregates the per-item invocations the
  classic menu makes (see pixtag.ps1).
#>
[CmdletBinding()]
param(
    # Menu label shown in the right-click menu.
    [string] $Label = 'Tag with pix'
)

$ErrorActionPreference = 'Stop'

$launcher = Join-Path $PSScriptRoot 'pixtag.ps1'
if (-not (Test-Path -LiteralPath $launcher)) {
    throw "Launcher not found next to this script: $launcher"
}

$psExe = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
# %1 = the clicked item; the classic menu fires this once per selected item.
$command = "`"$psExe`" -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$launcher`" `"%1`""

# Files (*) and folders (Directory) are separate roots in the classic menu.
$roots = @(
    'HKCU:\Software\Classes\*\shell\pixtag',
    'HKCU:\Software\Classes\Directory\shell\pixtag'
)

foreach ($key in $roots) {
    $cmdKey = Join-Path $key 'command'
    New-Item -Path $cmdKey -Force | Out-Null
    Set-ItemProperty -Path $key -Name '(default)' -Value $Label
    # Borrow pix's own icon if one ever ships; harmless if pix has no icon.
    Set-ItemProperty -Path $key -Name 'Icon' -Value $psExe
    Set-ItemProperty -Path $cmdKey -Name '(default)' -Value $command
}

Write-Host "Installed '$Label' context menu for files and folders (current user)." -ForegroundColor Green
Write-Host "Launcher: $launcher"
Write-Host 'Right-click any media files/folders in a pix library and choose the entry.'
