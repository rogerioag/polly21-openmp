#!/usr/bin/env python3
"""Gera sizes/<kernel>.h para cada kernel de uma árvore PolyBench.

Cada dimensão definida nos blocos *_DATASET do <kernel>.h vira
    #define NI pb_size("NI", mini, small, medium, large, extralarge)
e, como o NI fica definido antes do <kernel>.h, o bloco de tamanhos fixos
dele é pulado (guarda "#if !defined(NI) && ...").

Uso:
  gen_sizes.py <dir_fontes> <dir_saida> [--ref <dir_PolyBenchC-4.2.1>]

--ref: usa os tamanhos do PolyBench/C 4.2.1 de referência para os kernels
correspondentes, para que "LARGE" signifique as mesmas dimensões em todas as
variantes. Isso vale para o PolyBench-ACC, que é baseado no PolyBench 3.2 e tem
outros tamanhos e outro nome de dataset (STANDARD). Kernels sem
correspondente, ou com nomes de dimensão diferentes, mantêm os tamanhos do
próprio cabeçalho, e um aviso é impresso.
"""
import argparse, os, re, sys

DS = ["MINI", "SMALL", "MEDIUM", "LARGE", "EXTRALARGE"]
ALT = {"MEDIUM": ["MEDIUM", "STANDARD"]}          # PolyBench 3.2 / ACC
ALIAS = {"jacobi-1d-imper": "jacobi-1d", "jacobi-2d-imper": "jacobi-2d"}

def kernels(root):
    for d, _, files in os.walk(root):
        k = os.path.basename(d)
        if k + ".c" in files and k + ".h" in files:
            yield k, os.path.join(d, k + ".h")

def read_sizes(h):
    text = open(h, errors="replace").read()
    vals = {}
    for ds in DS:
        m = None
        for name in ALT.get(ds, [ds]):
            m = re.search(r"#\s*ifdef\s+%s_DATASET\b[^\n]*\n(.*?)#\s*endif" % name, text, re.S)
            if m: break
        if not m:
            raise ValueError(f"{h}: bloco {ds}_DATASET não encontrado")
        for name, val in re.findall(r"#\s*define\s+(\w+)\s+(\S+)", m.group(1)):
            vals.setdefault(name, {})[ds] = val
    for name, v in vals.items():
        if set(v) != set(DS):
            raise ValueError(f"{h}: {name} não definido em todos os datasets")
    return vals

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("src"); ap.add_argument("out")
    ap.add_argument("--ref", help="PolyBench/C 4.2.1 cujos tamanhos devem ser usados")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    ref = {k: h for k, h in kernels(a.ref)} if a.ref else {}

    n = 0
    for k, h in sorted(kernels(a.src)):
        try:
            own = read_sizes(h)
        except ValueError as e:
            print(f"aviso: {e} — kernel ignorado", file=sys.stderr); continue
        vals, origin = own, f"{k}.h"
        rk = ALIAS.get(k, k)
        if a.ref:
            if rk not in ref:
                print(f"aviso: {k}: sem correspondente no PolyBench/C de referência — tamanhos próprios", file=sys.stderr)
            else:
                rv = read_sizes(ref[rk])
                if set(rv) == set(own):
                    vals, origin = rv, f"{rk}.h (referência)"
                else:
                    print(f"aviso: {k}: dimensões {sorted(own)} ≠ referência {sorted(rv)} — tamanhos próprios", file=sys.stderr)
        lines = [f"/* gerado por gen_sizes.py; tamanhos de {origin} */"]
        for name, v in vals.items():
            lines.append(f'#define {name} pb_size("{name}", {", ".join(v[d] for d in DS)})')
        open(os.path.join(a.out, k + ".h"), "w").write("\n".join(lines) + "\n")
        n += 1
    print(f"{n} kernels -> {a.out}")

if __name__ == "__main__":
    main()
