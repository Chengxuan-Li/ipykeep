# publish.ps1 — build (optional) and upload dist/ artifacts to TestPyPI or PyPI.
#
# The API token is read from a git-ignored file in .secrets/ and exposed to
# twine via the TWINE_* environment variables for this process only — it is
# never committed and never printed.
#
# Usage:
#   .\publish.ps1                 # upload existing dist/ to TestPyPI
#   .\publish.ps1 -Target pypi    # upload to real PyPI
#   .\publish.ps1 -Build          # rebuild dist/ first, then upload to TestPyPI
#
# Token files (create these yourself, paste the "pypi-..." token as the only line):
#   .secrets/testpypi-token.txt
#   .secrets/pypi-token.txt

param(
    [ValidateSet("testpypi", "pypi")]
    [string]$Target = "testpypi",
    [switch]$Build
)

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

$tokenFile = ".secrets/$Target-token.txt"
if (-not (Test-Path $tokenFile)) {
    throw "Missing $tokenFile - create it and paste your $Target API token (the 'pypi-...' string) as the only line."
}
$token = (Get-Content $tokenFile -Raw).Trim()
if ([string]::IsNullOrWhiteSpace($token) -or $token -eq "PASTE_YOUR_TESTPYPI_TOKEN_HERE" -or $token -eq "PASTE_YOUR_PYPI_TOKEN_HERE") {
    throw "$tokenFile still holds the placeholder - replace it with your real $Target token."
}

if ($Build) {
    Remove-Item -Recurse -Force dist -ErrorAction SilentlyContinue
    python -m build
}

if (-not (Test-Path "dist") -or (Get-ChildItem dist -ErrorAction SilentlyContinue).Count -eq 0) {
    throw "No artifacts in dist/. Run with -Build or run 'python -m build' first."
}

# twine reads these env vars; scoped to this process, inherited by the python child.
$env:TWINE_USERNAME = "__token__"
$env:TWINE_PASSWORD = $token
if ($Target -eq "testpypi") {
    $env:TWINE_REPOSITORY_URL = "https://test.pypi.org/legacy/"
} else {
    $env:TWINE_REPOSITORY_URL = "https://upload.pypi.org/legacy/"
}

Write-Host "Checking artifacts..." -ForegroundColor Cyan
python -m twine check dist/*

Write-Host "Uploading to $Target ..." -ForegroundColor Cyan
python -m twine upload dist/*

Write-Host "Done. View at https://$( if ($Target -eq 'testpypi') { 'test.' } )pypi.org/project/ipykeep/" -ForegroundColor Green
