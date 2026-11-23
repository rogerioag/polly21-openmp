/* pbsize.h — tamanhos do PolyBench/C lidos em tempo de execução.
 *
 * Incluído antes do fonte (-include) junto com o sizes/<kernel>.h gerado pelo
 * gen_sizes.py, que define NI, NJ, N, TSTEPS... como chamadas a pb_size().
 *
 * Em tempo de execução:
 *   PB_DATASET=MINI|SMALL|MEDIUM|LARGE|EXTRALARGE   (padrão: LARGE)
 *   <NOME>=valor  sobrepõe uma dimensão específica, ex.: NI=3000 TSTEPS=50
 */
#ifndef PBSIZE_H
#define PBSIZE_H
#include <stdlib.h>
#include <string.h>
#include <stdio.h>

static inline __attribute__((unused))
int pb_size(const char *name, int mini, int small, int medium, int large, int xlarge)
{
  const char *v = getenv(name);
  if (v && *v) return atoi(v);
  const char *ds = getenv("PB_DATASET");
  if (!ds || !*ds)                      return large;
  if (!strcasecmp(ds, "MINI"))          return mini;
  if (!strcasecmp(ds, "SMALL"))         return small;
  if (!strcasecmp(ds, "MEDIUM"))        return medium;
  if (!strcasecmp(ds, "LARGE"))         return large;
  if (!strcasecmp(ds, "EXTRALARGE"))    return xlarge;
  fprintf(stderr, "PB_DATASET inválido: %s\n", ds);
  exit(2);
}
#endif
