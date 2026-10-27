param(
    [Parameter(Mandatory = $true)]
    [string] $StateDirectory,
    [Parameter(Mandatory = $true)]
    [string] $ProjectDirectory
)

$ErrorActionPreference = 'Stop'
$marker = 'sophon-infravm-two-iso-v1'
$bridge = Join-Path $StateDirectory 'workstation'
$requests = Join-Path $bridge 'requests'
$responses = Join-Path $bridge 'responses'
$stopFile = Join-Path $bridge 'stop'
$readyFile = Join-Path $bridge 'ready'
$projectRoot = $ProjectDirectory

New-Item -ItemType Directory -Force -Path $requests, $responses | Out-Null
Remove-Item -Force -ErrorAction SilentlyContinue $stopFile, $readyFile

function Find-WorkstationTool([string] $Name) {
    $command = Get-Command $Name -ErrorAction SilentlyContinue
    if ($command) { return $command.Source }
    $candidates = @(
        (Join-Path $env:ProgramFiles "VMware\VMware Workstation\$Name.exe"),
        (Join-Path ${env:ProgramFiles(x86)} "VMware\VMware Workstation\$Name.exe")
    )
    foreach ($candidate in $candidates) {
        if ($candidate -and (Test-Path -LiteralPath $candidate -PathType Leaf)) { return $candidate }
    }
    throw "VMware Workstation tool not found: $Name.exe. Install VMware Workstation Pro and ensure its tools are available."
}

function Invoke-Workstation([string] $Executable, [string[]] $Arguments) {
    $output = & $Executable @Arguments 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "Workstation command failed ($([IO.Path]::GetFileName($Executable))): $($output -join ' ')"
    }
    return @($output)
}

function Resolve-ProjectFile([string] $RelativePath) {
    if ([IO.Path]::IsPathRooted($RelativePath)) { throw 'ISO paths must be relative to the project directory.' }
    $root = [IO.Path]::GetFullPath($projectRoot).TrimEnd('\') + '\'
    $path = [IO.Path]::GetFullPath((Join-Path $root $RelativePath))
    if (-not $path.StartsWith($root, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'ISO path escapes the project directory.'
    }
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw "Missing ISO: $RelativePath" }
    return $path
}

function Get-RunningVMs([string] $Vmrun) {
    $output = Invoke-Workstation $Vmrun @('list')
    return @($output | Select-Object -Skip 1 | ForEach-Object { $_.Trim() } | Where-Object { $_ })
}

function Invoke-Provision([object] $Request) {
    if ($Request.name -notmatch '^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$') { throw 'Invalid VM name.' }
    if ([int]$Request.cores -lt 1 -or [int]$Request.memory -lt 1024 -or [int]$Request.disk_gb -lt 40) {
        throw 'Provide valid CPU/memory and at least 40 GiB of disk space.'
    }
    if ($Request.network -notin @('bridged', 'nat', 'hostonly')) { throw 'Invalid Workstation network mode.' }
    $vmrun = Find-WorkstationTool 'vmrun'
    $diskManager = Find-WorkstationTool 'vmware-vdiskmanager'
    $vmRoot = if ($env:SOPHON_WORKSTATION_VM_DIR) { $env:SOPHON_WORKSTATION_VM_DIR } else {
        Join-Path $env:LOCALAPPDATA 'Sophon\VMs'
    }
    $vmDirectory = Join-Path $vmRoot $Request.name
    $vmx = Join-Path $vmDirectory ($Request.name + '.vmx')
    $disk = Join-Path $vmDirectory ($Request.name + '.vmdk')
    $ownership = Join-Path $vmDirectory '.sophon-managed'

    if (Test-Path -LiteralPath $vmx -PathType Leaf) {
        $contents = Get-Content -LiteralPath $vmx -Raw
        if ($contents -notmatch ('(?m)^annotation\s*=\s*"' + [regex]::Escape($marker + ':preparing') + '"\s*$') -or
            -not (Test-Path -LiteralPath $ownership -PathType Leaf) -or
            (Get-Content -LiteralPath $ownership -Raw).Trim() -ne $marker) {
            throw 'An unmanaged VM already exists at the selected name. It has not been changed.'
        }
        $running = Get-RunningVMs $vmrun
        if ($vmx -notin $running) { Invoke-Workstation $vmrun @('start', $vmx, 'nogui') | Out-Null }
        return @{ changed = $vmx -notin $running; vm_path = $vmx; created = $false }
    }
    if (Test-Path -LiteralPath $vmDirectory) {
        throw 'VM directory already exists without a managed VM configuration. It has not been changed.'
    }

    $coreosIso = Resolve-ProjectFile $Request.coreos_iso
    $bundleIso = Resolve-ProjectFile $Request.bundle_iso
    New-Item -ItemType Directory -Force -Path $vmDirectory | Out-Null
    Invoke-Workstation $diskManager @('-c', '-s', "$($Request.disk_gb)GB", '-a', 'lsilogic', '-t', '0', $disk) | Out-Null

    $network = switch ($Request.network) {
        'bridged' { 'vmnet0' }
        'nat' { 'vmnet8' }
        'hostonly' { 'vmnet1' }
    }
    $vmxContent = @(
        'config.version = "8"'
        'virtualHW.version = "20"'
        'displayName = "' + $Request.name + '"'
        'annotation = "' + $marker + ':preparing"'
        'guestOS = "fedora-64"'
        'firmware = "efi"'
        'numvcpus = "' + [int]$Request.cores + '"'
        'memsize = "' + [int]$Request.memory + '"'
        'scsi0.present = "TRUE"'
        'scsi0.virtualDev = "lsilogic"'
        'scsi0:0.present = "TRUE"'
        'scsi0:0.fileName = "' + [IO.Path]::GetFileName($disk) + '"'
        'ethernet0.present = "TRUE"'
        'ethernet0.connectionType = "custom"'
        'ethernet0.vnet = "' + $network + '"'
        'ethernet0.addressType = "generated"'
        'ide1:0.present = "TRUE"'
        'ide1:0.deviceType = "cdrom-image"'
        'ide1:0.fileName = "' + $coreosIso.Replace('\', '\\') + '"'
        'ide1:0.startConnected = "TRUE"'
        'ide1:1.present = "TRUE"'
        'ide1:1.deviceType = "cdrom-image"'
        'ide1:1.fileName = "' + $bundleIso.Replace('\', '\\') + '"'
        'ide1:1.startConnected = "TRUE"'
        'bios.bootOrder = "hdd,cdrom"'
        'cleanShutdown = "FALSE"'
        'uuid.action = "create"'
    )
    Set-Content -LiteralPath $vmx -Value $vmxContent -Encoding ascii
    Invoke-Workstation $vmrun @('start', $vmx, 'nogui') | Out-Null
    Set-Content -LiteralPath $ownership -Value $marker -Encoding ascii
    return @{ changed = $true; vm_path = $vmx; created = $true }
}

Set-Content -LiteralPath $readyFile -Value 'ready' -Encoding ascii
try {
    while (-not (Test-Path -LiteralPath $stopFile)) {
        foreach ($requestFile in Get-ChildItem -LiteralPath $requests -Filter '*.json' -File) {
            $responseFile = Join-Path $responses $requestFile.Name
            if (Test-Path -LiteralPath $responseFile) { continue }
            try {
                $request = Get-Content -LiteralPath $requestFile.FullName -Raw | ConvertFrom-Json
                if ($request.id -ne [IO.Path]::GetFileNameWithoutExtension($requestFile.Name)) {
                    throw 'Request identifier mismatch.'
                }
                $result = if ($request.action -eq 'authenticate') {
                    Find-WorkstationTool 'vmrun' | Out-Null
                    @{ ok = $true; changed = $false; host = $env:COMPUTERNAME }
                } elseif ($request.action -eq 'provision') {
                    @{ ok = $true } + (Invoke-Provision $request)
                } else {
                    throw 'Unsupported Workstation request.'
                }
                $temporary = $responseFile + '.tmp'
                Set-Content -LiteralPath $temporary -Value ($result | ConvertTo-Json -Compress) -Encoding utf8
                Move-Item -LiteralPath $temporary -Destination $responseFile -Force
            } catch {
                $temporary = $responseFile + '.tmp'
                Set-Content -LiteralPath $temporary -Value (@{ ok = $false; error = $_.Exception.Message } | ConvertTo-Json -Compress) -Encoding utf8
                Move-Item -LiteralPath $temporary -Destination $responseFile -Force
            }
        }
        [Threading.Thread]::Sleep(250)
    }
} finally {
    Remove-Item -Force -ErrorAction SilentlyContinue $readyFile
}