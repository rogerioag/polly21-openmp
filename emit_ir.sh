#!/usr/bin/env bash
# Gera LLVM IR (.ll) e objeto (.o) de cada kernel de uma árvore de fontes PolyBench,
# rotulando a saída com o nome da variante. Depois, rode o ompscan.py em results/ir.
#
# Uso: ./emit_ir.sh <variante> <dir_fontes> <dir_utilities> [flags extras...]
#
# Exemplos:
#   # Polly paralelizando o PolyBench/C original (ou a versão linearizada)
#   ./emit_ir.sh polly-omp ~/PolyBenchC-4.2.1 ~/PolyBenchC-4.2.1/utilities \
#       -mllvm -polly -mllvm -polly-parallel -mllvm -polly-omp-backend=LLVM \
#       -mllvm -polly-scheduling=static
#
#   # Saída anotada do DawnCC, compilada com o clang 21 para CPU
#   ./emit_ir.sh dawncc ~/dawncc-out ~/PolyBenchC-4.2.1/utilities -fopenmp
#
#   # PolyBench-ACC, versão OpenMP
#   ./emit_ir.sh acc-omp ~/PolyBench-ACC/OpenMP ~/PolyBench-ACC/common -fopenmp
set -euo pipefail

VAR=${1:?variante}; SRC=${2:?dir de fontes}; UTIL=${3:?dir utilities}; shift 3
OUT=${OUT:-$PWD/results}
CC=${CC:-clang}
DATASET=${DATASET:-LARGE_DATASET}
mkdir -p "$OUT/ir" "$OUT/obj" "$OUT/log"

# -g: linhas do fonte nas regiões; -fno-inline-functions: kernels não somem dentro do main
BASE="-O3 -g -fno-inline-functions -I$UTIL -DPOLYBENCH_TIME -D$DATASET"

find "$SRC" -name '*.c' ! -name 'polybench.c' ! -path '*/utilities/*' ! -path '*/common/*' | sort |
while read -r src; do
  k=$(basename "$src" .c)
  d=$(dirname "$src")
  # só <kernel>/<kernel>.c — ignora auxiliares como Nussinov.orig.c
  [ "$k" = "$(basename "$d")" ] || continue
  log="$OUT/log/$k.$VAR.txt"
  if $CC $BASE "$@" -I"$d" -S -emit-llvm "$src" -o "$OUT/ir/$k.$VAR.ll" -Rpass-analysis=polly 2> "$log" &&
     $CC $BASE "$@" -I"$d" -c "$src" -o "$OUT/obj/$k.$VAR.o" 2>> "$log"; then
    echo "ok    $k ($VAR)"
  else
    echo "FALHA $k ($VAR) — ver $log" >&2
  fi
done
