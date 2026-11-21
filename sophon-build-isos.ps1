<#
.SYNOPSIS
  Build Sophon OS, app, SFTP server, and unattended secrets ISOs on Windows (Docker, Podman, or WSL).
.EXAMPLE
  .\sophon-build-isos.ps1                          # build mode-0600 secrets ISOs + CoreOS/apps/SFTP ISOs
  .\sophon-build-isos.ps1 --os-only                # build only secret-free CoreOS/apps/SFTP ISOs
  .\sophon-build-isos.ps1 --secrets-only           # build only mode-0600 secrets ISOs
  .\sophon-build-isos.ps1 certificates issue       # air-gapped 90-day wildcard certificate renewal
#>
param([Parameter(ValueFromRemainingArguments)][string[]]$Rest)
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

$command = 'build-isos'
$more = @($Rest)
if ($Rest -and -not $Rest[0].StartsWith('-')) {
    if ($Rest[0] -notin 'build-isos', 'prepare', 'secrets', 'certificates', 'help') {
        throw "Unknown command '$($Rest[0])' (use .\sophon-provision.ps1 for VM provisioning commands)."
    }
    $command = $Rest[0]
    $more = @($Rest | Select-Object -Skip 1)
} elseif ($Rest -and $Rest[0] -in '-h', '--help') {
    $command = '--help'
    $more = @()
}

$names = @(Get-ChildItem Env: | Where-Object { $_.Name -match '^(SOPHON_|CF_DNS_API_TOKEN$)' } | ForEach-Object Name)
$engine = @('docker', 'podman') | Where-Object { Get-Command $_ -ErrorAction SilentlyContinue } | Select-Object -First 1
if (-not $engine) {
    if (-not (Get-Command wsl -ErrorAction SilentlyContinue)) { throw 'Docker, Podman or WSL (with one of them) is required.' }
    if ($names) { $env:WSLENV = (@($env:WSLENV -split ':' | Where-Object { $_ }) + $names | Select-Object -Unique) -join ':' }
    wsl --cd $PSScriptRoot -- sh ./sophon-build-isos.sh $command @more
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

$run = @('run', '--rm', '--init', '-e', 'PYTHONUNBUFFERED=1', '-e', 'SOPHON_LOAD_DOTENV=1', '-e', 'SOPHON_ENTRYPOINT=build-isos')
if (-not [Console]::IsInputRedirected -and -not [Console]::IsOutputRedirected) { $run += '-it' }
$run += '-v', "${PSScriptRoot}:/workspace", '-v', "${socket}:/var/run/docker.sock", '-v', 'sophon-state:/state'
foreach ($name in $names) { $run += '-e', $name }
& $engine @run sophon-tools $command @more
exit $LASTEXITCODE
