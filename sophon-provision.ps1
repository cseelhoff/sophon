<#
.SYNOPSIS
  Provision or manage Sophon VMs on Proxmox, VMware vCenter, or Microsoft Hyper-V.
.EXAMPLE
  .\sophon-provision.ps1                 # deploy SFTP Backup VM (if paired) + production InfraVM
  .\sophon-provision.ps1 --recreate      # replace production InfraVM in place (safe standby until sophon-activate)
  .\sophon-provision.ps1 dev             # recreate & restore dev/drill InfraVM on SOPHON_DEV_NETWORK_CIDR
  .\sophon-provision.ps1 backup          # deploy only the standalone Alpine SFTP Backup Server VM
  .\sophon-provision.ps1 destroy-vm      # stop and delete a Sophon-managed VM
  .\sophon-provision.ps1 attach-certificates   # local Hyper-V: insert sophon-secrets.iso (detach-certificates ejects it)
#>
param([Parameter(ValueFromRemainingArguments)][string[]]$Rest)
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

$command = 'provision'
$more = @($Rest)
if ($Rest -and $Rest[0] -in '-h', '--help') {
    $command = '--help'
    $more = @()
} elseif ($Rest -and -not $Rest[0].StartsWith('-')) {
    if ($Rest[0] -in 'build-isos', 'prepare', 'secrets', 'certificates') {
        $sub = if ($Rest[0] -eq 'build-isos') { '' } else { " $($Rest[0])" }
        throw "ISO/secrets commands live in sophon-build-isos.ps1 — run: .\sophon-build-isos.ps1$sub"
    }
    $command = switch ($Rest[0]) {
        'dev' { 'provision-dev' }
        'backup' { 'provision-backup' }
        default { $Rest[0] }
    }
    if ($command -notin 'provision', 'provision-dev', 'provision-backup', 'destroy-vm', 'verify-restore',
                        'attach-certificates', 'detach-certificates', 'test', 'shell', 'help') {
        throw "Unknown command '$($Rest[0])' (expected dev, backup, destroy-vm, or --recreate)."
    }
    $more = @($Rest | Select-Object -Skip 1)
}

function Load-EnvFile([string]$FilePath) {
    if (-not (Test-Path -LiteralPath $FilePath)) { return }
    foreach ($line in Get-Content -LiteralPath $FilePath) {
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
if ($command -notin 'test', 'shell', '--help', 'help') { Load-EnvFile '.env' }

$interactive = [Environment]::UserInteractive -and -not [Console]::IsInputRedirected
if ($command -in 'provision', 'provision-dev', 'provision-backup', 'destroy-vm', 'verify-restore' -and $interactive -and -not $env:SOPHON_HYPERVISOR) {
    Write-Host 'Where should the VM run?'
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

$hypervisor = if ($env:SOPHON_HYPERVISOR) { $env:SOPHON_HYPERVISOR } else { 'proxmox' }
$localHyperV = $hypervisor -eq 'hyperv' -and (-not $env:SOPHON_HYPERV_HOST -or
    $env:SOPHON_HYPERV_HOST -in 'localhost', '127.0.0.1', $env:COMPUTERNAME)

if ($command -in 'attach-certificates', 'detach-certificates') {
    if ($command -eq 'attach-certificates') { & "$PSScriptRoot\2-provision-hyperv-local.ps1" -AttachCertificates }
    else { & "$PSScriptRoot\2-provision-hyperv-local.ps1" -DetachCertificates }
    return
}

$names = @(Get-ChildItem Env: | Where-Object { $_.Name -match '^(SOPHON_|CF_DNS_API_TOKEN$)' } | ForEach-Object Name)
$engine = @('docker', 'podman') | Where-Object { Get-Command $_ -ErrorAction SilentlyContinue } | Select-Object -First 1

if ($localHyperV -and $command -in 'provision', 'provision-dev') {
    foreach ($iso in 'artifacts\coreos\sophon-coreos.iso', 'artifacts\bootstrap\sophon-apps.iso') {
        if (-not (Test-Path -LiteralPath (Join-Path $PSScriptRoot $iso) -PathType Leaf)) {
            throw "Missing installation ISO: $iso (run .\sophon-build-isos.ps1 first)."
        }
    }
    if ($command -eq 'provision-dev') {
        if (-not $engine) { throw 'Docker or Podman is required to derive the dev secrets ISO.' }
        $devName = if ($env:SOPHON_DEV_INFRAVM_NAME) { $env:SOPHON_DEV_INFRAVM_NAME } else { 'sophon-dev-infravm' }
        $srcIso = if ($env:SOPHON_SECRETS_ISO) { $env:SOPHON_SECRETS_ISO } else { 'artifacts/secrets/sophon-secrets.iso' }
        $devIsoRel = 'artifacts/secrets/.tmp-sophon-dev-secrets.iso'
        $devIsoAbs = Join-Path $PSScriptRoot 'artifacts\secrets\.tmp-sophon-dev-secrets.iso'
        $drvRun = @('run', '--rm', '-e', 'SOPHON_LOAD_DOTENV=1', '-v', "${PSScriptRoot}:/workspace")
        foreach ($name in $names) { $drvRun += '-e', $name }
        & $engine @drvRun sophon-tools secrets --output-iso $srcIso --derive-dev-iso $devIsoRel
        if ($LASTEXITCODE) { exit $LASTEXITCODE }
        try {
            & "$PSScriptRoot\2-provision-hyperv-local.ps1" -Name $devName -Recreate -CertificatesIso $devIsoAbs
        } finally {
            Remove-Item -LiteralPath $devIsoAbs -Force -ErrorAction SilentlyContinue
        }
        return
    }
    $hvArgs = @()
    if ($more -contains '--recreate') { $hvArgs += '-Recreate' }
    & "$PSScriptRoot\2-provision-hyperv-local.ps1" @hvArgs
    return
}

if (-not $engine) {
    if (-not (Get-Command wsl -ErrorAction SilentlyContinue)) { throw 'Docker, Podman or WSL (with one of them) is required.' }
    if ($names) { $env:WSLENV = (@($env:WSLENV -split ':' | Where-Object { $_ }) + $names | Select-Object -Unique) -join ':' }
    wsl --cd $PSScriptRoot -- sh ./sophon-provision.sh $command @more
    exit $LASTEXITCODE
}

$extraMounts = @()
if ($command -in 'provision-dev', 'verify-restore' -and $env:SOPHON_VERIFY_RESTORE_CA_FILE) {
    $caAbs = (Resolve-Path -LiteralPath $env:SOPHON_VERIFY_RESTORE_CA_FILE).Path
    $extraMounts += '-v', "${caAbs}:/tmp/sophon-verify-ca.pem:ro"
    $env:SOPHON_VERIFY_RESTORE_CA_FILE = '/tmp/sophon-verify-ca.pem'
    if ('SOPHON_VERIFY_RESTORE_CA_FILE' -notin $names) { $names += 'SOPHON_VERIFY_RESTORE_CA_FILE' }
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

$run = @('run', '--rm', '--init', '-e', 'PYTHONUNBUFFERED=1', '-e', 'SOPHON_LOAD_DOTENV=1', '-e', 'SOPHON_ENTRYPOINT=provision')
if (-not [Console]::IsInputRedirected -and -not [Console]::IsOutputRedirected) { $run += '-it' }
$run += '-v', "${PSScriptRoot}:/workspace", '-v', "${socket}:/var/run/docker.sock", '-v', 'sophon-state:/state'
$run += $extraMounts
foreach ($name in $names) { $run += '-e', $name }
& $engine @run sophon-tools $command @more
exit $LASTEXITCODE
