# Clang + Polly + libomp do LLVM 21, compilados da mesma revisão
FROM ubuntu:24.04

ARG LLVM_TAG=llvmorg-21.1.8
ARG JOBS=0

RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        build-essential cmake ninja-build python3 git ca-certificates zlib1g-dev libzstd-dev \
    && rm -rf /var/lib/apt/lists/*

RUN git clone --depth 1 --branch ${LLVM_TAG} https://github.com/llvm/llvm-project.git /src/llvm-project

RUN cmake -S /src/llvm-project/llvm -B /build -G Ninja \
        -DCMAKE_BUILD_TYPE=Release \
        -DCMAKE_INSTALL_PREFIX=/opt/llvm21 \
        -DLLVM_ENABLE_PROJECTS="clang;polly" \
        -DLLVM_ENABLE_RUNTIMES="openmp" \
        -DLLVM_POLLY_LINK_INTO_TOOLS=ON \
        -DLLVM_TARGETS_TO_BUILD="X86" \
        -DLLVM_ENABLE_ASSERTIONS=OFF \
        -DLLVM_INCLUDE_TESTS=OFF -DLLVM_INCLUDE_EXAMPLES=OFF -DLLVM_INCLUDE_BENCHMARKS=OFF \
    && J=$([ "${JOBS}" = "0" ] && nproc || echo ${JOBS}) \
    && ninja -C /build -j"$J" install \
    && rm -rf /build /src

ENV PATH=/opt/llvm21/bin:$PATH \
    LD_LIBRARY_PATH=/opt/llvm21/lib:/opt/llvm21/lib/x86_64-unknown-linux-gnu

WORKDIR /work
CMD ["bash"]
