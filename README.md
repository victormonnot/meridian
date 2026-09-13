# Meridian

Meridian is an experimental state-estimation and sensor-fusion project. Its first
study targets roll angle and residual gyroscope bias using gyroscope and
accelerometer measurements, with reproducible simulation and offline replay of
real drone bench data.

**Implemented:** Python gyro integration, accelerometer tilt, a complementary
filter, and an angle/bias Kalman reference, with controlled simulation,
reproducible exports, and numerical tests. On the selected nominal simulation,
angle RMSE is 8.854° for gyro integration, 0.660° for the complementary filter, and
0.354° for Kalman. A paired translation disturbance exposes failure of the
gravity-based observation model. The EKF, C++, bench replay, and web interface
remain planned.

![Nominal and disturbed gyro–accelerometer fusion](results/accelerometer-fusion/overview.png)

The latest experiment combines 100 Hz gyro intervals with tilt derived from 10 Hz
accelerometer components. All methods share measurements and a known initial angle.
The [fusion report](results/accelerometer-fusion/README.md) includes 20 additional
paired noise seeds, disturbance recovery, and covariance limitations.
The noise and timing models are controlled assumptions, not identified sensor characteristics.

The earlier [gyro-drift baseline](results/gyro-drift/README.md) isolates integration
error from bias and noise, and the [linear reference](results/linear-kalman/README.md)
uses direct synthetic angle observations. All experiments use **mean rates over simulation
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
python -m meridian.accel_experiment --output outputs/accelerometer-fusion
python -m pytest -q
```

On Windows with Python 3.12 installed, create the environment with
`py -3.12 -m venv .venv` and run the remaining `python -m ...` commands using
`.venv\Scripts\python.exe` in place of `python`.

Each experiment exports separate measurements, truth, and estimates, plus a JSON
summary and PNG plot. Fusion experiments also export covariance and innovation
diagnostics and evaluate seeds 0–19. Use a **new output directory** for each run;
existing directories are refused. Generated runs under `outputs/` are ignored by
Git. Selected figures and summaries under `results/` document the default runs.

For a stationary scenario or a different noise realization:

```sh
python -m meridian.gyro_drift --amplitude-deg 0 --output outputs/stationary
python -m meridian.gyro_drift --seed 7 --output outputs/seed-7
python -m meridian.kalman_experiment --help
python -m meridian.accel_experiment --help
```

## Design and next steps

| Location | Responsibility |
| --- | --- |
| `src/meridian/simulation.py` | Roll truth and gyro, angle, and specific-force measurements. |
| `src/meridian/integration.py` | Gyro integration from interval rates and timestamps. |
| `src/meridian/kalman.py` | Linear angle/bias prediction and correction; no file or plotting dependencies. |
| `src/meridian/tilt.py` | Tilt extraction, angular wrapping, and complementary roll estimation. |
| `src/meridian/gyro_drift.py` | Experiment configuration, evaluation, plots, and exports. |
| `src/meridian/kalman_experiment.py` | Shared-data comparison, sparse angle observations, and repeated trials. |
| `src/meridian/accel_experiment.py` | Nominal/disturbed comparisons using shared simulated IMU data. |
| `tests/` | Numerical contracts, analytical drift, reproducibility, and export checks. |
| `results/` | Selected results and reproduction reports. |

Next come current bench-data inspection and a nonlinear accelerometer-vector
reference, followed by C++ verification. A lightweight web interface will expose comparison plots and
time-based replay after the first filter comparisons exist. Its technology stack
is undecided; the numerical core works independently of the presentation layer.

The [linear Kalman model](docs/linear-kalman.md) and [accelerometer fusion model](docs/accelerometer-fusion.md)
specify timing, noise, initialization, and limitations.
The [design and validation approach](docs/design.md) defines the broader models,
assumptions, software boundaries, and remaining decisions. Real bench acquisition
and replay are part of completing the study: the drone remains disarmed, with
propellers removed, and Meridian stays outside the flight-control loop. No
embedded, real-time, or flight validation is claimed. Additional estimation tasks
and integrations are possible future directions.
