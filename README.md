# polly21-openmp

Comparação da paralelização automática do **Polly (LLVM 21)** com código OpenMP
anotado por outras ferramentas, como o **DawnCC**, e com versões escritas à mão,
como o **PolyBench-ACC/OpenMP**, usando os kernels do **PolyBench/C 4.2.1**.

A comparação é feita de duas formas:

1. **Desempenho**: tempos de execução de cada variante.
2. **Estrutura do código gerado**: recuperação "reversa" das construções OpenMP
   a partir das chamadas à ABI da libomp (`__kmpc_fork_call`,
   `__kmpc_for_static_init_*`, `__kmpc_dispatch_init_*`, `__kmpc_reduce`...),
   no LLVM IR ou no binário.

## Arquivos

| Arquivo | Função |
|---|---|
| `Dockerfile` | Compila clang + Polly + libomp do LLVM 21 (`llvmorg-21.1.8`) a partir da mesma revisão |
| `run_polybench_polly.sh` | Compila e cronometra cada kernel em 3 variantes (`base`, `polly`, `polly-omp`) e guarda IR, assembly e manifest de cada execução |
| `run_variant.sh` | Compila e executa outra árvore já anotada (DawnCC, PolyBench-ACC) com os mesmos datasets e tamanhos, na mesma pasta de resultados |
| `runtime-sizes/` | `pbsize.h` + `gen_sizes.py`: tamanhos do PolyBench lidos em tempo de execução |
| `emit_ir.sh` | Gera `.ll` e `.o` de cada kernel de uma árvore de fontes, rotulados por variante |
| `ompscan.py` | Lê `.ll`/`.o`/binários e reconstrói as regiões paralelas, os laços worksharing, o schedule, as reduções e as barreiras |

## 1. Ambiente

```bash
docker build -t polly21 .
docker run --rm -it -v ~/PolyBenchC-4.2.1:/work/pb -v $PWD:/work/proj -w /work/proj polly21
```

No container, os scripts ficam em `/work/proj` (a pasta do projeto no host). O
`-w /work/proj` já abre o shell ali, e os resultados ficam gravados no host.

O build do LLVM é demorado. Rode-o dentro do `tmux` ou do `screen` no servidor.

## 2. Desempenho

```bash
./run_polybench_polly.sh /work/pb "MEDIUM LARGE EXTRALARGE" 16 5
TAG=sem-pattern ./run_polybench_polly.sh /work/pb LARGE 16 5   # rótulo na pasta
```

### Variar o dataset só na execução

Por padrão (`SIZES=runtime`), cada kernel é compilado **uma vez**. Os tamanhos
são lidos em tempo de execução: o `gen_sizes.py` transforma cada `NI`, `N`,
`TSTEPS`... do `<kernel>.h` em `pb_size("NI", mini, small, medium, large, xl)`,
e a compilação usa `-DPOLYBENCH_USE_C99_PROTO`.

```bash
PB_DATASET=EXTRALARGE ./results/latest/bin/gemm.polly-omp   # um dataset do PolyBench
NI=3000 NJ=3000 NK=3000 ./results/latest/bin/gemm.polly-omp  # dimensões livres
```

Validado: nos 30 kernels, a saída de `-DPOLYBENCH_DUMP_ARRAYS` é idêntica à da
versão com tamanhos fixos (MINI).

**Atenção:** com tamanhos em tempo de execução, o compilador vê parâmetros em
vez de constantes. O Polly passa a gerar código paramétrico, com checagens em
tempo de execução, e pode tomar decisões diferentes. Para o comportamento
original do PolyBench (um binário por dataset), use `SIZES=fixed`. Compare
DawnCC e PolyBench-ACC sempre no mesmo modo.

### Outras ferramentas com os mesmos datasets

Depois do `run_polybench_polly.sh`, acrescente as outras variantes à mesma
execução (`results/latest`):

```bash
./run_variant.sh dawncc  ~/dawncc-out           -fopenmp
./run_variant.sh acc-omp ~/PolyBench-ACC/OpenMP -fopenmp
```

O script lê do `manifest.txt` os datasets, as threads e as repetições. Os
tamanhos vêm do PolyBench/C 4.2.1 de referência, então `LARGE` tem as mesmas
dimensões em todas as variantes. Isso importa no PolyBench-ACC, que é baseado no
PolyBench 3.2, tem outros tamanhos e chama o dataset médio de STANDARD.

Kernels do ACC sem correspondente no 4.2.1 (convolution-2d/3d, dynprog,
fdtd-apml, reg_detect), ou com outras dimensões (atax, bicg: NX/NY; gemm-like
symm, syrk, syr2k, trmm, gramschmidt: NI/NJ), usam os próprios tamanhos. Esses
casos ficam listados em `sizes-<variante>.avisos.txt`. Para forçar as
dimensões, passe-as por variável de ambiente, ex.: `NX=1900 NY=2100`.

**PolyBench-ACC/OpenMP:** 10 kernels não compilam no clang porque têm OpenMP
inválido. Em adi, dynprog, fdtd-2d, jacobi-1d/2d-imper, reg_detect, seidel-2d e
trisolv há `omp for` dentro de `omp master`; em ludcmp há aninhamento inválido
de `for`; em floyd-warshall, `shared` num `omp for`.

### O que cada execução guarda

```
results/<data-hora>[-TAG]/
  manifest.txt        compilador, flags de cada variante, datasets, threads, CPU, commit
  times.csv           kernel,variant,dataset,sizes,threads,rep,seconds
  omp-structure.csv   regiões paralelas recuperadas pelo ompscan.py
  ir/   asm/          LLVM IR (-g, sem inlining) e assembly de cada kernel/variante
  bin/  remarks/  sizes/
results/latest -> última execução
```

### Guardar os artefatos

`bin/`, `ir/` e `asm/` ficam fora do git. Para guardar o que o Polly gerou junto
com o commit que o produziu:

```bash
R=results/20261123-160000-exemplo
tar -C results -cJf $(basename $R).tar.xz $(basename $R)
gh release create $(basename $R) $(basename $R).tar.xz -n "$(cat $R/manifest.txt)"
```

## 3. Estrutura OpenMP gerada

```bash
FLAGS_POLLY="-mllvm -polly -mllvm -polly-parallel -mllvm -polly-omp-backend=LLVM -mllvm -polly-scheduling=static"

./emit_ir.sh polly-omp ~/PolyBenchC-4.2.1     ~/PolyBenchC-4.2.1/utilities $FLAGS_POLLY
./emit_ir.sh dawncc    ~/dawncc-out            ~/PolyBenchC-4.2.1/utilities -fopenmp
./emit_ir.sh acc-omp   ~/PolyBench-ACC/OpenMP  ~/PolyBench-ACC/common        -fopenmp

./ompscan.py --quiet --compare --csv results/estrutura/omp.csv results/estrutura/ir/*.ll   # IR (preferido)
./ompscan.py --quiet --compare results/estrutura/obj/*.o                       # binários
./ompscan.py results/estrutura/ir/gemm.polly-omp.ll                            # relatório detalhado
```

Os arquivos de saída devem se chamar `<kernel>.<variante>.ll|.o`, pois o
`--compare` agrupa por esse padrão.

### Colunas do `--compare` / CSV

- `reg`: regiões paralelas (`__kmpc_fork_call`)
- `loop`: regiões com o fork **dentro de um laço**, ou seja, repetido a cada iteração externa (`?` em binário)
- `ws`: laços worksharing; `schedule`: static / dynamic / runtime...; `iv`: largura da variável de indução (32/64 bits)
- `red`, `bar`: reduções e barreiras explícitas
- `linhas`: linha do fork (clang com `-g`) ou faixa de linhas do corpo outlined (Polly)

## Assinaturas para distinguir os geradores

| | Polly | clang a partir de `#pragma` |
|---|---|---|
| Função outlined | `kernel_x_polly_subfn_N` | `kernel_x.omp_outlined` (DawnCC/`target`: `__omp_offloading_…`) |
| Laço | `*_init_8` (iv de 64 bits) | `*_init_4` quando o iv é `int` |
| Schedule padrão | `runtime` (37), salvo `-polly-scheduling=static` | `static` (34) |
| Linha do fonte | o fork não tem `!dbg` | `!dbg` com `-g` |
| Reduções/barreiras | não gera | `__kmpc_reduce`, `__kmpc_barrier` |

## Cuidados

- **`-fno-inline-functions`** (já está no `emit_ir.sh`): sem ele, os kernels
  `static` são inlinados no `main`.
- **gemm**: o Polly aplica a otimização por casamento de padrão de multiplicação
  de matrizes e **não paraleliza**. Use `-mllvm -polly-pattern-matching-based-opts=false`
  para ver a versão paralela.
- **Stencils com laço de tempo** (jacobi-2d etc.): o escalonador do Polly pode
  não deixar nenhum laço externo paralelo. Com `-mllvm -polly-optimizer=none`,
  ele paraleliza os laços internos, como fazem os pragmas. A ausência de regiões
  também é um resultado.
- O Polly pode gerar **mais regiões** que os pragmas, por causa das versões com
  checagem de alias em tempo de execução e da distribuição de laços.
- **DawnCC** compilado sem `-fopenmp-targets`: o `#pragma omp target` vira código
  no host, sem chamadas `__tgt_*`.
- `-polly-num-threads` insere `__kmpc_push_num_threads`. Para comparar
  estrutura, prefira `OMP_NUM_THREADS`.
- A análise é **estática**: conta os pontos de fork, não as execuções. Para
  contagens dinâmicas, o próximo passo seria uma ferramenta OMPT.
