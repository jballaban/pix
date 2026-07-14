<#
.SYNOPSIS
  Windows Explorer context-menu launcher for `pix tag set` / `pix tag clear` /
  `pix tag rotate` / `pix info meta`.

.DESCRIPTION
  Registered by `pix context-menu install` as a cascading menu:

      Pix  >  Event | Date  >  Set value... | Clear
                                Info   (files only)

  The menu leaf encodes the tag (-Tag event|date) and operation
  (-Op set|clear|meta). A classic registry verb is invoked once per selected
  item, and only up to a hard Windows limit (100 for a legacy verb) — so
  rather than try to receive every path via %1, the script runs in two stages:

    1. COLLATE (default, hidden window). The first invocation becomes the
       "leader"; a short-lived freshness lock makes the sibling per-item
       invocations from the same selection exit silently. The leader reads the
       *live selection* of the Explorer window the menu was invoked from, via
       COM (Shell.Application). That returns the complete selection in one shot
       with no 100-item cap and no dependence on Explorer's per-item launch
       waves. It then relaunches itself in RUN mode in a visible console.

    2. RUN (-Run <listfile>, visible window). Reads the collected paths and,
       for set, prompts for the value; for clear/meta, no value is needed. Then
       calls `pix`, which shows its own Apply plan + confirmation.

  Folder expansion is done by pix itself (a folder arg expands to the taggable
  media it contains), so the launcher just forwards whatever was selected.
#>
[CmdletBinding(DefaultParameterSetName = 'Collate')]
param(
    # COLLATE mode: the clicked item (the registry passes "%1"). Used only to
    # identify which Explorer window's live selection to read.
    [Parameter(ParameterSetName = 'Collate', Position = 0, ValueFromRemainingArguments = $true)]
    [string[]] $Paths,
    # RUN mode: path to the snapshot list file produced by the COLLATE leader.
    [Parameter(ParameterSetName = 'Run', Mandatory = $true)]
    [string] $Run,
    # Common to both stages: which tag, operation, and (for rotate) degrees.
    [ValidateSet('event', 'date')]
    [string] $Tag = 'event',
    [ValidateSet('set', 'clear', 'meta', 'rotate')]
    [string] $Op = 'set',
    [string] $Deg = ''
)

$ErrorActionPreference = 'Stop'

$workDir = Join-Path $env:TEMP 'pixtag'
$leader  = Join-Path $workDir 'leader.lock'
$logFile = Join-Path $workDir 'last-run.log'

function Write-PixLog {
    # Breadcrumb trail to %TEMP%\pixtag\last-run.log. Best-effort: a logging
    # failure must never derail the menu action. Lets a vanished-window report
    # be diagnosed after the fact (which stage ran, where it stopped).
    param([string] $Message)
    try {
        if (-not (Test-Path -LiteralPath $workDir)) {
            New-Item -ItemType Directory -Force -Path $workDir | Out-Null
        }
        Add-Content -LiteralPath $logFile -Value (
            '{0} pid={1} {2}' -f (Get-Date -Format 'o'), $PID, $Message
        )
    } catch {}
}

# Catch-all so the launcher never dies silently. Any uncaught terminating
# error (a COM hiccup reading the selection, a bad path, ...) is logged; in
# the visible RUN stage it's also shown with a pause, so the window can't
# just vanish with no trace — the exact symptom this guards against.
trap {
    Write-PixLog ('UNCAUGHT ' + $_.Exception.GetType().Name + ': ' + $_.Exception.Message)
    if ($Run) {
        Write-Host ''
        Write-Host ('pixtag error: ' + $_.Exception.Message) -ForegroundColor Red
        Write-Host "(details logged to $logFile)" -ForegroundColor DarkGray
        try { Read-Host 'Press Enter to close' } catch {}
    }
    exit 1
}

# How long a leader's lock suppresses sibling invocations from the same
# selection. Must comfortably exceed Explorer's per-item launch spread (it
# fires large selections in waves). The cost of too long is that a *separate*
# tag action begun within this window would be ignored.
$LeaderStaleSeconds = 6

function Get-PixMutex {
    # Serializes the leader check-and-claim across the per-item processes.
    New-Object System.Threading.Mutex($false, 'Global\pixtag_collate')
}

function Get-ExplorerSelection {
    # Return every path currently selected in the Explorer window the menu was
    # invoked from (the one whose selection contains $Clicked). Falls back to
    # just [$Clicked] for the Desktop or non-filesystem views, where the live
    # selection isn't reachable this way.
    param([string] $Clicked)

    $fallback = @($Clicked)
    $clickedFull = $Clicked
    try { $clickedFull = [System.IO.Path]::GetFullPath($Clicked) } catch {}

    try { $shell = New-Object -ComObject Shell.Application } catch { return $fallback }
    # The whole enumeration is guarded: if `$shell.Windows()` (or any COM call
    # not already caught below) throws, fall back to the clicked path rather
    # than let it escape and kill the leader before it spawns the RUN window.
    try {
        foreach ($w in $shell.Windows()) {
            $doc = $null
            try { $doc = $w.Document } catch { continue }
            if ($null -eq $doc) { continue }
            $selected = $null
            try { $selected = $doc.SelectedItems() } catch { continue }
            if ($null -eq $selected) { continue }
            $paths = @()
            foreach ($item in $selected) { try { $paths += $item.Path } catch {} }
            foreach ($p in $paths) {
                if ($p -ieq $clickedFull) { return $paths }
            }
        }
    } catch { return $fallback }
    return $fallback
}

function Get-EventValue {
    # Inline, editor-style autosuggest in the terminal (no GUI): the best
    # matching event shows as dim "ghost" text after the cursor, with its date
    # range as a further-dimmed hint to tell similar events apart. Tab or Right
    # accepts the name only (never the hint); Enter submits, Esc cancels.
    # Returns the text, or $null on cancel. Falls back to a plain prompt when
    # stdin isn't an interactive console (e.g. piped input in tests).
    param([string[]] $Names, [hashtable] $Ranges)

    if ([Console]::IsInputRedirected) {
        return (Read-Host 'Event name')
    }

    $sorted = @($Names | Sort-Object)
    $prompt = 'Event: '
    $buffer = ''
    $lastLen = 0

    Write-Host '(type to autocomplete; Tab/Right completes, Enter accepts, Esc cancels)' -ForegroundColor DarkGray

    while ($true) {
        # Best match = first event that case-insensitively starts with what's
        # typed. fullGhost = its remaining name (what Tab accepts); hint = its
        # date range (display only).
        $fullGhost = ''
        $hint = ''
        if ($buffer.Length -gt 0) {
            $lower = $buffer.ToLower()
            foreach ($s in $sorted) {
                if ($s.Length -ge $buffer.Length -and
                    $s.Substring(0, $buffer.Length).ToLower() -eq $lower) {
                    $fullGhost = $s.Substring($buffer.Length)
                    if ($Ranges -and $Ranges.ContainsKey($s) -and $Ranges[$s]) {
                        $hint = "  ($($Ranges[$s]))"
                    }
                    break
                }
            }
        }

        # Fit ghost (priority) then hint into the remaining console width.
        $width = [Console]::BufferWidth
        $avail = $width - 1 - ($prompt.Length + $buffer.Length)
        if ($avail -lt 0) { $avail = 0 }
        $ghost = $fullGhost
        if ($ghost.Length -gt $avail) { $ghost = $ghost.Substring(0, $avail) }
        $hintRoom = $avail - $ghost.Length
        if ($hint.Length -gt $hintRoom) { $hint = $hint.Substring(0, $hintRoom) }

        # Redraw: prompt + typed text, ghost (dim grey), range hint (dim cyan),
        # erase any leftover from a longer prior line, then park the cursor
        # right after the typed text.
        [Console]::CursorLeft = 0
        [Console]::Write($prompt + $buffer)
        $fg = [Console]::ForegroundColor
        [Console]::ForegroundColor = [ConsoleColor]::DarkGray
        [Console]::Write($ghost)
        [Console]::ForegroundColor = [ConsoleColor]::DarkCyan
        [Console]::Write($hint)
        [Console]::ForegroundColor = $fg
        $total = $prompt.Length + $buffer.Length + $ghost.Length + $hint.Length
        if ($total -lt $lastLen) { [Console]::Write(' ' * ($lastLen - $total)) }
        $lastLen = $total
        $col = $prompt.Length + $buffer.Length
        if ($col -ge $width) { $col = $width - 1 }
        [Console]::CursorLeft = $col

        $key = [Console]::ReadKey($true)
        switch ($key.Key) {
            'Enter' { [Console]::WriteLine(); return $buffer }
            'Escape' { [Console]::WriteLine(); return $null }
            'Backspace' {
                if ($buffer.Length -gt 0) {
                    $buffer = $buffer.Substring(0, $buffer.Length - 1)
                }
            }
            'Tab' { if ($fullGhost) { $buffer += $fullGhost } }
            'RightArrow' { if ($fullGhost) { $buffer += $fullGhost } }
            default {
                $ch = $key.KeyChar
                if ($ch -and -not [char]::IsControl($ch)) { $buffer += $ch }
            }
        }
    }
}

function Get-CommonAncestor {
    # Deepest folder containing all of $Paths (a file's parent folder; a folder
    # path is used as-is). This is the scope handed to `pix organize`.
    param([string[]] $Paths)
    $dirs = @()
    foreach ($p in $Paths) {
        if (Test-Path -LiteralPath $p -PathType Container) { $dirs += $p }
        else { $dirs += (Split-Path -Parent $p) }
    }
    $dirs = @($dirs | Where-Object { $_ } | Select-Object -Unique)
    if ($dirs.Count -eq 0) { return $null }
    if ($dirs.Count -eq 1) { return $dirs[0] }

    $split = @($dirs | ForEach-Object { , ($_ -split '[\\/]') })
    $min = ($split | ForEach-Object { $_.Count } | Measure-Object -Minimum).Minimum
    $common = @()
    for ($i = 0; $i -lt $min; $i++) {
        $seg = $split[0][$i]
        $match = $true
        foreach ($s in $split) { if ($s[$i] -ne $seg) { $match = $false; break } }
        if ($match) { $common += $seg } else { break }
    }
    if ($common.Count -eq 0) { return $null }
    return ($common -join '\')
}

function Invoke-Organize {
    # Reshape just the affected subtree so the freshly-tagged files land in
    # their event/date folders immediately. `pix organize` refuses to run with
    # the working directory inside the library, so step out to TEMP first.
    param([string[]] $Items)
    $scope = Get-CommonAncestor -Paths $Items
    if (-not $scope) { return }
    Write-Host ''
    Write-Host "Organizing $scope ..." -ForegroundColor Cyan
    $previous = (Get-Location).Path
    Set-Location -LiteralPath $env:TEMP
    try {
        & pix organize $scope --no-prompt
    }
    finally {
        Set-Location -LiteralPath $previous
    }
}

# ---------------------------------------------------------------------------
# RUN mode - the interactive, visible stage.
# ---------------------------------------------------------------------------
if ($Run) {
    Write-PixLog "RUN start Op=$Op Tag=$Tag Deg=$Deg list=$Run"
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
        Write-Host "pix info meta - $($items.Count) item(s):" -ForegroundColor Cyan
        foreach ($it in $items) {
            Write-Host ''
            & pix info meta $it
        }
        Write-Host ''
        Read-Host 'Press Enter to close'
        return
    }

    # rotate: lossless, no value prompt, no organize (orientation only).
    if ($Op -eq 'rotate') {
        Write-Host ''
        Write-Host "pix tag rotate $Deg - $($items.Count) item(s):" -ForegroundColor Cyan
        & pix tag rotate $Deg --no-prompt @items
        Write-Host ''
        Read-Host 'Press Enter to close'
        return
    }

    Write-Host ''
    Write-Host "pix tag $Op $Tag - $($items.Count) item(s) selected:" -ForegroundColor Cyan
    foreach ($i in ($items | Select-Object -First 10)) { Write-Host "  $i" }
    if ($items.Count -gt 10) { Write-Host "  ... and $($items.Count - 10) more" }
    Write-Host ''

    # Set's value dialog is its own confirmation, so it applies directly.
    # Clear instead keeps pix's own "Proceed? [Y/n]" prompt (default Yes): the
    # launcher can't know the real count when a folder is selected (it expands
    # to the media inside), but pix does — its prompt shows "clear ... on N
    # file(s)" and gates the mis-click.
    if ($Op -eq 'clear') {
        & pix tag clear $Tag @items
    }
    else {
        if ($Tag -eq 'date') {
            Write-Host 'Date override pattern: YYYY-MM-DD-HH:MM:SS with * for any unpinned part.'
            Write-Host '  e.g. 2022-*-*-*:*:* (pin year)  or  2022-08-15-*:*:* (pin the day)'
            $value = Read-Host 'Date value'
        }
        else {
            # Offer existing library events (with their date ranges) as
            # type-ahead suggestions. `pix info events` emits `name<TAB>range`.
            $names = @()
            $ranges = @{}
            try {
                foreach ($row in (& pix info events $items[0] 2>$null)) {
                    if (-not $row) { continue }
                    $parts = $row -split "`t", 2
                    $names += $parts[0]
                    if ($parts.Count -gt 1 -and $parts[1]) { $ranges[$parts[0]] = $parts[1] }
                }
            }
            catch {}
            $value = Get-EventValue -Names $names -Ranges $ranges
        }
        if ([string]::IsNullOrWhiteSpace($value)) {
            Write-Host 'Cancelled - no value entered (use the Clear menu to remove a tag).' -ForegroundColor Yellow
            Read-Host 'Press Enter to close'
            return
        }
        & pix tag set $Tag $value --no-prompt @items
    }

    # Tag write succeeded → organize the affected folder so the files move now.
    if ($LASTEXITCODE -eq 0) {
        Invoke-Organize -Items $items
    }

    Write-Host ''
    Read-Host 'Press Enter to close'
    return
}

# ---------------------------------------------------------------------------
# COLLATE mode - elect one leader, read the live selection, hand off to RUN.
# ---------------------------------------------------------------------------
if (-not $Paths -or $Paths.Count -eq 0) { return }
$clicked = $Paths[0]

New-Item -ItemType Directory -Force -Path $workDir | Out-Null

$mutex = Get-PixMutex
$iAmLeader = $false
[void] $mutex.WaitOne()
try {
    $fresh = $false
    if (Test-Path -LiteralPath $leader) {
        $age = ((Get-Date) - (Get-Item -LiteralPath $leader).LastWriteTime).TotalSeconds
        if ($age -lt $LeaderStaleSeconds) { $fresh = $true }
    }
    if (-not $fresh) {
        Set-Content -LiteralPath $leader -Value ([string](Get-Date).Ticks) -Encoding UTF8
        $iAmLeader = $true
    }
}
finally {
    $mutex.ReleaseMutex()
    $mutex.Dispose()
}

if (-not $iAmLeader) { return }   # sibling invocation from the same selection
Write-PixLog "COLLATE leader Op=$Op Tag=$Tag Deg=$Deg clicked=$clicked"

$items = @(Get-ExplorerSelection -Clicked $clicked | Where-Object { $_ -ne '' })
Write-PixLog "selection read: $($items.Count) item(s)"

$itemsFile = Join-Path $workDir ("items-{0}.txt" -f ([guid]::NewGuid().ToString('N')))
Set-Content -LiteralPath $itemsFile -Value $items -Encoding UTF8

# Hand off to the visible RUN stage, carrying the menu's tag + operation.
# PIXTAG_COLLATE_ONLY is a test seam: skip the relaunch and report the file.
if ($env:PIXTAG_COLLATE_ONLY) {
    Write-Output $itemsFile
    return
}
$psExe = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
Write-PixLog "spawning RUN window: $psExe -File $PSCommandPath -Run $itemsFile"
# Build the arg list incrementally: `-Deg` is only meaningful for rotate, and
# Windows PowerShell 5.1's `Start-Process -ArgumentList` REJECTS an empty-string
# element ("The argument is null or empty"). Passing `-Deg ''` for set/clear/meta
# therefore threw here and killed the leader before the RUN window spawned. Only
# append `-Deg` when it carries a value.
$runArgs = @(
    '-NoProfile', '-ExecutionPolicy', 'Bypass',
    '-File', $PSCommandPath, '-Run', $itemsFile, '-Tag', $Tag, '-Op', $Op
)
if ($Deg) { $runArgs += @('-Deg', $Deg) }
Start-Process -FilePath $psExe -ArgumentList $runArgs
Write-PixLog "RUN window spawned"
