<#
.SYNOPSIS
  Windows Explorer context-menu launcher for `pix set` / `pix clear` / `pix meta`.

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
    # Common to both stages: which tag and operation the menu leaf chose.
    [ValidateSet('event', 'date')]
    [string] $Tag = 'event',
    [ValidateSet('set', 'clear', 'meta')]
    [string] $Op = 'set'
)

$ErrorActionPreference = 'Stop'

$workDir = Join-Path $env:TEMP 'pixtag'
$leader  = Join-Path $workDir 'leader.lock'

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
    return $fallback
}

function Get-EventValue {
    # Prompt for an event name with type-ahead autocomplete over $Suggestions
    # (existing library events). Returns the entered text, or $null on cancel.
    # Falls back to a plain console prompt where WinForms isn't available.
    param([string[]] $Suggestions)

    try {
        Add-Type -AssemblyName System.Windows.Forms
        Add-Type -AssemblyName System.Drawing
    }
    catch {
        return (Read-Host 'Event name')
    }

    $form = New-Object System.Windows.Forms.Form
    $form.Text = 'pix - set event'
    $form.ClientSize = New-Object System.Drawing.Size(430, 110)
    $form.StartPosition = 'CenterScreen'
    $form.FormBorderStyle = 'FixedDialog'
    $form.MinimizeBox = $false
    $form.MaximizeBox = $false
    $form.TopMost = $true

    $label = New-Object System.Windows.Forms.Label
    $label.Text = 'Event name (type to autocomplete):'
    $label.AutoSize = $true
    $label.Location = New-Object System.Drawing.Point(12, 12)
    $form.Controls.Add($label)

    $box = New-Object System.Windows.Forms.TextBox
    $box.Location = New-Object System.Drawing.Point(12, 36)
    $box.Width = 406
    if ($Suggestions -and $Suggestions.Count -gt 0) {
        $box.AutoCompleteMode = [System.Windows.Forms.AutoCompleteMode]::SuggestAppend
        $box.AutoCompleteSource = [System.Windows.Forms.AutoCompleteSource]::CustomSource
        $col = New-Object System.Windows.Forms.AutoCompleteStringCollection
        $col.AddRange([string[]] $Suggestions)
        $box.AutoCompleteCustomSource = $col
    }
    $form.Controls.Add($box)

    $ok = New-Object System.Windows.Forms.Button
    $ok.Text = 'OK'
    $ok.Location = New-Object System.Drawing.Point(250, 72)
    $ok.DialogResult = [System.Windows.Forms.DialogResult]::OK
    $form.Controls.Add($ok)
    $form.AcceptButton = $ok

    $cancelButton = New-Object System.Windows.Forms.Button
    $cancelButton.Text = 'Cancel'
    $cancelButton.Location = New-Object System.Drawing.Point(337, 72)
    $cancelButton.DialogResult = [System.Windows.Forms.DialogResult]::Cancel
    $form.Controls.Add($cancelButton)
    $form.CancelButton = $cancelButton

    $form.Add_Shown({ $form.Activate(); $box.Focus() })
    $result = $form.ShowDialog()
    if ($result -eq [System.Windows.Forms.DialogResult]::OK) { return $box.Text }
    return $null
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
            # Offer existing library events as type-ahead suggestions.
            $suggestions = @()
            try {
                $suggestions = @(& pix events $items[0] 2>$null | Where-Object { $_ -ne '' })
            }
            catch {}
            $value = Get-EventValue -Suggestions $suggestions
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

$items = @(Get-ExplorerSelection -Clicked $clicked | Where-Object { $_ -ne '' })

$itemsFile = Join-Path $workDir ("items-{0}.txt" -f ([guid]::NewGuid().ToString('N')))
Set-Content -LiteralPath $itemsFile -Value $items -Encoding UTF8

# Hand off to the visible RUN stage, carrying the menu's tag + operation.
# PIXTAG_COLLATE_ONLY is a test seam: skip the relaunch and report the file.
if ($env:PIXTAG_COLLATE_ONLY) {
    Write-Output $itemsFile
    return
}
$psExe = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
Start-Process -FilePath $psExe -ArgumentList @(
    '-NoProfile', '-ExecutionPolicy', 'Bypass',
    '-File', $PSCommandPath, '-Run', $itemsFile, '-Tag', $Tag, '-Op', $Op
)
