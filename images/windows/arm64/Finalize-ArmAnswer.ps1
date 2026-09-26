[CmdletBinding()]
param(
    [Parameter(Mandatory)] [string] $Source,
    [Parameter(Mandatory)] [string] $Template,
    [Parameter(Mandatory)] [string] $PasswordBlob,
    [Parameter(Mandatory)] [string] $Output
)

$ErrorActionPreference = 'Stop'
$original = [xml](Get-Content -LiteralPath $Source -Raw)
$answer = [xml](Get-Content -LiteralPath $Template -Raw)

# The ARM template adds Setup's Pro, disk and account settings, while the
# Winhance payload must stay exactly as supplied by the user.
if ($original.unattend.Extensions.File.InnerText -cne
    $answer.unattend.Extensions.File.InnerText) {
    throw 'The template does not contain the exact Winhance script from the source answer file.'
}

$ns = [Xml.XmlNamespaceManager]::new($answer.NameTable)
$ns.AddNamespace('u', 'urn:schemas-microsoft-com:unattend')
$components = $answer.SelectNodes('//u:component', $ns)
if (@($components | Where-Object { $_.GetAttribute('processorArchitecture') -ne 'arm64' }).Count) {
    throw 'The ARM answer file still contains components for another architecture.'
}
$image = $answer.SelectSingleNode(
    "//u:settings[@pass='windowsPE']/u:component[@name='Microsoft-Windows-Setup']/u:ImageInstall/u:OSImage/u:InstallFrom/u:MetaData", $ns)
if (-not $image -or $image.Key -ne '/IMAGE/NAME' -or $image.Value -ne 'Windows 11 Pro') {
    throw 'The answer file does not select Windows 11 Pro.'
}

$passwordValues = $answer.SelectNodes(
    "//u:settings[@pass='oobeSystem']/u:component[@name='Microsoft-Windows-Shell-Setup']//u:Password/u:Value", $ns)
if ($passwordValues.Count -ne 2) { throw 'Expected the admin account and first-logon password nodes.' }
$adminName = $answer.SelectSingleNode(
    "//u:settings[@pass='oobeSystem']/u:component[@name='Microsoft-Windows-Shell-Setup']/u:UserAccounts/u:LocalAccounts/u:LocalAccount/u:Name", $ns)
if ($adminName.InnerText -ne 'admin') { throw 'The local account is not admin.' }

$secret = (Get-Content -LiteralPath $PasswordBlob -Raw).Trim() | ConvertTo-SecureString
$bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secret)
try {
    $plain = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
    foreach ($value in $passwordValues) { $value.InnerText = $plain }
    $answer.Save($Output)
    $plain = $null
} finally {
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
}

$check = [xml](Get-Content -LiteralPath $Output -Raw)
if ($check.unattend.Extensions.File.InnerText -cne $original.unattend.Extensions.File.InnerText) {
    throw 'The saved answer file changed the Winhance payload.'
}
Write-Host "ARM64 Windows 11 Pro answer file prepared: $Output"
