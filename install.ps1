#requires -Version 5.1
<#
.SYNOPSIS
    Installs Nexus Agent on Windows from an immutable source revision.

.DESCRIPTION
    Creates an isolated virtual environment, installs the pinned Nexus commit,
    and puts a nexus.exe launcher on the user's PATH. No API keys are read or
    changed. Download this script from the commit-pinned raw.githubusercontent.com
    URL documented in README.md.

    Optional environment overrides:
      NEXUS_INSTALL_DIR  Virtual-environment parent (default: %LOCALAPPDATA%\Nexus-Agent)
      NEXUS_BIN_DIR      Launcher directory (default: %USERPROFILE%\.local\bin)
#>

[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# Do not replace this with a branch name. A full commit hash cannot move like
# "main" or a release tag can, so every run installs the reviewed source.
$NexusCommit = '83dafca49d8e66f8c77ed75365cb5a9fba3c152f'
if ($NexusCommit -notmatch '^[0-9a-f]{40}$') {
    throw 'NexusCommit must be a full, lowercase Git commit hash.'
}
$PackageSpec = "git+https://github.com/BFH-HAMID/Nexus-Agent.git@$NexusCommit" + '#egg=nexus-agent'

if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    throw 'Git is required. Install Git for Windows from https://git-scm.com/download/win and try again.'
}

# Find a real Python 3.10+ interpreter. The Microsoft Store alias and older
# versions fail this probe and are skipped.
$PythonExe = $null
$PythonPrefix = @()
$PythonVersion = $null
$PythonCandidates = @(
    @{ Exe = 'py'; Prefix = @('-3') },
    @{ Exe = 'python'; Prefix = @() },
    @{ Exe = 'python3'; Prefix = @() }
)
$VersionProbe = 'import sys; print(".".join(map(str, sys.version_info[:3]))); sys.exit(0 if sys.version_info >= (3, 10) else 1)'

foreach ($Candidate in $PythonCandidates) {
    if (-not (Get-Command $Candidate.Exe -ErrorAction SilentlyContinue)) {
        continue
    }

    try {
        $ProbeArgs = @($Candidate.Prefix) + @('-c', $VersionProbe)
        $ProbeOutput = & $Candidate.Exe @ProbeArgs 2>$null
        if ($LASTEXITCODE -eq 0 -and $ProbeOutput) {
            $PythonExe = $Candidate.Exe
            $PythonPrefix = @($Candidate.Prefix)
            $PythonVersion = ($ProbeOutput | Select-Object -Last 1).ToString().Trim()
            break
        }
    }
    catch {
        # Try the next interpreter candidate.
    }
}

if (-not $PythonExe) {
    throw 'Python 3.10 or newer is required. Install it from https://www.python.org/downloads/windows/ and try again.'
}

if ($env:NEXUS_INSTALL_DIR) {
    $InstallRoot = $env:NEXUS_INSTALL_DIR
}
elseif ($env:LOCALAPPDATA) {
    $InstallRoot = Join-Path $env:LOCALAPPDATA 'Nexus-Agent'
}
else {
    $InstallRoot = Join-Path $HOME '.local\share\Nexus-Agent'
}

if ($env:NEXUS_BIN_DIR) {
    $BinDir = $env:NEXUS_BIN_DIR
}
else {
    $BinDir = Join-Path $HOME '.local\bin'
}

$InstallRoot = [System.IO.Path]::GetFullPath($InstallRoot)
$BinDir = [System.IO.Path]::GetFullPath($BinDir)
if ($BinDir.Contains(';')) {
    throw 'The launcher directory cannot contain a semicolon.'
}

$VenvDir = Join-Path $InstallRoot 'venv'
$SourceRoot = Join-Path $InstallRoot 'src'
$SourceCheckout = Join-Path $SourceRoot 'nexus-agent'
$VenvScripts = Join-Path $VenvDir 'Scripts'
$VenvPython = Join-Path $VenvScripts 'python.exe'
$VenvNexus = Join-Path $VenvScripts 'nexus.exe'
$Launcher = Join-Path $BinDir 'nexus.exe'

Write-Host "Installing Nexus Agent at commit $NexusCommit" -ForegroundColor Cyan
Write-Host "Using Python $PythonVersion; virtual environment: $VenvDir"

New-Item -ItemType Directory -Path $InstallRoot -Force | Out-Null
if (-not (Test-Path -LiteralPath $VenvPython -PathType Leaf)) {
    $VenvArgs = @($PythonPrefix) + @('-m', 'venv', $VenvDir)
    & $PythonExe @VenvArgs
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to create the virtual environment (exit code $LASTEXITCODE)."
    }
}

# Editable VCS installation keeps the pinned checkout beside the virtualenv and
# uses the same PEP 660 installation path as the repository's documented setup.
$PipArgs = @(
    '-m', 'pip', 'install', '--disable-pip-version-check', '--upgrade',
    '--src', $SourceRoot, '--editable', $PackageSpec
)
& $VenvPython @PipArgs
if ($LASTEXITCODE -ne 0) {
    throw "Nexus installation failed (exit code $LASTEXITCODE)."
}

$SourceCommit = (& git -C $SourceCheckout rev-parse HEAD).Trim()
if ($LASTEXITCODE -ne 0 -or $SourceCommit -ne $NexusCommit) {
    throw "Source verification failed: expected $NexusCommit, got $SourceCommit."
}
if (-not (Test-Path -LiteralPath $VenvNexus -PathType Leaf)) {
    throw "Installation finished without creating the expected launcher: $VenvNexus"
}

New-Item -ItemType Directory -Path $BinDir -Force | Out-Null
Copy-Item -LiteralPath $VenvNexus -Destination $Launcher -Force

# Add the launcher directory to the user PATH once, and make it available in
# the current terminal too. No system-wide PATH or administrator rights needed.
$UserPath = [System.Environment]::GetEnvironmentVariable('Path', 'User')
$UserPathEntries = @($UserPath -split ';' | Where-Object { $_ })
$PathWasChanged = $false
if ($UserPathEntries -notcontains $BinDir) {
    $NewUserPath = (@($UserPathEntries) + $BinDir) -join ';'
    [System.Environment]::SetEnvironmentVariable('Path', $NewUserPath, 'User')
    $PathWasChanged = $true
}
if (@($env:Path -split ';') -notcontains $BinDir) {
    $env:Path = "$BinDir;$env:Path"
}

& $Launcher version
if ($LASTEXITCODE -ne 0) {
    throw "The installed Nexus launcher failed its smoke test (exit code $LASTEXITCODE)."
}

Write-Host "`nNexus Agent installed successfully." -ForegroundColor Green
if ($PathWasChanged) {
    Write-Host 'Open a new terminal so other applications can see the updated PATH.'
}
Write-Host 'Run: nexus'
Write-Host 'Optional first API key: nexus keys set gemini'
