# Meridian

Meridian is an experimental state-estimation and sensor-fusion project. Its first
study targets roll angle and residual gyroscope bias using gyroscope and
accelerometer measurements, with reproducible simulation and offline replay of
real drone bench data.

**Implemented:** Python gyro integration and a linear Kalman reference for roll
and constant gyro bias, controlled simulation, reproducible CSV/JSON exports,
plots, and numerical tests. On the selected 30 s simulation, Kalman reduces angle
RMSE from 8.854° to 0.487° and estimates 0.519°/s for a true bias of 0.5°/s.
The reference uses a direct synthetic angle observation. Accelerometer fusion,
C++, bench replay, and the web interface remain planned.

![Gyro integration and Kalman angle/bias estimates](results/linear-kalman/overview.png)

The Kalman experiment combines 100 Hz gyro intervals with 10 Hz noisy angle
observations. Both methods receive the same gyro measurements and known initial
angle. Results across 20 additional noise seeds are included in the
[Kalman report](results/linear-kalman/README.md). Noise parameters match the simulator;
these results do not establish physical sensor accuracy.

The earlier [gyro-drift baseline](results/gyro-drift/README.md) isolates integration
error from bias and noise. Both experiments use **mean rates over simulation
intervals**, so the ideal case reconstructs the trajectory up to roundoff by
construction. This is not an established sample convention for real logs.

## Run the experiment

The pinned environment was verified with **Python 3.12 on Linux**. From the
repository root:

```sh
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.lock -e .
python -m meridian.gyro_drift --output outputs/gyro-drift
python -m meridian.kalman_experiment --output outputs/linear-kalman
python -m pytest -q
```

On Windows with Python 3.12 installed, create the environment with
`py -3.12 -m venv .venv` and run the remaining `python -m ...` commands using
`.venv\Scripts\python.exe` in place of `python`.

Each experiment exports separate measurements, truth, and estimates, plus a JSON
summary and PNG plot. The Kalman experiment also exports covariance and innovation
diagnostics and evaluates seeds 0–19. Use a **new output directory** for each run;
existing directories are refused. Generated runs under `outputs/` are ignored by
Git. Selected figures and summaries under `results/` document the default runs.

For a stationary scenario or a different noise realization:

```sh
python -m meridian.gyro_drift --amplitude-deg 0 --output outputs/stationary
python -m meridian.gyro_drift --seed 7 --output outputs/seed-7
python -m meridian.kalman_experiment --help
```

## Design and next steps

| Location | Responsibility |
| --- | --- |
| `src/meridian/simulation.py` | Roll truth and idealized gyro measurement generation. |
| `src/meridian/integration.py` | Gyro integration from interval rates and timestamps. |
| `src/meridian/kalman.py` | Linear angle/bias prediction and correction; no file or plotting dependencies. |
| `src/meridian/gyro_drift.py` | Experiment configuration, evaluation, plots, and exports. |
| `src/meridian/kalman_experiment.py` | Shared-data comparison, sparse angle observations, and repeated trials. |
| `tests/` | Numerical contracts, analytical drift, reproducibility, and export checks. |
| `results/` | Selected results and reproduction reports. |

Next comes accelerometer simulation and tilt-based fusion, a complementary
baseline, and a C++ EKF with a justified nonlinear
measurement model. A lightweight web interface will expose comparison plots and
time-based replay after the first filter comparisons exist. Its technology stack
is undecided; the numerical core works independently of the presentation layer.

The [linear Kalman model](docs/linear-kalman.md) specifies timing, noise, and
initialization. The [design and validation approach](docs/design.md) defines the broader models,
assumptions, software boundaries, and remaining decisions. Real bench acquisition
and replay are part of completing the study: the drone remains disarmed, with
propellers removed, and Meridian stays outside the flight-control loop. No
embedded, real-time, or flight validation is claimed. Additional estimation tasks
and integrations are possible future directions.
