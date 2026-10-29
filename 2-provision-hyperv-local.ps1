<#
.SYNOPSIS
  Step 2 for Hyper-V on THIS machine, as the signed-in user (no WinRM, no credentials, no Python).
.DESCRIPTION
  Run after step 1 (the container's `prepare`) has built the ISOs. Creates the same blank InfraVM as the
  WinRM provider: Generation 2, Secure Boot off, static memory, no checkpoints, one dynamic disk, the installer and
  deployment ISOs as DVD drives plus a third drive for the secret certificate ISO (empty unless it exists), disk first.
  Needs an elevated PowerShell or Hyper-V Administrators membership.
.EXAMPLE
  .\2-provision-hyperv-local.ps1 -Switch 'Default Switch'
.EXAMPLE
  .\2-provision-hyperv-local.ps1 -AttachCertificates   # insert the secret certificate ISO into the existing InfraVM
.EXAMPLE
  .\2-provision-hyperv-local.ps1 -DetachCertificates   # eject it again once sophon-deploy has used it
#>
param(
    [string]$Switch = $env:SOPHON_HYPERV_SWITCH,
    [string]$Path = $env:SOPHON_HYPERV_PATH,
    [string]$Name = $(if ($env:SOPHON_INFRAVM_NAME) { $env:SOPHON_INFRAVM_NAME } else { 'sophon-infravm' }),
    [int]$Cores = $(if ($env:SOPHON_INFRAVM_CORES) { $env:SOPHON_INFRAVM_CORES } else { 8 }),
    [int]$MemoryMB = $(if ($env:SOPHON_INFRAVM_MEMORY) { $env:SOPHON_INFRAVM_MEMORY } else { 8192 }),
    [int]$DiskGB = $(if ($env:SOPHON_INFRAVM_DISK_GB) { $env:SOPHON_INFRAVM_DISK_GB } else { 100 }),
    [string]$CoreOsIso = (Join-Path $PSScriptRoot 'artifacts\coreos\sophon-installer.iso'),
    [string]$BundleIso = (Join-Path $PSScriptRoot 'artifacts\bootstrap\sophon-deployment.iso'),
    [string]$CertificatesIso = (Join-Path $PSScriptRoot 'artifacts\secrets\sophon-certificates.iso'),
    [switch]$AttachCertificates,
    [switch]$DetachCertificates
)
$ErrorActionPreference = 'Stop'
$marker = 'sophon-infravm-two-iso-v1'

# Hyper-V needs Administrators or Hyper-V Administrators (S-1-5-32-578); relaunch elevated with one UAC prompt otherwise.
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$allowed = ([Security.Principal.WindowsPrincipal]$identity).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator) -or
    ([Security.Principal.WindowsPrincipal]$identity).IsInRole([Security.Principal.SecurityIdentifier]'S-1-5-32-578')
if (-not $allowed) {
    Write-Host 'Hyper-V needs administrator rights; requesting elevation (UAC)...'
    $arguments = @('-NoProfile', '-NoExit', '-ExecutionPolicy', 'Bypass', '-File', "`"$PSCommandPath`"")
    foreach ($key in $PSBoundParameters.Keys) {
        if ($PSBoundParameters[$key] -is [switch]) { $arguments += "-$key" } else { $arguments += "-$key", "`"$($PSBoundParameters[$key])`"" }
    }
    Start-Process -FilePath (Get-Process -Id $PID).Path -Verb RunAs -ArgumentList $arguments
    return
}

$manageCertificates = $AttachCertificates -or $DetachCertificates
if ($AttachCertificates -and $DetachCertificates) { throw 'Use either -AttachCertificates or -DetachCertificates, not both.' }
if ($Name -notmatch '^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$') { throw 'VM name must contain 1-80 letters, digits, dots, underscores or hyphens.' }
if (-not $manageCertificates) {
    if ($Cores -lt 1 -or $MemoryMB -lt 1024 -or $DiskGB -lt 40) { throw 'Provide valid CPU/memory and at least 40 GiB of disk space.' }
    foreach ($iso in $CoreOsIso, $BundleIso) { if (-not (Test-Path -LiteralPath $iso -PathType Leaf)) { throw "Missing installation ISO: $iso (run step 1 first)." } }
}

try { $vmHost = Get-VMHost } catch { throw "Cannot use Hyper-V as this user ($($_.Exception.Message)). Run from an elevated PowerShell or join Hyper-V Administrators." }

if ($manageCertificates) {
    $vm = @(Get-VM | Where-Object { $_.Name -eq $Name -and $_.Notes -eq $marker })
    if ($vm.Count -ne 1) { throw "No managed InfraVM named '$Name' was found; create it first." }
    $vm = $vm[0]
    $full = if (Test-Path -LiteralPath $CertificatesIso) { (Resolve-Path -LiteralPath $CertificatesIso).Path } else { $CertificatesIso }
    $drives = @(Get-VMDvdDrive -VM $vm)
    if ($DetachCertificates) {
        foreach ($drive in $drives | Where-Object { $_.Path -eq $full }) { Set-VMDvdDrive -VMDvdDrive $drive -Path $null }
        Write-Host 'Certificate disk ejected. Delete the ISO from this PC when you no longer need it.'
        return
    }
    if (-not (Test-Path -LiteralPath $CertificatesIso -PathType Leaf)) { throw "Missing certificate ISO: $CertificatesIso (run: .\sophon-provision.ps1 certificates issue)." }
    $empty = $drives | Where-Object { -not $_.Path } | Select-Object -First 1
    if ($empty) { Set-VMDvdDrive -VMDvdDrive $empty -Path $full } else { $null = Add-VMDvdDrive -VM $vm -Path $full }
    Write-Host "Certificate disk attached to '$Name'. On the VM run sophon-deploy and answer y to air-gapped."
    return
}

if (-not $Switch) {
    $names = @(Get-VMSwitch | ForEach-Object Name)
    if (-not $names) { throw 'This host has no virtual switches; create one in Hyper-V Manager > Virtual Switch Manager.' }
    Write-Host ('Virtual switches: ' + ($names -join ', '))
    $default = if ($names -contains 'Default Switch') { 'Default Switch' } else { $names[0] }
    $answer = Read-Host "Virtual switch [$default]"
    $Switch = if ($answer) { $answer } else { $default }
}
if (-not (Get-VMSwitch | Where-Object { $_.Name -eq $Switch })) { throw "Virtual switch '$Switch' does not exist on this host." }
if (-not $Path) { $Path = $vmHost.VirtualMachinePath }

$existing = @(Get-VM | Where-Object { $_.Name -eq $Name })
if ($existing.Count -gt 1) { throw 'Multiple VMs use this name. No VMs have been changed.' }
if ($existing.Count -eq 1) {
    $vm = $existing[0]
    if ($vm.Notes -ne $marker) { throw 'Existing VM is unmanaged or an incomplete bootstrap. It has not been changed.' }
    if ($vm.State -notin 'Running', 'Off') { throw "Existing VM state is $($vm.State); inspect it before continuing." }
    if ($vm.State -eq 'Off') { Start-VM -VM $vm }
    Write-Host "InfraVM '$Name' already exists ($($vm.State)); nothing else changed."
    return
}

# ISOs are copied once, hash-named, so Hyper-V gets stable files it can read regardless of where the project lives.
$isoDir = Join-Path $Path 'sophon-isos'
New-Item -ItemType Directory -Force -Path $isoDir | Out-Null
function Install-Iso([string]$Source, [string]$Label) {
    $hash = (Get-FileHash -LiteralPath $Source -Algorithm SHA256).Hash.ToLower()
    $target = Join-Path $isoDir "sophon-$Label-$hash.iso"
    $size = (Get-Item -LiteralPath $Source).Length
    if (Test-Path -LiteralPath $target) {
        if ((Get-Item -LiteralPath $target).Length -ne $size) { throw 'A hash-named ISO has an unexpected size on the host; refusing to overwrite it.' }
        return $target
    }
    Copy-Item -LiteralPath $Source -Destination "$target.part"
    if ((Get-FileHash -LiteralPath "$target.part" -Algorithm SHA256).Hash.ToLower() -ne $hash) {
        Remove-Item -LiteralPath "$target.part" -Force
        throw 'Copied ISO failed SHA-256 verification and was removed.'
    }
    Move-Item -LiteralPath "$target.part" -Destination $target
    return $target
}
$coreos = Install-Iso $CoreOsIso 'coreos'
$bundle = Install-Iso $BundleIso 'deployment'

New-Item -ItemType Directory -Force -Path $Path | Out-Null
$vm = New-VM -Name $Name -Generation 2 -MemoryStartupBytes ([int64]$MemoryMB * 1MB) `
    -Path $Path -NewVHDPath (Join-Path $Path "$Name.vhdx") -NewVHDSizeBytes ([int64]$DiskGB * 1GB) -SwitchName $Switch
# The marker stays ':preparing' until the hardware is complete, so a half-built VM is never adopted.
Set-VM -VM $vm -StaticMemory -AutomaticCheckpointsEnabled $false -CheckpointType Disabled -Notes "${marker}:preparing"
Set-VMProcessor -VM $vm -Count $Cores
Set-VMFirmware -VM $vm -EnableSecureBoot Off
$null = Add-VMDvdDrive -VM $vm -Path $coreos
$null = Add-VMDvdDrive -VM $vm -Path $bundle
# A third drive for the secret certificate ISO (air-gapped sites): inserted now if it exists, or later with -AttachCertificates.
if (Test-Path -LiteralPath $CertificatesIso -PathType Leaf) { $null = Add-VMDvdDrive -VM $vm -Path (Resolve-Path -LiteralPath $CertificatesIso).Path }
else { $null = Add-VMDvdDrive -VM $vm }
$disk = Get-VMHardDiskDrive -VM $vm
$installer = Get-VMDvdDrive -VM $vm | Where-Object { $_.Path -eq $coreos }
Set-VMFirmware -VM $vm -BootOrder $disk, $installer
Set-VM -VM $vm -Notes $marker
Start-VM -VM $vm
Write-Host "InfraVM '$Name' created and started. Step 3 installs by itself; open it in Hyper-V Manager > Connect, choose the core password when asked, log in and run sophon-deploy (step 4)."
