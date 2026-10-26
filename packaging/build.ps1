# بناء ملف تشغيل مستقل (EXE) — Long Video Clips
# الاستخدام:
#   .\packaging\build.ps1 -Install     # أول مرة: يثبّت PyInstaller ثم يبني
#   .\packaging\build.ps1             # بناء فقط
param(
    [switch]$Install,
    [switch]$WithTorch
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root '.venv\Scripts\python.exe'
$spec = Join-Path $PSScriptRoot 'long_video_clips.spec'

if (-not (Test-Path $python)) {
    throw "لم أجد بيئة المشروع: $python`nأنشئها أولًا: python -m venv .venv"
}

& $python -c "import PyInstaller" 2>$null
if ($LASTEXITCODE -ne 0) {
    if (-not $Install) {
        throw "PyInstaller غير مثبت.`nشغّل: .\packaging\build.ps1 -Install"
    }
    Write-Host 'تثبيت PyInstaller...'
    & $python -m pip install --upgrade pyinstaller
    if ($LASTEXITCODE -ne 0) { throw 'فشل تثبيت PyInstaller' }
}

$env:LVC_WITH_TORCH = if ($WithTorch) { '1' } else { '0' }
$dist = Join-Path $root 'dist'
$work = Join-Path $root 'build'

Write-Host "بناء التطبيق إلى $dist ..."
& $python -m PyInstaller --noconfirm --clean --distpath $dist --workpath $work $spec
if ($LASTEXITCODE -ne 0) { throw 'فشل البناء' }

$exe = Join-Path $dist 'LongVideoClips\LongVideoClips.exe'
if (-not (Test-Path $exe)) { throw "لم يُنتج الملف المتوقع: $exe" }

$size = [math]::Round(((Get-ChildItem (Split-Path $exe) -Recurse | Measure-Object Length -Sum).Sum / 1MB), 1)
Write-Host ''
Write-Host "تم: $exe  ($size MB)"
Write-Host 'تحقّق سريع:  & "' + $exe + '" --smoke'
