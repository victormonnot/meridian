# SPDX-License-Identifier: GPL-3.0-or-later
FROM ubuntu:24.04@sha256:68ee22004a9109b44973313b02ff7b1de250f1de0f5ecb628d73f579aaff33ac AS dependencies

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    build-essential=12.10ubuntu1 \
    cmake=3.28.3-1build7 \
    git=1:2.43.0-1ubuntu7.3 \
    libopencv-dev=4.6.0+dfsg-13.1ubuntu1 \
    libeigen3-dev=3.4.0-4build0.1 \
    libceres-dev=2.2.0+dfsg-4.1ubuntu2 \
    libboost-system-dev=1.83.0.1ubuntu2 \
    libboost-filesystem-dev=1.83.0.1ubuntu2 \
    libboost-thread-dev=1.83.0.1ubuntu2 \
    libboost-date-time-dev=1.83.0.1ubuntu2 \
    && rm -rf /var/lib/apt/lists/*

FROM dependencies AS builder
RUN git init /opt/openvins \
    && git -C /opt/openvins remote add origin https://github.com/rpng/open_vins.git \
    && git -C /opt/openvins fetch --depth 1 origin 69488123ed9362dd44b6f28e7f4680abbff1442b \
    && git -C /opt/openvins checkout --detach FETCH_HEAD \
    && test "$(git -C /opt/openvins rev-parse HEAD)" = 69488123ed9362dd44b6f28e7f4680abbff1442b
COPY CMakeLists.txt runner.cpp LICENSE /opt/meridian/adapter/
RUN cmake -S /opt/meridian/adapter -B /opt/meridian/build \
      -DCMAKE_BUILD_TYPE=Release -DOPENVINS_SOURCE=/opt/openvins \
    && cmake --build /opt/meridian/build --parallel 2 \
    && cmake --install /opt/meridian/build \
    && dpkg-query -W -f='${Package}\t${Version}\n' > /opt/meridian/dependency-versions.txt \
    && /usr/local/bin/meridian-openvins --version > /opt/meridian/build-info.json \
    && sha256sum /usr/local/bin/meridian-openvins /usr/local/lib/libmeridian_openvins.so \
      > /opt/meridian/binary-sha256.txt
COPY config /opt/meridian/adapter/config
COPY README.md NOTICE.md smoke_test.py Dockerfile /opt/meridian/adapter/

# Upstream bundles benchmark reference trajectories in ov_data and Git objects.
# A fresh runtime stage includes only the needed estimator sources and binaries,
# so those references cannot be reached by the estimator process.
FROM dependencies AS reference
COPY --from=builder /usr/local/bin/meridian-openvins /usr/local/bin/meridian-openvins
COPY --from=builder /usr/local/lib/libmeridian_openvins.so /usr/local/lib/libmeridian_openvins.so
COPY --from=builder /opt/meridian/adapter /opt/meridian/adapter
COPY --from=builder /opt/meridian/build-info.json /opt/meridian/dependency-versions.txt /opt/meridian/binary-sha256.txt /opt/meridian/
COPY --from=builder /opt/openvins/ov_core /opt/openvins/ov_core
COPY --from=builder /opt/openvins/ov_init /opt/openvins/ov_init
COPY --from=builder /opt/openvins/ov_msckf /opt/openvins/ov_msckf
COPY --from=builder /opt/openvins/LICENSE /opt/openvins/LICENSE
ENV OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1
WORKDIR /tmp
ENTRYPOINT ["/usr/local/bin/meridian-openvins"]
