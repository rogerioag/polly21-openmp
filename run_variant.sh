#!/usr/bin/env bash
# Compila e executa outra árvore de fontes já anotada com OpenMP (saída do
# DawnCC, PolyBench-ACC/OpenMP, ...) com os mesmos datasets, threads e
# repetições de uma execução do run_polybench_polly.sh, acrescentando os
# resultados à MESMA pasta (times.csv, ir/, asm/, bin/, manifest.txt).
#
# Uso: ./run_variant.sh <variante> <dir_fontes> [flags extras...]
#   ex.: ./run_variant.sh dawncc  ~/dawncc-out          -fopenmp
#        ./run_variant.sh acc-omp ~/PolyBench-ACC/OpenMP -fopenmp
#
# Variáveis:
#   OUT       pasta da execução (padrão: results/latest)
#   PB_REF    PolyBench/C 4.2.1 de referência para os tamanhos (padrão: o
#             "polybench:" do manifest). Assim, LARGE tem as mesmas dimensões
#             que nas variantes do Polly, mesmo no PolyBench-ACC (base 3.2).
#   UTIL      utilities da árvore (padrão: <dir_fontes>/utilities)
#   DATASETS, THREADS, REPS   padrão: os mesmos do manifest
#
# Os tamanhos são sempre lidos em tempo de execução (PB_DATASET / NI=... etc.).
set -euo pipefail

VAR=${1:?variante}; SRC=$(cd "${2:?dir de fontes}" && pwd); shift 2
HERE=$(cd "$(dirname "$0")" && pwd)
OUT=$(cd "${OUT:-$HERE/results/latest}" && pwd)
MAN="$OUT/manifest.txt"
[ -f "$MAN" ] || { echo "sem manifest em $OUT — rode antes o run_polybench_polly.sh" >&2; exit 1; }
field() { sed -n "s/^$1: *//p" "$MAN" | head -1; }

PB_REF=${PB_REF:-$(field polybench)}
DATASETS=${DATASETS:-$(field datasets | sed 's/ *(sizes=.*//')}
THREADS=${THREADS:-$(field threads | awk '{print $1}')}
REPS=${REPS:-$(field threads | sed -n 's/.*repetições: *//p')}
UTIL=${UTIL:-$SRC/utilities}
CC=${CC:-clang}
MSIZES=$(field datasets | sed -n 's/.*sizes=\([a-z]*\).*/\1/p')
[ "$MSIZES" = runtime ] || echo "aviso: a execução base usou sizes=$MSIZES; aqui os tamanhos são lidos em tempo de execução" >&2

COMMON="-O3 -march=native -I$UTIL -DPOLYBENCH_TIME -DPOLYBENCH_USE_C99_PROTO"
SZ="$OUT/sizes-$VAR"
python3 "$HERE/runtime-sizes/gen_sizes.py" "$SRC" "$SZ" --ref "$PB_REF" 2> "$OUT/sizes-$VAR.avisos.txt" >/dev/null
[ -s "$OUT/sizes-$VAR.avisos.txt" ] && echo "avisos de tamanho em $OUT/sizes-$VAR.avisos.txt" >&2

{
  echo "variante $VAR: $* (fontes: $SRC; tamanhos: $PB_REF; compilada em $(date -Iseconds))"
} >> "$MAN"

CSV="$OUT/times.csv"
mapfile -t KERNELS < <(find "$SRC" -name '*.c' ! -name 'polybench.c' ! -path '*/utilities/*' ! -path '*/common/*' | sort |
                       while read -r f; do [ "$(basename "$f" .c)" = "$(basename "$(dirname "$f")")" ] && echo "$f"; done)

for src in "${KERNELS[@]}"; do
  k=$(basename "$src" .c); d=$(dirname "$src")
  [ -f "$SZ/$k.h" ] || { echo "PULADO $k ($VAR) — sem tabela de tamanhos" >&2; continue; }
  bin="$OUT/bin/$k.$VAR"; log="$OUT/remarks/$k.$VAR.txt"
  flags="$COMMON $* -I$d -include $HERE/runtime-sizes/pbsize.h -include $SZ/$k.h"
  if ! $CC $flags "$src" "$UTIL/polybench.c" -lm -o "$bin" 2> "$log"; then
    echo "FALHA ao compilar $k ($VAR) — ver $log" >&2; continue
  fi
  $CC $flags -g -fno-inline-functions -S -emit-llvm "$src" -o "$OUT/ir/$k.$VAR.ll" 2>> "$log" || true
  $CC $flags -S -masm=intel "$src" -o "$OUT/asm/$k.$VAR.s" 2>> "$log" || true
  for ds in $DATASETS; do
    ds=${ds%_DATASET}
    for r in $(seq 1 "$REPS"); do
      t=$(PB_DATASET="$ds" OMP_NUM_THREADS="$THREADS" "$bin")
      echo "$k,$VAR,$ds,runtime,$THREADS,$r,$t" >> "$CSV"
    done
    echo "$k $VAR $ds ok"
  done
done

python3 "$HERE/ompscan.py" --quiet --csv "$OUT/omp-structure.csv" "$OUT"/ir/*.ll || true
echo "Resultados acrescentados em $OUT"
