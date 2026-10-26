<#
.SYNOPSIS
Cyberpunk Nexus terminal. Boot telemetry is explicitly simulated; tasks use Nexus.
.EXAMPLE
pwsh -File ./scripts/nexus-core.ps1
.EXAMPLE
pwsh -File ./scripts/nexus-core.ps1 -Demo -NoAnimation
#>
[CmdletBinding()]
param([switch]$Demo, [switch]$NoAnimation)

$escChar = [char]27
$green = "$escChar[92m"
$cyan = "$escChar[96m"
$amber = "$escChar[93m"
$reset = "$escChar[0m"
$oldBackground = [Console]::BackgroundColor
$oldForeground = [Console]::ForegroundColor
$oldTitle = [Console]::Title
$interactive = -not [Console]::IsOutputRedirected
$animate = $interactive -and -not $NoAnimation -and -not $env:NO_COLOR
if ($env:NO_COLOR -or -not $interactive) {
    $green = ''; $cyan = ''; $amber = ''; $reset = ''
}
try {
    if ($interactive) {
        [Console]::BackgroundColor = 'Black'
        [Console]::ForegroundColor = 'Green'
        [Console]::Title = 'NEXUS CORE AI NET'
        Clear-Host
    }
    Write-Host "${cyan}============================================================${reset}"
    Write-Host "${green} NEXUS CORE AI NET // PERSONAL AGENT TERMINAL${reset}"
    Write-Host "${amber} VISUAL SIMULATION: 1000 AGENTS ONLINE (not live health)${reset}"
    Write-Host "${cyan}============================================================${reset}"
    $glyphs = @(
        @('10001','11001','10101','10011','10001'),
        @('11111','10000','11110','10000','11111'),
        @('10001','01010','00100','01010','10001'),
        @('10001','10001','10001','10001','01110'),
        @('01111','10000','01110','00001','11110')
    )
    for ($row = 0; $row -lt 5; $row++) {
        $line = ''
        foreach ($glyph in $glyphs) {
            foreach ($bit in $glyph[$row].ToCharArray()) {
                if ($bit -eq '1') { $line += '10' } else { $line += '  ' }
            }
            $line += '  '
        }
        Write-Host "${green}$line${reset}"
    }
    Write-Host "${cyan} [SIM] AGENT_SYNC [1000/1000] OK"
    Write-Host ' [SIM] PATHWAY_OPTIMIZATION [7.8M/sec]'
    Write-Host " [SIM] DEEP_LEARNING_MATRIX: ENABLED${reset}"
    foreach ($percent in @(0, 10, 25, 40, 55, 70, 85, 100)) {
        $filled = [int]($percent / 5)
        $bar = ('1' * $filled) + ('-' * (20 - $filled))
        if ($animate) {
            Write-Host -NoNewline "`r${green} CORE_BOOT [$bar] $percent%${reset}"
            Start-Sleep -Milliseconds 65
        }
    }
    if ($animate) { Write-Host '' }
    else { Write-Host "${green} CORE_BOOT [11111111111111111111] 100%${reset}" }
    Write-Host "${amber} /exit to quit | /help for commands | tasks run via nexus${reset}"
    if ($Demo) { Write-Host ' DEMO: tasks are scripted offline, no real model calls.' }
    while ($true) {
        Write-Host -NoNewline "${cyan}[NEXUS_CORE@SYSTEM:~]`$ ${reset}"
        $taskText = [Console]::ReadLine()
        if ($null -eq $taskText -or $taskText -in @('/exit', '/quit')) { break }
        if ([string]::IsNullOrWhiteSpace($taskText)) { continue }
        if ($taskText -eq '/help') {
            Write-Host 'Enter a task. /gui opens the dashboard, /doctor checks models, /exit quits.'
            continue
        }
        if (-not (Get-Command nexus -ErrorAction SilentlyContinue)) {
            Write-Host "${amber}Nexus not found. Activate your Nexus venv or install: pip install -e .${reset}"
            continue
        }
        # Invoke with arguments, never evaluate input as PowerShell code.
        if ($taskText -eq '/gui') { & nexus gui; continue }
        if ($taskText -eq '/doctor') { & nexus doctor; continue }
        if ($Demo) { & nexus task $taskText --demo }
        else { & nexus task $taskText }
    }
}
finally {
    if ($interactive) {
        Write-Host -NoNewline $reset
        [Console]::BackgroundColor = $oldBackground
        [Console]::ForegroundColor = $oldForeground
        [Console]::Title = $oldTitle
    }
}
