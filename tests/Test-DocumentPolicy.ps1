$ErrorActionPreference = 'Stop'

$repoRoot = Split-Path -Parent $PSScriptRoot
$documentExtensions = @('.md', '.mdx', '.txt', '.doc', '.docx', '.pdf', '.rtf', '.odt')

$rootDocuments = Get-ChildItem -LiteralPath $repoRoot -File -Force |
    Where-Object { $documentExtensions -contains $_.Extension.ToLowerInvariant() }
$unexpectedRootDocuments = $rootDocuments | Where-Object { $_.Name -ne 'README.md' }
if ($unexpectedRootDocuments) {
    throw "Only README.md may be stored as a root document: $($unexpectedRootDocuments.Name -join ', ')"
}

$trackedDocuments = & git -C $repoRoot ls-files | Where-Object {
    $documentExtensions -contains [System.IO.Path]::GetExtension($_).ToLowerInvariant()
}
$unexpectedTrackedDocuments = $trackedDocuments | Where-Object { $_ -ne 'README.md' }
if ($unexpectedTrackedDocuments) {
    throw "Only README.md may be tracked as documentation: $($unexpectedTrackedDocuments -join ', ')"
}

$ignoreChecks = @(
    'docs/example.md',
    'notes.md',
    'draft.docx',
    'report.pdf',
    '.docx_work/example.txt'
)
foreach ($path in $ignoreChecks) {
    & git -C $repoRoot check-ignore --quiet --no-index -- $path
    if ($LASTEXITCODE -ne 0) { throw "Documentation path is not ignored: $path" }
}

& git -C $repoRoot check-ignore --quiet --no-index -- README.md
if ($LASTEXITCODE -eq 0) { throw 'README.md must remain trackable' }

Write-Output 'PASS: repository documentation policy validation'
