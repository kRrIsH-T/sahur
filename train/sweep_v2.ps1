# System 1 v2: action model (3 RLCD seeds merged) and slot tagger, on train/data/dataset.jsonl, then installed as
# the app's models. Run from the project root: powershell -File train/sweep_v2.ps1
$ErrorActionPreference = "Stop"
$env:PYTHONUNBUFFERED = "1"
foreach ($seed in 1, 2, 3) {
    uv run python train/train_fast.py --name a-rlcd-s$seed --loss rlcd --seed $seed --char-min 1 --char-max 4 --buckets 65536 --epochs 30
    if ($LASTEXITCODE) { throw "training seed $seed failed" }
}
uv run python train/ensemble.py --name a-rlcd-ens a-rlcd-s1 a-rlcd-s2 a-rlcd-s3
if ($LASTEXITCODE) { throw "ensemble failed" }  # native commands don't stop the script on their own
uv run python train/train_slots.py --name s-96 --dim 96 --hidden 96 --epochs 20
if ($LASTEXITCODE) { throw "slot tagger failed" }
Copy-Item bolos/fast/models/a-rlcd-ens.npz bolos/fast/models/system1.npz -Force
Copy-Item bolos/fast/models/s-96.npz bolos/fast/models/slots.npz -Force
