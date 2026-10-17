# Third-party notices

The adapter source and build integration in this directory are distributed under
**GPL-3.0-or-later**, with the full GPL version 3 text in [LICENSE](LICENSE).
They link to OpenVINS, rather than implementing a separate estimator.

OpenVINS: An Open Platform for Visual-Inertial Research.
Copyright (C) 2018–2023 Patrick Geneva, Guoquan Huang, OpenVINS Contributors;
Copyright (C) 2018–2019 Kevin Eckenhoff. Individual upstream files retain their
own copyright notices. Upstream source and license:
<https://github.com/rpng/open_vins/tree/69488123ed9362dd44b6f28e7f4680abbff1442b>.

The three YAML files and two PNG camera masks in `config/` come from that
revision's `config/tum_vi/`. Meridian changes the estimator configuration's
verbosity and threading, removes unused ROS trajectory/reference output paths,
and adds provenance comments. The calibration YAML
files and PNGs are copied unchanged. The remaining baseline filter/calibration
parameters, including static initialization, remain unchanged. No ground-truth
initialization or reference path is added. The upstream source is not patched.

The Docker image retains the upstream `ov_core`, `ov_init` and `ov_msckf` source
directories used in this build, adapter sources, license and dependency manifest
under `/opt/openvins` and `/opt/meridian`. Benchmark reference data, unrelated
evaluation examples and Git objects from the upstream checkout are excluded
from the runtime image. The Docker recipe fetches the full pinned checkout in
its separate build stage. Ubuntu,
Eigen, OpenCV, Boost, Ceres and their dependencies carry separate licenses;
installed Debian package notices remain under `/usr/share/doc` in the image.
Distributing a built image or executable requires preserving the applicable
notices and corresponding-source obligations; a hosted service alone is not
the same distribution artifact as a downloadable binary.

Dataset attribution and terms belong to the separately acquired TUM-VI data;
no dataset images, IMU samples or motion-capture references are included here.
