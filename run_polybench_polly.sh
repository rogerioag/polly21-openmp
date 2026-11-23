#!/usr/bin/env bash
# Compila e executa o PolyBench/C com clang 21 em 3 variantes:
#   base      : clang -O3
#   polly     : -O3 + Polly (tiling/fusão, sequencial)
#   polly-omp : -O3 + Polly + paralelização automática com OpenMP (libomp)
#
# Uso: ./run_polybench_polly.sh <dir_polybench> ["DATASETS"] [THREADS] [REPS]
#   ex.: ./run_polybench_polly.sh ~/PolyBenchC-4.2.1 "MEDIUM LARGE EXTRALARGE" 16 5
#
# SIZES=runtime (padrão): cada kernel é compilado UMA vez, com os tamanhos lidos
#   em tempo de execução (pbsize.h + sizes/<kernel>.h); os datasets variam só na
#   execução, via PB_DATASET. Dimensões avulsas também podem ser passadas por
#   variável de ambiente, ex.: NI=3000 ./bin/gemm.polly-omp
# SIZES=fixed: tamanhos como constantes (-D<DATASET>_DATASET), um binário por
#   dataset — o comportamento original do PolyBench.
#
# Cada execução grava tudo numa pasta própria, results/<data-hora>[-TAG]/:
#   manifest.txt   o que foi comparado: compilador, flags de cada variante,
#                  datasets, threads, máquina, commit do repositório
#   times.csv      tempos
#   ir/ asm/       LLVM IR e assembly de cada kernel/variante (o que o Polly gerou)
#   bin/           executáveis;  remarks/  relatórios -Rpass-analysis=polly
#   omp-structure.csv  regiões paralelas recuperadas pelo ompscan.py
# TAG=nome dá um rótulo à pasta (ex.: TAG=sem-pattern-matching).
set -euo pipefail

PB=${1:?informe o diretório do PolyBench/C}
DATASETS=${2:-LARGE}
THREADS=${3:-$(nproc)}
REPS=${4:-5}
SIZES=${SIZES:-runtime}
CC=${CC:-clang}
HERE=$(cd "$(dirname "$0")" && pwd)
STAMP=$(date +%Y%m%d-%H%M%S)
OUT=${OUT:-$HERE/results/$STAMP${TAG:+-$TAG}}

mkdir -p "$OUT/bin" "$OUT/remarks" "$OUT/ir" "$OUT/asm"
CSV="$OUT/times.csv"
echo "kernel,variant,dataset,sizes,threads,rep,seconds" > "$CSV"

COMMON="-O3 -march=native -I$PB/utilities -DPOLYBENCH_TIME"
POLLY="-mllvm -polly"
# o número de threads fica só em OMP_NUM_THREADS (sem -polly-num-threads),
# para também poder variar na execução
POLLY_OMP="$POLLY -mllvm -polly-parallel -mllvm -polly-omp-backend=LLVM \
           -mllvm -polly-scheduling=static -fopenmp"

declare -A FLAGS=(
  [base]=""
  [polly]="$POLLY"
  [polly-omp]="$POLLY_OMP"
)

if [ "$SIZES" = runtime ]; then
  python3 "$HERE/runtime-sizes/gen_sizes.py" "$PB" "$OUT/sizes" >/dev/null
fi

# ---- manifest: o que foi comparado nesta execução
{
  echo "data:        $(date -Iseconds)"
  echo "tag:         ${TAG:-}"
  echo "host:        $(hostname)"
  echo "cpu:         $(grep -m1 'model name' /proc/cpuinfo | cut -d: -f2- | sed 's/^ *//')"
  echo "núcleos:     $(nproc)"
  echo "repositório: $(git -C "$HERE" rev-parse --short HEAD 2>/dev/null || echo '?')$(git -C "$HERE" diff --quiet 2>/dev/null || echo ' (com alterações locais)')"
  echo "polybench:   $PB"
  echo "datasets:    $DATASETS   (sizes=$SIZES)"
  echo "threads:     $THREADS    repetições: $REPS"
  echo "compilador:  $($CC --version | head -1)"
  echo "flags comuns: $COMMON"
  for v in base polly polly-omp; do echo "variante $v: ${FLAGS[$v]}" | tr -s ' '; done
} > "$OUT/manifest.txt"

# só <kernel>/<kernel>.c — ignora arquivos auxiliares como medley/nussinov/Nussinov.orig.c
mapfile -t KERNELS < <(find "$PB" -name '*.c' ! -path '*/utilities/*' | sort |
                       while read -r f; do [ "$(basename "$f" .c)" = "$(basename "$(dirname "$f")")" ] && echo "$f"; done)

compile() {  # compile <src> <variante> <bin> <log> [flags de tamanho...]
  local src=$1 v=$2 bin=$3 log=$4; shift 4
  local name; name=$(basename "$bin")
  local flags="$COMMON ${FLAGS[$v]} $* -I$(dirname "$src")"
  # executável (+ remarks do Polly)
  $CC $flags "$src" "$PB/utilities/polybench.c" -Rpass-analysis=polly -lm -o "$bin" 2> "$log" || return 1
  # o que o Polly gerou para o kernel: IR (com -g e sem inlining dos kernels,
  # para o ompscan.py) e assembly com as mesmas flags do executável
  $CC $flags -g -fno-inline-functions -S -emit-llvm "$src" -o "$OUT/ir/$name.ll" 2>> "$log"
  $CC $flags -S -masm=intel "$src" -o "$OUT/asm/$name.s" 2>> "$log"
}

run() {  # run <bin> <kernel> <variante> <dataset> [env...]
  local bin=$1 k=$2 v=$3 ds=$4; shift 4
  for r in $(seq 1 "$REPS"); do
    t=$(env "$@" OMP_NUM_THREADS="$THREADS" "$bin")
    echo "$k,$v,$ds,$SIZES,$THREADS,$r,$t" >> "$CSV"
  done
}

for src in "${KERNELS[@]}"; do
  k=$(basename "$src" .c)
  for v in base polly polly-omp; do
    if [ "$SIZES" = runtime ]; then
      bin="$OUT/bin/$k.$v"
      if ! compile "$src" "$v" "$bin" "$OUT/remarks/$k.$v.txt" \
           -DPOLYBENCH_USE_C99_PROTO -include "$HERE/runtime-sizes/pbsize.h" -include "$OUT/sizes/$k.h"; then
        echo "FALHA ao compilar $k ($v) — ver $OUT/remarks/$k.$v.txt" >&2; continue
      fi
      for ds in $DATASETS; do
        ds=${ds%_DATASET}
        run "$bin" "$k" "$v" "$ds" PB_DATASET="$ds"
        echo "$k $v $ds ok"
      done
    else
      for ds in $DATASETS; do
        ds=${ds%_DATASET}
        bin="$OUT/bin/$k.$v.$ds"
        if ! compile "$src" "$v" "$bin" "$OUT/remarks/$k.$v.$ds.txt" -D"${ds}_DATASET"; then
          echo "FALHA ao compilar $k ($v, $ds) — ver $OUT/remarks/$k.$v.$ds.txt" >&2; continue
        fi
        run "$bin" "$k" "$v" "$ds"
        echo "$k $v $ds ok"
      done
    fi
  done
done

python3 "$HERE/ompscan.py" --quiet --csv "$OUT/omp-structure.csv" "$OUT"/ir/*.ll || true
ln -sfn "$(basename "$OUT")" "$HERE/results/latest"
echo "Resultados em $OUT  (results/latest aponta para esta execução)"
