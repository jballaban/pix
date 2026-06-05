<#
.SYNOPSIS
  Windows Explorer context-menu launcher for `pix set` / `pix clear`.

.DESCRIPTION
  Registered by `pix context-menu install` as a cascading menu:

      Pix  >  Event | Date  >  Set value... | Clear

  The menu leaf encodes the tag (-Tag event|date) and operation
  (-Op set|clear), so this launcher only has to collect the value (for set)
  and forward the selected paths. The classic menu invokes the leaf command
  once per selected item, so the script runs in two stages:

    1. COLLATE (default, hidden window). Each per-item process appends its
       path to a shared pending list and races to become "leader". Non-leaders
       exit immediately. The leader waits for the burst to settle, snapshots
       the full selection, then relaunches itself in RUN mode in a visible
       console (carrying -Tag/-Op). This is the COM-free way to aggregate a
       multi-select with a pure-registry context menu.

    2. RUN (-Run <listfile>, visible window). Reads the collected paths and,
       for set, prompts for the value (event name / date pattern); for clear,
       no value is needed. Then calls `pix set` / `pix clear`. pix shows its
       own Apply plan + confirmation in the same console, so nothing is
       written without review.

  Folder expansion is done by pix itself (a folder arg expands to the taggable
  media it contains), so this launcher just forwards whatever Explorer selected.
#>
[CmdletBinding(DefaultParameterSetName = 'Collate')]
param(
    # COLLATE mode: the selected path(s) (the registry passes one "%1" per item).
    # Position 0 + remaining-args so a bare "%1" positional binds here.
    [Parameter(ParameterSetName = 'Collate', Position = 0, ValueFromRemainingArguments = $true)]
    [string[]] $Paths,
    # RUN mode: path to the snapshot list file produced by the COLLATE leader.
    [Parameter(ParameterSetName = 'Run', Mandatory = $true)]
    [string] $Run,
    # Common to both stages: which tag and operation the menu leaf chose.
    [ValidateSet('event', 'date')]
    [string] $Tag = 'event',
    [ValidateSet('set', 'clear', 'meta')]
    [string] $Op = 'set'
)

$ErrorActionPreference = 'Stop'

$workDir = Join-Path $env:TEMP 'pixtag'
$pending = Join-Path $workDir 'pending.txt'
$leader  = Join-Path $workDir 'leader.lock'

function Get-PixMutex {
    # One named mutex serializes the append + leader-election critical section
    # across the per-item processes the shell spawns.
    New-Object System.Threading.Mutex($false, 'Global\pixtag_collate')
}

# ---------------------------------------------------------------------------
# RUN mode - the interactive, visible stage.
# ---------------------------------------------------------------------------
if ($Run) {
    $items = @()
    if (Test-Path -LiteralPath $Run) {
        $items = @(Get-Content -LiteralPath $Run -Encoding UTF8 | Where-Object { $_ -ne '' })
        Remove-Item -LiteralPath $Run -Force -ErrorAction SilentlyContinue
    }

    if ($items.Count -eq 0) {
        Write-Host 'pixtag: nothing selected.' -ForegroundColor Yellow
        Read-Host 'Press Enter to close'
        return
    }

    if (-not (Get-Command pix -ErrorAction SilentlyContinue)) {
        Write-Host 'pixtag: `pix` is not on PATH. Install it (uv tool install) first.' -ForegroundColor Red
        Read-Host 'Press Enter to close'
        return
    }

    # meta is read-only and single-file; run it for each selected item in turn.
    if ($Op -eq 'meta') {
        Write-Host ''
        Write-Host "pix meta - $($items.Count) item(s):" -ForegroundColor Cyan
        foreach ($it in $items) {
            Write-Host ''
            & pix meta $it
        }
        Write-Host ''
        Read-Host 'Press Enter to close'
        return
    }

    Write-Host ''
    Write-Host "pix $Op $Tag - $($items.Count) item(s) selected:" -ForegroundColor Cyan
    foreach ($i in ($items | Select-Object -First 10)) { Write-Host "  $i" }
    if ($items.Count -gt 10) { Write-Host "  ... and $($items.Count - 10) more" }
    Write-Host ''

    if ($Op -eq 'clear') {
        & pix clear $Tag @items
    }
    else {
        if ($Tag -eq 'date') {
            Write-Host 'Date override pattern: YYYY-MM-DD-HH:MM:SS with * for any unpinned part.'
            Write-Host '  e.g. 2022-*-*-*:*:* (pin year)  or  2022-08-15-*:*:* (pin the day)'
            $value = Read-Host 'Date value'
        }
        else {
            $value = Read-Host 'Event name'
        }
        if ([string]::IsNullOrWhiteSpace($value)) {
            Write-Host 'Cancelled - no value entered (use the Clear menu to remove a tag).' -ForegroundColor Yellow
            Read-Host 'Press Enter to close'
            return
        }
        & pix set $Tag $value @items
    }

    Write-Host ''
    Read-Host 'Press Enter to close'
    return
}

# ---------------------------------------------------------------------------
# COLLATE mode - the fast, hidden stage (one process per selected item).
# ---------------------------------------------------------------------------
if (-not $Paths -or $Paths.Count -eq 0) { return }

New-Item -ItemType Directory -Force -Path $workDir | Out-Null

$mutex = Get-PixMutex
$iAmLeader = $false
[void] $mutex.WaitOne()
try {
    Add-Content -LiteralPath $pending -Value $Paths -Encoding UTF8
    if (-not (Test-Path -LiteralPath $leader)) {
        New-Item -ItemType File -Path $leader -Force | Out-Null
        $iAmLeader = $true
    }
}
finally {
    $mutex.ReleaseMutex()
}

if (-not $iAmLeader) { return }   # a sibling is the leader and will do the work

# Leader: wait for the shell's burst of per-item launches to settle. Poll the
# pending count until it stops growing (a quiet period), capped so a hang can't
# block forever.
$lastCount = -1
$deadline = (Get-Date).AddSeconds(10)
while ((Get-Date) -lt $deadline) {
    Start-Sleep -Milliseconds 400
    [void] $mutex.WaitOne()
    try {
        $count = @(Get-Content -LiteralPath $pending -Encoding UTF8 -ErrorAction SilentlyContinue).Count
    }
    finally {
        $mutex.ReleaseMutex()
    }
    if ($count -eq $lastCount) { break }
    $lastCount = $count
}

# Snapshot the full selection and reset, so any later (separate) action starts
# a fresh group with its own leader.
$itemsFile = Join-Path $workDir ("items-{0}.txt" -f ([guid]::NewGuid().ToString('N')))
[void] $mutex.WaitOne()
try {
    $all = @(Get-Content -LiteralPath $pending -Encoding UTF8 -ErrorAction SilentlyContinue | Where-Object { $_ -ne '' })
    Set-Content -LiteralPath $itemsFile -Value $all -Encoding UTF8
    Remove-Item -LiteralPath $pending -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $leader -Force -ErrorAction SilentlyContinue
}
finally {
    $mutex.ReleaseMutex()
    $mutex.Dispose()
}

# Hand off to the visible RUN stage, carrying the menu's tag + operation.
# PIXTAG_COLLATE_ONLY is a test seam: it skips the relaunch and just reports
# the snapshot file, so the collation shim can be exercised headlessly.
if ($env:PIXTAG_COLLATE_ONLY) {
    Write-Output $itemsFile
    return
}
$psExe = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
Start-Process -FilePath $psExe -ArgumentList @(
    '-NoProfile', '-ExecutionPolicy', 'Bypass',
    '-File', $PSCommandPath, '-Run', $itemsFile, '-Tag', $Tag, '-Op', $Op
)
