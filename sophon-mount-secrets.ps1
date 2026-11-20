<#
.SYNOPSIS
  Upload and hot-mount artifacts\secrets\sophon-secrets.iso onto a Sophon InfraVM, then remove the local ISO copy.
.EXAMPLE
  .\sophon-mount-secrets.ps1
  .\sophon-mount-secrets.ps1 --vm-name sophon-dev-infravm
#>
param([Parameter(ValueFromRemainingArguments)][string[]]$Rest)
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

$command = 'mount-secrets'
$more = @($Rest)
if ($Rest -and $Rest[0] -in '-h', '--help', 'help') {
    $command = '--help'
    $more = @()
} elseif ($Rest -and -not $Rest[0].StartsWith('-')) {
    throw "Unknown argument '$($Rest[0])' (expected options such as --vm-name <name> or --keep-local)."
}

function Load-EnvFile([string]$FilePath) {
    if (-not (Test-Path -LiteralPath $FilePath)) { return }
    foreach ($line in Get-Content -LiteralPath $FilePath) {
        if ($line -cmatch '^\s*([A-Z][A-Z0-9_]*)=(.*)$') {
            $key = $Matches[1]; $value = $Matches[2].Trim()
            if ($value -match '^(["''])(.*)\1$') {
                $q = $Matches[1]; $value = $Matches[2]
                if ($q -eq '"') { $value = $value -replace '\\"', '"' -replace '\\\\', '\' }
            }
            if (($key -like 'SOPHON_*' -or $key -eq 'CF_DNS_API_TOKEN') -and $value -and
                -not [Environment]::GetEnvironmentVariable($key)) {
                [Environment]::SetEnvironmentVariable($key, $value)
            }
        }
    }
}
if ($command -ne '--help') { Load-EnvFile '.env' }

$interactive = [Environment]::UserInteractive -and -not [Console]::IsInputRedirected
if ($command -eq 'mount-secrets' -and $interactive -and -not $env:SOPHON_HYPERVISOR) {
    Write-Host 'Where does the VM run?'
    Write-Host '  1) Proxmox'
    Write-Host '  2) VMware vCenter'
    Write-Host '  3) Microsoft Hyper-V on this PC (runs as you; no password)'
    Write-Host '  4) Microsoft Hyper-V on another host (WinRM)'
    switch (Read-Host 'Choose 1-4') {
        '1' { $env:SOPHON_HYPERVISOR = 'proxmox' }
        '2' { $env:SOPHON_HYPERVISOR = 'vcenter' }
        '3' { $env:SOPHON_HYPERVISOR = 'hyperv'; $env:SOPHON_HYPERV_HOST = '' }
        '4' { $env:SOPHON_HYPERVISOR = 'hyperv' }
        default { throw 'Choose a number from 1 to 4.' }
    }
}

if ($command -eq 'mount-secrets' -and $env:SOPHON_HYPERVISOR -eq 'hyperv' -and -not $env:SOPHON_HYPERV_HOST) {
    $psArgs = @{ AttachCertificates = $true }
    for ($i = 0; $i -lt $more.Count; $i++) {
        switch ($more[$i]) {
            '--vm-name' { $i++; $psArgs['Name'] = $more[$i] }
            '--secrets-iso' { $i++; $psArgs['CertificatesIso'] = $more[$i] }
            '--keep-local' { $psArgs['KeepLocal'] = $true }
            default { throw "Unsupported local Hyper-V option: $($more[$i])" }
        }
    }
    & "$PSScriptRoot\2-provision-hyperv-local.ps1" @psArgs
    exit $LASTEXITCODE
}

$names = @(Get-ChildItem Env: | Where-Object { $_.Name -match '^(SOPHON_|CF_DNS_API_TOKEN$)' } | ForEach-Object Name)
$engine = @('docker', 'podman') | Where-Object { Get-Command $_ -ErrorAction SilentlyContinue } | Select-Object -First 1

if (-not $engine) {
    if (-not (Get-Command wsl -ErrorAction SilentlyContinue)) { throw 'Docker, Podman or WSL (with one of them) is required.' }
    if ($names) { $env:WSLENV = (@($env:WSLENV -split ':' | Where-Object { $_ }) + $names | Select-Object -Unique) -join ':' }
    if ($command -eq '--help') { wsl --cd $PSScriptRoot -- sh ./sophon-mount-secrets.sh --help }
    else { wsl --cd $PSScriptRoot -- sh ./sophon-mount-secrets.sh @more }
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

$run = @('run', '--rm', '--init', '-e', 'PYTHONUNBUFFERED=1', '-e', 'SOPHON_LOAD_DOTENV=1', '-e', 'SOPHON_ENTRYPOINT=mount-secrets')
if (-not [Console]::IsInputRedirected -and -not [Console]::IsOutputRedirected) { $run += '-it' }
$run += '-v', "${PSScriptRoot}:/workspace", '-v', "${socket}:/var/run/docker.sock", '-v', 'sophon-state:/state'
foreach ($name in $names) { $run += '-e', $name }
& $engine @run sophon-tools $command @more
exit $LASTEXITCODE
