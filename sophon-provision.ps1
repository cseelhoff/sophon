<#
.SYNOPSIS
  Runs a sophon-tools command on Windows with whichever engine is available (Docker, Podman or Podman/Docker in WSL).
.EXAMPLE
  .\sophon-provision.ps1                 # steps 1+2 (default command: provision)
  .\sophon-provision.ps1 prepare         # step 1 only
.NOTES
  With no SOPHON_HYPERVISOR set it asks where InfraVM should run (or to only download and build, then exit), and
  asks for any missing required setting. Every SOPHON_* variable (and CF_DNS_API_TOKEN) is passed by name.
  For Hyper-V on this machine (SOPHON_HYPERVISOR=hyperv with no SOPHON_HYPERV_HOST) it runs step 1 in the container,
  then 2-provision-hyperv-local.ps1 as the signed-in user, so no WinRM or credentials are needed.
#>
param([Parameter(ValueFromRemainingArguments)][string[]]$Rest)
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
$command = if ($Rest) { $Rest[0] } else { 'provision' }
$more = @($Rest | Select-Object -Skip 1)

# Load SOPHON_* settings from .env (see .env.example); variables already set in the shell win, empty values are skipped.
if (Test-Path -LiteralPath .env) {
    foreach ($line in Get-Content -LiteralPath .env) {
        if ($line -cmatch '^\s*([A-Z][A-Z0-9_]*)=(.*)$') {
            $key = $Matches[1]; $value = $Matches[2].Trim()
            if ($value -match '^(["''])(.*)\1$') { $value = $Matches[2] }
            if (($key -like 'SOPHON_*' -or $key -eq 'CF_DNS_API_TOKEN') -and $value -and
                -not [Environment]::GetEnvironmentVariable($key)) {
                [Environment]::SetEnvironmentVariable($key, $value)
            }
        }
    }
}

$interactive = [Environment]::UserInteractive -and -not [Console]::IsInputRedirected

function Need([string]$Name, [string]$Prompt, [string]$Default = '') {
    if ([Environment]::GetEnvironmentVariable($Name)) { return }
    $answer = Read-Host ($Prompt + $(if ($Default) { " [$Default]" }))
    if (-not $answer) { $answer = $Default }
    if (-not $answer) { throw "$Prompt is required." }
    [Environment]::SetEnvironmentVariable($Name, $answer)
}
function NeedSecret([string]$Name) {
    if ([Environment]::GetEnvironmentVariable($Name)) { return }
    $secure = Read-Host $Name -AsSecureString
    $value = [pscredential]::new('x', $secure).GetNetworkCredential().Password
    if (-not $value) { throw "$Name is required." }
    [Environment]::SetEnvironmentVariable($Name, $value)
}

# Ask where InfraVM should run unless SOPHON_HYPERVISOR already says (automation sets it).
if ($command -eq 'provision' -and $interactive -and -not $env:SOPHON_HYPERVISOR) {
    Write-Host 'Where should InfraVM run?'
    Write-Host '  1) Proxmox'
    Write-Host '  2) VMware vCenter'
    Write-Host '  3) Microsoft Hyper-V on this PC (runs as you; no password)'
    Write-Host '  4) Microsoft Hyper-V on another host (WinRM)'
    Write-Host '  5) Nowhere yet: only download and build the installation media (step 1), then exit'
    switch (Read-Host 'Choose 1-5') {
        '1' { $env:SOPHON_HYPERVISOR = 'proxmox' }
        '2' { $env:SOPHON_HYPERVISOR = 'vcenter' }
        '3' { $env:SOPHON_HYPERVISOR = 'hyperv'; $env:SOPHON_HYPERV_HOST = '' }
        '4' { $env:SOPHON_HYPERVISOR = 'hyperv'; Need 'SOPHON_HYPERV_HOST' 'Hyper-V host name or address' }
        '5' { $command = 'prepare' }
        default { throw 'Choose a number from 1 to 5.' }
    }
}

$hypervisor = if ($env:SOPHON_HYPERVISOR) { $env:SOPHON_HYPERVISOR } else { 'proxmox' }
$localHyperV = $hypervisor -eq 'hyperv' -and (-not $env:SOPHON_HYPERV_HOST -or
    $env:SOPHON_HYPERV_HOST -in 'localhost', '127.0.0.1', $env:COMPUTERNAME)

if ($command -eq 'provision' -and $interactive -and -not $localHyperV) {
    switch ($hypervisor) {
        'vcenter' {
            Need 'SOPHON_VCENTER_URL' 'vCenter URL (https://...)'
            Need 'SOPHON_VCENTER_USERNAME' 'vCenter username' 'administrator@vsphere.local'
            Need 'SOPHON_VCENTER_DATACENTER' 'Datacenter'
            Need 'SOPHON_VCENTER_CLUSTER' 'Cluster'
            Need 'SOPHON_VCENTER_DATASTORE' 'Datastore'
            Need 'SOPHON_VCENTER_NETWORK' 'Port group'
            NeedSecret 'SOPHON_VCENTER_PASSWORD'
        }
        'hyperv' {
            Need 'SOPHON_HYPERV_HOST' 'Hyper-V host name or address'
            Need 'SOPHON_HYPERV_USERNAME' 'Hyper-V username' 'Administrator'
            Need 'SOPHON_HYPERV_SWITCH' 'Virtual switch'
            NeedSecret 'SOPHON_HYPERV_PASSWORD'
        }
        default {
            Need 'SOPHON_PROXMOX_HOST' 'Proxmox host name or address'
            Need 'SOPHON_PROXMOX_NODE' 'Proxmox node' 'proxmox'
            NeedSecret 'SOPHON_PROXMOX_PASSWORD'
        }
    }
}

$names = @(Get-ChildItem Env: | Where-Object { $_.Name -match '^(SOPHON_|CF_DNS_API_TOKEN$)' } | ForEach-Object Name)

$engine = @('docker', 'podman') | Where-Object { Get-Command $_ -ErrorAction SilentlyContinue } | Select-Object -First 1
if ($localHyperV -and $command -eq 'provision') {
    & $PSCommandPath prepare
    if ($LASTEXITCODE) { exit $LASTEXITCODE }
    & "$PSScriptRoot\2-provision-hyperv-local.ps1"
    return
}

if (-not $engine) {
    if (-not (Get-Command wsl -ErrorAction SilentlyContinue)) { throw 'Docker, Podman or WSL (with one of them) is required.' }
    # Run the bash wrapper inside WSL; WSLENV carries the settings by name.
    $env:WSLENV = (@($env:WSLENV -split ':' | Where-Object { $_ }) + $names | Select-Object -Unique) -join ':'
    wsl --cd $PSScriptRoot -- bash ./sophon-provision.sh $command @more
    exit $LASTEXITCODE
}

$socket = (& $engine info --format '{{.Host.RemoteSocket.Path}}' 2>$null)
if ($LASTEXITCODE -eq 0 -and $socket) {
    $socket = $socket -replace '^unix://', ''
    if ($socket -notmatch '^/') { $socket = '/run/user/1000/podman/podman.sock' }
} else {
    $socket = '/var/run/docker.sock'
}
& $engine image inspect sophon-tools *> $null
if ($LASTEXITCODE) { & $engine build -t sophon-tools -f Containerfile .; if ($LASTEXITCODE) { exit $LASTEXITCODE } }

$run = @('run', '--rm', '--init')
if (-not [Console]::IsInputRedirected -and -not [Console]::IsOutputRedirected) { $run += '-it' }
$run += '-v', "${PSScriptRoot}:/workspace", '-v', "${socket}:/var/run/docker.sock", '-v', 'sophon-state:/state'
foreach ($name in $names) { $run += '-e', $name }
& $engine @run sophon-tools $command @more
exit $LASTEXITCODE
