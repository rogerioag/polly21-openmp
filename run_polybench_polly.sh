#!/usr/bin/env bash
# Compila e executa o PolyBench/C com clang 21 em 3 variantes:
#   base      : clang -O3
#   polly     : -O3 + Polly (tiling/fusão, sequencial)
#   polly-omp : -O3 + Polly + paralelização automática com OpenMP (libomp)
#
# Uso: ./run_polybench_polly.sh <dir_polybench> [DATASET] [THREADS] [REPS]
#   ex.: ./run_polybench_polly.sh ~/PolyBench-C-4.2.1 LARGE_DATASET 16 5
set -euo pipefail

PB=${1:?informe o diretório do PolyBench/C}
DATASET=${2:-LARGE_DATASET}
THREADS=${3:-$(nproc)}
REPS=${4:-5}
OUT=${OUT:-$PWD/results-polly21}
CC=${CC:-clang}

mkdir -p "$OUT/bin" "$OUT/remarks"
CSV="$OUT/times.csv"
echo "kernel,variant,threads,rep,seconds" > "$CSV"

COMMON="-O3 -march=native -I$PB/utilities -DPOLYBENCH_TIME -D$DATASET"
POLLY="-mllvm -polly"
POLLY_OMP="$POLLY -mllvm -polly-parallel -mllvm -polly-omp-backend=LLVM \
           -mllvm -polly-num-threads=$THREADS -mllvm -polly-scheduling=static -fopenmp"

declare -A FLAGS=(
  [base]=""
  [polly]="$POLLY"
  [polly-omp]="$POLLY_OMP"
)

mapfile -t KERNELS < <(find "$PB" -name '*.c' ! -path '*/utilities/*' | sort)

for src in "${KERNELS[@]}"; do
  k=$(basename "$src" .c)
  dir=$(dirname "$src")
  for v in base polly polly-omp; do
    bin="$OUT/bin/$k.$v"
    # -Rpass-analysis=polly registra quais SCoPs o Polly detectou/otimizou
    if ! $CC $COMMON ${FLAGS[$v]} -I"$dir" "$src" "$PB/utilities/polybench.c" \
         -Rpass-analysis=polly -lm -o "$bin" 2> "$OUT/remarks/$k.$v.txt"; then
      echo "FALHA ao compilar $k ($v) — ver $OUT/remarks/$k.$v.txt" >&2
      continue
    fi
    for r in $(seq 1 "$REPS"); do
      t=$(OMP_NUM_THREADS=$THREADS "$bin")
      echo "$k,$v,$THREADS,$r,$t" >> "$CSV"
    done
    echo "$k $v ok"
  done
done

echo "Tempos em $CSV"
