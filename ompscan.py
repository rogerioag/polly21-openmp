#!/usr/bin/env python3
"""
ompscan.py — recupera a estrutura OpenMP a partir do código gerado.

Lê LLVM IR (.ll; modo preferido) ou objetos/binários x86-64 (via objdump) e,
para cada função "hospedeira" (ex.: kernel_gemm), reconstrói:
  - regiões paralelas (__kmpc_fork_call / GOMP_parallel*), a função outlined
    e quem a gerou (clang, Polly, GOMP);
  - se o fork está dentro de um laço (fork repetido a cada iteração externa);
  - laços worksharing: __kmpc_for_static_init_* / __kmpc_dispatch_init_*,
    com schedule decodificado (static, dynamic, runtime...), chunk e largura
    da variável de indução (4 = int, 8 = 64 bits);
  - reduções, barreiras, critical, single, master, tasks, target.

Uso:
  ompscan.py arquivo.ll [mais.ll ...]            # relatório em texto
  ompscan.py --csv saida.csv results/ir/*.ll     # uma linha por (arquivo, função)
  ompscan.py --json saida.json ...
  ompscan.py --binary gemm.polly-omp [...]       # usa objdump -dr (heurístico)

Para o IR, compile com:  -S -emit-llvm -g -fno-inline-functions
(-g dá a linha do fonte de cada região; -fno-inline-functions evita que os
kernels static do PolyBench sejam inlinados no main).
"""
import argparse, csv, json, os, re, subprocess, sys
from collections import defaultdict

# ---------------------------------------------------------------- schedules
SCHED = {
    33: "static,chunk", 34: "static", 35: "dynamic", 36: "guided", 37: "runtime",
    38: "auto", 39: "trapezoidal", 40: "static_greedy", 41: "static_balanced",
    42: "guided_iterative", 43: "guided_analytical", 44: "static_steal",
    45: "static_balanced_chunked", 46: "guided_simd", 47: "runtime_simd",
    65: "ordered static,chunk", 66: "ordered static", 67: "ordered dynamic",
    68: "ordered guided", 69: "ordered runtime", 70: "ordered auto",
    91: "distribute static,chunk", 92: "distribute static",
}

def decode_sched(v):
    if v is None:
        return "?"
    mods = []
    if v & (1 << 29): mods.append("monotonic")
    if v & (1 << 30): mods.append("nonmonotonic")
    base = v & ~((1 << 29) | (1 << 30))
    s = SCHED.get(base, f"sched#{base}")
    return s + (f"[{','.join(mods)}]" if mods else "")

# categorias de chamadas ao runtime
def classify(name):
    n = name
    if n in ("__kmpc_fork_call", "__kmpc_fork_call_if") or re.match(r"GOMP_parallel(_start|$|_loop_.*_start|_loop_.*$|_sections.*)", n):
        return "fork"
    if n == "__kmpc_fork_teams" or n.startswith("GOMP_teams"):
        return "teams"
    if re.match(r"__kmpc_(dist_)?for_static_init_", n) or re.match(r"__kmpc_dispatch_init_", n) \
            or re.match(r"GOMP_(parallel_)?loop_.*(start|_init)", n):
        return "ws_loop"
    if n.startswith("__kmpc_end_reduce"):
        return None
    if n.startswith("__kmpc_reduce") or n.startswith("GOMP_reduction"):
        return "reduce"
    if n in ("__kmpc_barrier", "__kmpc_cancel_barrier", "GOMP_barrier"):
        return "barrier"
    if n.startswith("__kmpc_critical") or n.startswith("GOMP_critical"):
        return "critical"
    if n in ("__kmpc_single", "GOMP_single_start"):
        return "single"
    if n in ("__kmpc_master", "__kmpc_masked"):
        return "master"
    if n.startswith("__kmpc_omp_task") or n.startswith("GOMP_task"):
        return "task"
    if n.startswith("__tgt_"):
        return "target"
    if n == "__kmpc_push_num_threads":
        return "num_threads"
    return None

def is_runtime(n):
    return n.startswith(("__kmpc_", "GOMP_", "__tgt_", "omp_"))

def generator_of(fn):
    if "_polly_subfn" in fn: return "polly"
    if ".omp_outlined" in fn or fn.startswith("__omp_offloading"): return "clang"
    if "._omp_fn." in fn: return "gcc"
    return "?"

# ---------------------------------------------------------------- IR parser
RE_DEF   = re.compile(r'^define\b[^@]*@("?)([^"(\s]+)\1\(')
RE_LABEL = re.compile(r'^([\w.$-]+):')
RE_CALL  = re.compile(r'\b(?:call|invoke)\b[^@]*?@("?)([\w.$]+)\1\((.*)')
RE_DBG   = re.compile(r'!dbg !(\d+)')
RE_DILOC = re.compile(r'^!(\d+) = (?:distinct )?!DILocation\(line: (\d+)(?:, column: (\d+))?')
RE_IDENT = re.compile(r'^@([\w.$]+) = .*%struct\.ident_t \{[^}]*ptr @([\w.$]+) \}')
RE_STR   = re.compile(r'^@([\w.$]+) = .*c"(.*)\\00"')
RE_LBLREF= re.compile(r'label %([\w.$-]+)')

def split_args(s):
    """Separa os argumentos de uma chamada no nível 0 de parênteses."""
    out, depth, cur = [], 0, ""
    for ch in s:
        if ch in "([{<":
            depth += 1
        elif ch in ")]}>":
            if depth == 0:
                out.append(cur.strip()); return out
            depth -= 1
        if ch == "," and depth == 0:
            out.append(cur.strip()); cur = ""
        else:
            cur += ch
    if cur.strip(): out.append(cur.strip())
    return out

def int_arg(a):
    m = re.search(r'(-?\d+)\s*$', a or "")
    return int(m.group(1)) if m and re.match(r'^i\d+ ', a) else None

def sym_arg(a):
    m = re.search(r'@("?)([\w.$]+)\1', a or "")
    return m.group(2) if m else None

def parse_ir(path):
    funcs = {}          # nome -> {"blocks": {label: [linhas]}, "order": [...]}
    diloc, idents, strs = {}, {}, {}
    cur = None
    with open(path, errors="replace") as f:
        for line in f:
            line = line.rstrip("\n")
            if cur is None:
                m = RE_DEF.match(line)
                if m:
                    cur = {"name": m.group(2), "blocks": {"__entry": []}, "order": ["__entry"]}
                    blk = "__entry"
                    continue
                m = RE_DILOC.match(line)
                if m: diloc[m.group(1)] = (int(m.group(2)), int(m.group(3) or 0)); continue
                m = RE_IDENT.match(line)
                if m: idents[m.group(1)] = m.group(2); continue
                m = RE_STR.match(line)
                if m: strs[m.group(1)] = m.group(2); continue
            else:
                if line.startswith("}"):
                    funcs[cur["name"]] = cur; cur = None; continue
                m = RE_LABEL.match(line)
                if m:
                    blk = m.group(1); cur["blocks"][blk] = []; cur["order"].append(blk); continue
                cur["blocks"][blk].append(line)
    # LLVM numera o bloco de entrada implicitamente; se o primeiro rótulo
    # explícito vier sem instruções antes, o __entry vazio é descartado.
    for fn in funcs.values():
        if not fn["blocks"]["__entry"]:
            del fn["blocks"]["__entry"]; fn["order"].remove("__entry")
    loc_of_ident = {k: strs.get(v, "") for k, v in idents.items()}
    return funcs, diloc, loc_of_ident

def blocks_in_cycles(fn):
    succ = {b: set(RE_LBLREF.findall("\n".join(lines))) for b, lines in fn["blocks"].items()}
    incyc = set()
    for b in succ:
        stack, seen = list(succ[b]), set()
        while stack:
            x = stack.pop()
            if x == b: incyc.add(b); break
            if x in seen or x not in succ: continue
            seen.add(x); stack.extend(succ[x])
    return incyc

def ir_calls(fn, diloc, loc_of_ident):
    """Lista de chamadas (callee, args, bloco, linha_fonte)."""
    out = []
    for b, lines in fn["blocks"].items():
        for ln in lines:
            m = RE_CALL.search(ln)
            if not m: continue
            callee = m.group(2)
            args = split_args(m.group(3))
            d = RE_DBG.search(ln)
            src = diloc.get(d.group(1)) if d else None
            if src is None and args:
                s = loc_of_ident.get(sym_arg(args[0]) or "", "")
                p = s.split(";")
                if len(p) > 4 and p[3].isdigit() and p[3] != "0":
                    src = (int(p[3]), int(p[4]) if p[4].isdigit() else 0)
            out.append({"callee": callee, "args": args, "block": b, "src": src})
    return out

# ------------------------------------------------------------- binary parser
RE_OD_FUNC = re.compile(r'^[0-9a-f]+ <([^>]+)>:')
RE_OD_INS  = re.compile(r'^\s*([0-9a-f]+):\s+(.*)$')
RE_OD_TGT  = re.compile(r'<([\w.$]+?)(?:@plt)?(?:\+0x[0-9a-f]+)?>')

def parse_binary(path):
    r = subprocess.run(["objdump", "-dr", "--no-show-raw-insn", "-M", "intel", path],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"objdump falhou em {path}: {r.stderr.strip()[:200]}")
    # tabela de símbolos para resolver relocações do tipo .text+0x1ec
    syms = defaultdict(dict)
    t = subprocess.run(["objdump", "-t", path], capture_output=True, text=True).stdout
    for line in t.splitlines():
        m = re.match(r'^([0-9a-f]+)\s+\S+\s+F\s+(\S+)\s+[0-9a-f]+\s+(\S+)$', line)
        if m: syms[m.group(2)][int(m.group(1), 16)] = m.group(3)
    funcs, cur = {}, None
    for line in r.stdout.splitlines():
        m = RE_OD_FUNC.match(line)
        if m:
            cur = m.group(1).split("@")[0]; funcs[cur] = []; continue
        if cur is None: continue
        rr = re.search(r'R_X86_64_(\w+)\s+([\w.$@]+?)([+-]0x[0-9a-f]+)?$', line)
        if rr and funcs[cur]:
            sym, add = rr.group(2).split("@")[0], int(rr.group(3) or "0", 16)
            if sym in syms:          # símbolo de seção: resolve pelo endereço
                pc = 4 if rr.group(1) in ("PC32", "PLT32") else 0
                sym = syms[sym].get(add + pc, f"{sym}+{add + pc:#x}")
            funcs[cur][-1]["reloc"] = sym; continue
        m = RE_OD_INS.match(line)
        if m: funcs[cur].append({"ins": m.group(2)})
    return funcs

def _target(y):
    if y.get("reloc"): return y["reloc"]
    m = RE_OD_TGT.search(y["ins"])
    return m.group(1) if m and "+0x" not in y["ins"].split("<")[-1] else None

def _find_fn_ptr(ins, i, reg):
    """Procura para trás a origem do ponteiro de função em `reg` (lea/mov)."""
    for y in reversed(ins[:i]):
        u = y["ins"]
        m = re.match(r'(lea|mov)\s+(\w+),(.*)$', u)
        if not m or m.group(2) != reg: continue
        if m.group(1) == "lea": return _target(y)
        src = m.group(3).strip()
        if re.fullmatch(r'r\w+', src): reg = src; continue   # mov rdx,r15
        return _target(y)
    return None

def binary_calls(ins):
    """Recupera callee e (heuristicamente) microtask/schedule das instruções."""
    calls = []
    for i, x in enumerate(ins):
        t = x["ins"]
        if not t.startswith("call"): continue
        callee = x.get("reloc") or (RE_OD_TGT.search(t).group(1) if RE_OD_TGT.search(t) else None)
        if callee is None: continue
        sched = None
        for y in reversed(ins[max(0, i - 20):i]):
            if y["ins"].startswith("call"): break
            mm = re.match(r'mov\s+edx,(0x[0-9a-f]+|\d+)$', y["ins"])
            if mm: sched = int(mm.group(1), 0); break
        reg = "rdi" if callee.startswith("GOMP_parallel") else "rdx"
        micro = _find_fn_ptr(ins, i, reg) if classify(callee) in ("fork", "teams") else None
        args = [None, None, f"i32 {sched}" if sched is not None else None, f"ptr @{micro}" if micro else None]
        calls.append({"callee": callee, "args": args, "block": None, "src": None,
                      "_micro": micro, "_sched": sched})
    # O backend pode fundir vários sítios de __kmpc_fork_call num só (tail merge),
    # com a microtask chegando por registrador vindo de blocos diferentes. Toda
    # função outlined referenciada por lea numa função com fork vira uma região.
    if any(classify(c["callee"]) in ("fork", "teams") for c in calls):
        seen = {c["_micro"] for c in calls if c["_micro"]}
        for y in ins:
            if not y["ins"].startswith("lea"): continue
            f = _target(y)
            if f and f not in seen and generator_of(f) != "?":
                seen.add(f)
                calls.append({"callee": "__kmpc_fork_call", "args": [None, None, None, f"ptr @{f}"],
                              "block": None, "src": None, "_micro": f, "_sched": None})
    return calls

# ---------------------------------------------------------------- analysis
def microtask_of(call):
    n, a = call["callee"], call["args"]
    if "_micro" in call:
        return call["_micro"]
    if n.startswith("__kmpc_fork"):
        return sym_arg(a[2]) if len(a) > 2 else None
    if n.startswith("GOMP_"):
        return sym_arg(a[0]) if a else None
    return None

def ws_info(call):
    n, a = call["callee"], call["args"]
    width = "8" if re.search(r'_8u?$', n) else ("4" if re.search(r'_4u?$', n) else "?")
    if n.startswith("__kmpc_"):
        sched = call.get("_sched", int_arg(a[2]) if len(a) > 2 else None)
        chunk = int_arg(a[-1]) if a and a[-1] else None
        return {"rt": n, "sched": decode_sched(sched), "chunk": chunk, "iv_bits": {"4": 32, "8": 64}.get(width)}
    m = re.match(r'GOMP_(?:parallel_)?loop_(?:ull_)?(\w+?)_(?:start|init)', n)
    return {"rt": n, "sched": m.group(1) if m else "?", "chunk": None, "iv_bits": None}

def analyze(funcs_calls, cycles, fn_lines=None):
    """funcs_calls: nome -> lista de chamadas; cycles: nome -> blocos em ciclo."""
    defined = set(funcs_calls)
    micro = set()
    for calls in funcs_calls.values():
        for c in calls:
            if classify(c["callee"]) in ("fork", "teams"):
                mt = microtask_of(c)
                if mt: micro.add(mt)
    # tudo o que é alcançável a partir de uma microtask é "corpo de região"
    inner = set()
    stack = list(micro)
    while stack:
        f = stack.pop()
        if f in inner or f not in defined: continue
        inner.add(f)
        stack += [c["callee"] for c in funcs_calls[f] if c["callee"] in defined]

    def inner_reach(f0):
        seen, st = set(), [f0]
        while st:
            f = st.pop()
            if f in seen or f not in defined: continue
            seen.add(f); st += [c["callee"] for c in funcs_calls[f] if c["callee"] in defined]
        return seen

    def collect(fn, seen):
        """Eventos OpenMP dentro de fn (seguindo chamadas a funções definidas)."""
        ev = defaultdict(list)
        if fn in seen or fn not in defined: return ev
        seen.add(fn)
        for c in funcs_calls[fn]:
            k = classify(c["callee"])
            if k == "ws_loop": ev["ws_loop"].append(ws_info(c))
            elif k == "fork":
                ev["nested_fork"].append(microtask_of(c))
            elif k: ev[k].append(c["callee"])
            if c["callee"] in defined and not is_runtime(c["callee"]):
                for kk, vv in collect(c["callee"], seen).items(): ev[kk] += vv
        return ev

    report = {}
    for fn, calls in funcs_calls.items():
        if fn in inner: continue
        regions, extra = [], defaultdict(int)
        for c in calls:
            k = classify(c["callee"])
            if k in ("fork", "teams"):
                mt = microtask_of(c)
                ev = collect(mt, set()) if mt else {}
                regions.append({
                    "kind": "teams" if k == "teams" else "parallel",
                    "runtime": c["callee"],
                    "outlined": mt, "generator": generator_of(mt or ""),
                    "src_line": c["src"][0] if c["src"] else None,
                    # Polly não põe !dbg no fork: usa a 1ª linha do corpo outlined
                    "body_lines": body_range(mt, fn_lines, inner_reach(mt)) if fn_lines and mt else None,
                    "in_loop": (c["block"] in cycles.get(fn, set())) if c["block"] else None,
                    "ws_loops": ev.get("ws_loop", []),
                    **{key: len(ev.get(key, [])) for key in
                       ("reduce", "barrier", "critical", "single", "master", "task", "nested_fork")},
                })
            elif k:
                extra[k] += 1
        if regions or extra:
            report[fn] = {"regions": regions, "outside_regions": dict(extra)}
    return report

def body_range(mt, fn_lines, fns):
    ls = sorted({l for f in fns for l in fn_lines.get(f, ())})
    return (ls[0], ls[-1]) if ls else None

# ---------------------------------------------------------------- output
def print_report(path, rep):
    print(f"=== {path}")
    if not rep:
        print("  (nenhuma chamada ao runtime OpenMP)"); return
    for fn, r in rep.items():
        print(f"  {fn}: {len(r['regions'])} região(ões) paralela(s)")
        for i, g in enumerate(r["regions"], 1):
            where = f"linha {g['src_line']}" if g["src_line"] else "linha ?"
            if g.get("body_lines"): where += f" (corpo: linhas {g['body_lines'][0]}-{g['body_lines'][1]})"
            loop = {True: "DENTRO de laço (fork repetido)", False: "fora de laço", None: "laço ?"}[g["in_loop"]]
            print(f"    [{i}] {g['runtime']} -> {g['outlined']} ({g['generator']}); {where}; {loop}")
            for w in g["ws_loops"]:
                ch = f", chunk={w['chunk']}" if w["chunk"] not in (None, 0, 1) else ""
                bits = f", iv={w['iv_bits']} bits" if w["iv_bits"] else ""
                print(f"        laço: {w['sched']}{ch}{bits}  [{w['rt']}]")
            flags = [f"{k}={g[k]}" for k in ("reduce", "barrier", "critical", "single", "master", "task", "nested_fork") if g[k]]
            if flags: print("        " + ", ".join(flags))
        if r["outside_regions"]:
            print("    fora de regiões: " + ", ".join(f"{k}={v}" for k, v in r["outside_regions"].items()))

def rows(path, rep):
    for fn, r in rep.items():
        regs = r["regions"]
        ws = [w for g in regs for w in g["ws_loops"]]
        yield {
            "file": os.path.basename(path), "function": fn,
            "generators": "|".join(sorted({g["generator"] for g in regs})),
            "parallel_regions": len(regs),
            "regions_in_loop": ("?" if regs and all(g["in_loop"] is None for g in regs)
                                else sum(1 for g in regs if g["in_loop"])),
            "ws_loops": len(ws),
            "schedules": "|".join(sorted({w["sched"] for w in ws})),
            "iv_bits": "|".join(sorted({str(w["iv_bits"]) for w in ws if w["iv_bits"]})),
            "reductions": sum(g["reduce"] for g in regs),
            "barriers": sum(g["barrier"] for g in regs),
            "critical": sum(g["critical"] for g in regs),
            "single_master": sum(g["single"] + g["master"] for g in regs),
            "tasks": sum(g["task"] for g in regs),
            "target_calls": r["outside_regions"].get("target", 0),
            "src_lines": "|".join(str(g["src_line"]) for g in regs if g["src_line"]),
            "body_lines": "|".join(f"{g['body_lines'][0]}-{g['body_lines'][1]}" for g in regs if g.get("body_lines")),
        }

def print_compare(all_rows):
    """Agrupa por kernel (prefixo do arquivo) e mostra uma linha por variante."""
    by = defaultdict(list)
    for r in all_rows:
        parts = r["file"].split(".")
        kernel, var = parts[0], (parts[1] if len(parts) > 2 else "-")
        by[kernel].append((var, r))
    hdr = f"{'kernel':<16}{'variante':<14}{'função':<28}{'reg':>4}{'loop':>5}{'ws':>4}{'red':>4}{'bar':>4}  {'schedule':<24}{'iv':<6}linhas"
    print("\n" + hdr + "\n" + "-" * len(hdr))
    for kernel in sorted(by):
        for var, r in sorted(by[kernel], key=lambda x: x[0]):
            lines = r["src_lines"] or r["body_lines"]
            print(f"{kernel:<16}{var:<14}{r['function'][:27]:<28}{r['parallel_regions']:>4}{r['regions_in_loop']:>5}"
                  f"{r['ws_loops']:>4}{r['reductions']:>4}{r['barriers']:>4}  {r['schedules'][:23]:<24}{r['iv_bits']:<6}{lines}")
        print()

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("files", nargs="+")
    ap.add_argument("--binary", action="store_true", help="entradas são objetos/binários (objdump)")
    ap.add_argument("--csv"); ap.add_argument("--json")
    ap.add_argument("--quiet", action="store_true", help="não imprime o relatório em texto")
    ap.add_argument("--compare", action="store_true",
                    help="tabela lado a lado por kernel; arquivos nomeados <kernel>.<variante>.ll|.o")
    a = ap.parse_args()

    all_rows, all_json = [], {}
    for p in a.files:
        binary = a.binary or not p.endswith(".ll")
        if binary:
            fc = {fn: binary_calls(ins) for fn, ins in parse_binary(p).items()}
            cyc = {}
        else:
            funcs, diloc, loc = parse_ir(p)
            fc = {n: ir_calls(f, diloc, loc) for n, f in funcs.items()}
            cyc = {n: blocks_in_cycles(f) for n, f in funcs.items()}
            fl = {n: {diloc[d][0] for ls in f["blocks"].values() for ln in ls
                      for d in RE_DBG.findall(ln) if d in diloc and diloc[d][0] > 0}
                  for n, f in funcs.items()}
        rep = analyze(fc, cyc, fl if not binary else None)
        all_json[p] = rep
        all_rows += list(rows(p, rep))
        if not a.quiet: print_report(p, rep)

    if a.compare:
        print_compare(all_rows)
    if a.csv:
        with open(a.csv, "w", newline="") as f:
            fields = list(all_rows[0].keys()) if all_rows else ["file", "function"]
            w = csv.DictWriter(f, fieldnames=fields); w.writeheader(); w.writerows(all_rows)
    if a.json:
        with open(a.json, "w") as f: json.dump(all_json, f, indent=2, ensure_ascii=False)

if __name__ == "__main__":
    main()
