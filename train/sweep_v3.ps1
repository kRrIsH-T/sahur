# System 1 v3: rebuild the dataset (slots + r3 rows + the user's corrections, test sets removed), train the action
# model (12 actions incl. "multi", control and language heads; 3 RLCD seeds merged and re-calibrated) and the slot
# tagger, then install them as the app's models. Run from the project root:
#     powershell -File train/sweep_v3.ps1
# For a trial run that touches nothing in the repo, point it at a scratch copy and skip the install:
#     powershell -File train/sweep_v3.ps1 -DataDir <scratch>\data -ModelsDir <scratch>\models -Corrections none -NoInstall
param(
    [string]$DataDir = "train/data",
    [string]$ModelsDir = "bolos/fast/models",
    [string]$Corrections = "",  # default: corrections.jsonl next to PREFERENCES_PATH, if present; "none" to skip
    [int]$Epochs = 30,
    [int]$SlotEpochs = 20,
    [switch]$NoInstall
)
$ErrorActionPreference = "Stop"
$env:PYTHONUNBUFFERED = "1"
$data = Join-Path $DataDir "dataset.jsonl"

# Native commands don't stop the script on their own, so every step checks its exit code.
uv run python train/check_rows.py "$DataDir/slots/*.jsonl" "$DataDir/r3/*.jsonl"
if ($LASTEXITCODE) { throw "bad training rows (see above)" }
$build = @("--data-dir", $DataDir)
if ($Corrections) { $build += @("--corrections", $Corrections) }
uv run python train/build_dataset.py @build
if ($LASTEXITCODE) { throw "building the dataset failed" }

foreach ($seed in 1, 2, 3) {
    uv run python train/train_fast.py --name a3-rlcd-s$seed --loss rlcd --seed $seed --char-min 1 --char-max 4 `
        --buckets 65536 --epochs $Epochs --data $data --models-dir $ModelsDir
    if ($LASTEXITCODE) { throw "training seed $seed failed" }
}
uv run python train/ensemble.py --name a3-rlcd-ens a3-rlcd-s1 a3-rlcd-s2 a3-rlcd-s3 --data $data --models-dir $ModelsDir
if ($LASTEXITCODE) { throw "ensemble failed" }
uv run python train/train_slots.py --name s3-96 --dim 96 --hidden 96 --epochs $SlotEpochs --data $data `
    --models-dir $ModelsDir
if ($LASTEXITCODE) { throw "slot tagger failed" }

if ($NoInstall) {
    Write-Host "Trained into $ModelsDir (not installed)"
} else {
    Copy-Item (Join-Path $ModelsDir "a3-rlcd-ens.npz") bolos/fast/models/system1.npz -Force
    Copy-Item (Join-Path $ModelsDir "s3-96.npz") bolos/fast/models/slots.npz -Force
    Write-Host "Installed bolos/fast/models/system1.npz and slots.npz"
}
