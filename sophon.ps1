param(
    [Parameter(Position = 0)]
    [string] $Command = 'help',
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]] $CommandArgs = @()
)

$ErrorActionPreference = 'Stop'

function Show-Help {
    @'
Sophon Windows launcher (requires Docker Desktop with Linux containers)

  .\sophon.ps1 prepare
  .\sophon.ps1 provision
  .\sophon.ps1 dashboard [port]
  .\sophon.ps1 certificates issue [...]
  .\sophon.ps1 test | shell | <any command>
'@
}

if ($Command -in @('help', '-h', '--help')) {
    Show-Help
    exit 0
}

Push-Location $PSScriptRoot
try {
    $engineName = if ($env:SOPHON_ENGINE) { $env:SOPHON_ENGINE } else { 'docker' }
    $engineCommand = Get-Command $engineName -ErrorAction SilentlyContinue
    if (-not $engineCommand) {
        Write-Error 'Install Docker Desktop and make sure docker.exe is on PATH.'
        exit 1
    }
    $engine = $engineCommand.Source
    $image = 'sophon-tools:latest'
    $containerfileHash = (Get-FileHash -Algorithm SHA256 Containerfile).Hash.Substring(0, 16).ToLowerInvariant()
    $existingHash = & $engine image inspect --format '{{index .Config.Labels "sophon.containerfile"}}' $image 2>$null
    if ($LASTEXITCODE -ne 0 -or $existingHash.Trim() -ne $containerfileHash) {
        Write-Host 'Building the Sophon tools image (needs internet once)...' -ForegroundColor Cyan
        & $engine build --target tools --label "sophon.containerfile=$containerfileHash" -t $image -f Containerfile .
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    }

    $project = (Get-Location).Path
    $stateRoot = if ($env:LOCALAPPDATA) { $env:LOCALAPPDATA } else { Join-Path $HOME 'AppData\Local' }
    $state = Join-Path $stateRoot 'Sophon'
    New-Item -ItemType Directory -Force -Path $state | Out-Null

    $runArgs = @(
        'run', '--rm', '--init',
        '--mount', "type=bind,source=$project,target=/workspace",
        '--workdir', '/workspace',
        '--mount', "type=bind,source=$state,target=/state",
        '--env', 'SOPHON_STATE_DIR=/state',
        '--env', "SOPHON_PROJECT_DIR=$project",
        '--mount', 'type=bind,source=/var/run/docker.sock,target=/var/run/docker.sock',
        '--env', 'DOCKER_HOST=unix:///var/run/docker.sock'
    )

    foreach ($entry in [Environment]::GetEnvironmentVariables('Process').GetEnumerator()) {
        if (($entry.Key -match '^(SOPHON_|CF_DNS_API_TOKEN$)') -and
            $entry.Key -notin @('SOPHON_STATE_DIR', 'SOPHON_PROJECT_DIR')) {
            $runArgs += @('--env', "$($entry.Key)=$($entry.Value)")
        }
    }

    switch ($Command) {
        'prepare' { $containerCommand = @('ansible-playbook', '1-prepare.yml') + $CommandArgs }
        'provision' { $containerCommand = @('python3', 'src/deployment.py', '--step', 'provision') + $CommandArgs + @('--', '2-provision.yml') }
        'dashboard' {
            $port = if ($CommandArgs.Count -gt 0) { $CommandArgs[0] } else { '41327' }
            if ($port -notmatch '^\d+$' -or [int]$port -lt 1 -or [int]$port -gt 65535) {
                Write-Error 'Dashboard port must be an integer from 1 to 65535.'
                exit 2
            }
            $runArgs += @('--publish', "127.0.0.1:${port}:${port}", '--env', 'SOPHON_DASHBOARD_HOST=0.0.0.0')
            $containerCommand = @('python3', 'src/deployment.py', '--step', 'provision', '--serve', '--port', $port)
        }
        'certificates' { $containerCommand = @('python3', 'src/certificates.py') + $CommandArgs }
        'test' { $containerCommand = @('sh', '-c', 'python3 -m unittest discover -s tests "$@" && python3 tests/test_deployment.py >/dev/null && python3 tests/test_steps.py', 'sh') + $CommandArgs }
        'shell' { $containerCommand = @('bash') }
        default { $containerCommand = @($Command) + $CommandArgs }
    }

    if ([Console]::IsInputRedirected) { $runArgs += '--interactive' } else { $runArgs += @('--interactive', '--tty') }
    $workstationHelper = $null
    if ($Command -in @('provision', 'dashboard')) {
        $helperScript = Join-Path $PSScriptRoot 'guest\workstation-host.ps1'
        $arguments = '-NoProfile -ExecutionPolicy Bypass -File "{0}" -StateDirectory "{1}" -ProjectDirectory "{2}"' -f $helperScript, $state, $PSScriptRoot
        $hostPowerShell = (Get-Process -Id $PID).Path
        $workstationHelper = Start-Process -FilePath $hostPowerShell `
            -ArgumentList $arguments -WindowStyle Hidden -PassThru
    }
    try {
        & $engine @runArgs $image @containerCommand
        $containerExitCode = $LASTEXITCODE
    }
    finally {
        if ($workstationHelper) {
            $bridge = Join-Path $state 'workstation'
            New-Item -ItemType Directory -Force -Path $bridge | Out-Null
            Set-Content -LiteralPath (Join-Path $bridge 'stop') -Value 'stop' -Encoding ascii
            if (-not $workstationHelper.WaitForExit(10000)) {
                Stop-Process -Id $workstationHelper.Id -Force -ErrorAction SilentlyContinue
            }
        }
    }
    exit $containerExitCode
}
finally {
    Pop-Location
}