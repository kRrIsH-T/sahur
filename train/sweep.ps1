# Experiment grid for the System 1 model. Each run takes about 1-2 minutes on a laptop CPU.
$py = Join-Path $PSScriptRoot "..\.venv\Scripts\python.exe"
$train = Join-Path $PSScriptRoot "train_fast.py"
$runs = @(
    "--name bag-ce",
    "--name bag-rlcd --loss rlcd",
    "--name linear-ce --kind linear",
    "--name linear-rlcd --kind linear --loss rlcd",
    "--name bag-ce-d32 --dim 32",
    "--name bag-ce-d96 --dim 96",
    "--name bag-ce-nofold --no-fold",
    "--name bag-ce-c14 --char-min 1 --char-max 4",
    "--name bag-ce-noaug --aug-prob 0",
    "--name bag-ce-b16 --buckets 65536",
    "--name bag-ce-e40 --epochs 40"
)
foreach ($r in $runs) {
    Write-Output "=== $r"
    & $py -W ignore $train $r.Split(" ") 2>&1 | Select-String -Pattern '"(name|cal_acc_augmented|coverage|acc_when_accepted|size_mb|train_seconds)"'
}
