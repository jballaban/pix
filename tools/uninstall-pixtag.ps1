<#
.SYNOPSIS
  Remove the "Tag with pix" Explorer context-menu entry (current user).

.DESCRIPTION
  Deletes the HKCU keys created by install-pixtag.ps1. Safe to run even if the
  entry was never installed (missing keys are ignored).
#>
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'

$roots = @(
    'HKCU:\Software\Classes\*\shell\pixtag',
    'HKCU:\Software\Classes\Directory\shell\pixtag'
)

$removed = 0
foreach ($key in $roots) {
    if (Test-Path -LiteralPath $key) {
        Remove-Item -LiteralPath $key -Recurse -Force
        $removed++
    }
}

if ($removed -gt 0) {
    Write-Host 'Removed the "Tag with pix" context menu (current user).' -ForegroundColor Green
}
else {
    Write-Host 'Nothing to remove — the context menu was not installed.' -ForegroundColor Yellow
}
