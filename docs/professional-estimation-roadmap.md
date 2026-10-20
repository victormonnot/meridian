# Professional state-estimation workspace

**Status: development roadmap, 2026-10-20.** The product direction prioritizes
professional state-estimation engineering with visual-inertial odometry (VIO)
and operation without reliable GNSS. Part 1's bounded hosted workflow is
complete; part 2 has started with sensor contracts, and parts 3–8 remain planned.
Part 1 has a [detailed implementation record](part-1-hosted-vio-workspace.md).
Part 2 has a [detailed increment plan](part-2-sensor-data-calibration-timing.md)
and initial [sensor contracts](sensor-contract.md);
parts 3–8 remain at outcome level. The [README](../README.md) and
[design notes](design.md) describe the implementation available today. The first
[local VIO reference](vio-reference.md) is implemented and qualified, and a
[private project workspace](hosted-workspace.md) is deployed.
[Durable VIO computation](hosted-vio-jobs.md) implements execution of maintained
reference jobs. The [complete acceptance report](../results/hosted-acceptance/README.md)
records the full workflow, off-server recovery and independent replay, while
preserving the reserved room2 coverage failure. User acquisitions, general
estimator integration/development and AI engineering agents remain planned.

## Product objective

Meridian should help estimation engineers build, investigate, improve, and
evaluate the state-estimation systems used in autonomous drones. VIO and
estimation when GNSS is absent, unreliable, or interrupted are central use cases.
The intended product is a hosted engineering workspace, optimized for professional work.
Its outputs should include executable estimator changes, configurations,
calibration artifacts, reproducible experiments, and evidence for engineering
decisions. Product examples should serve as benchmarks and integration fixtures.

The long-term ambition is to automate increasingly complete engineering tasks:

```text
Requirements + sensor data + estimator
  -> reproducible baseline
  -> candidate models, configurations, or implementation changes
  -> experiments and independent evaluation
  -> reusable implementation and evidence
```

The initial target user is an engineer developing a visual-inertial navigation
pipeline from camera and IMU recordings, with an existing or new codebase.
GNSS availability must not be required by the data or execution architecture.
GNSS can remain an optional aid and a source for transition/comparison studies.
Supporting broader teams requires interoperability across sensor setups and
estimator implementations. Universal compatibility is an objective to work
toward, not a release claim.

The scope is estimation and localization engineering. Mission planning,
obstacle avoidance, and flight control are downstream integrations rather than
additional product workstreams. Supporting navigation without GNSS does not
imply drift-free odometry or a globally referenced position under all conditions.

The product's value must be measured against the engineer's existing workflow:
time to investigate a regression, time to integrate a sensor model, quality of
candidate changes, reproducibility, and failure detection. Replacing substantial
manual tasks is a testable objective. Replacing an entire engineering role is an
unproven ambition, not a promised capability.

## Engineering workflows

### Develop or extend an estimator

Inputs include the intended state, sensor models, coordinate frames, timing
semantics, existing implementation, data, and numerical/runtime requirements.
The engineer adds or changes an observation model, calibration state, propagation
model, or update policy. Meridian should support explicit model definitions,
derivative generation where appropriate, implementation changes, numerical
checks, and execution against reference cases.

The deliverable is a versioned implementation or patch with configurations,
adapter code, regression tests, assumptions, and measured results. It must be
usable outside the website. A generated artifact is not automatically suitable
for an onboard target; integration and target measurements remain separate work.

### Investigate a navigation failure

Inputs include a failing recording, the exact estimator version and configuration,
sensor metadata, and any reference measurements. Meridian first determines
whether the failure can be reproduced with the available inputs. It then tests
competing explanations such as time misalignment, frame/calibration errors,
initialization, unsuitable noise assumptions, or an implementation regression.

The deliverable links each observation and hypothesis to data and experiments,
records alternatives, and provides a tested candidate change when supported.
When several explanations remain indistinguishable, it identifies the missing
measurement or experiment instead of inventing a cause.

### Evaluate a change across a campaign

Inputs include baseline and candidate estimators, a collection of recordings or
simulations, reference quality, acceptance criteria, and compute limits.
Meridian executes paired runs, searches allowed settings, tests declared fault
conditions, and evaluates the selected candidate on data excluded from tuning.

The deliverable contains improvements, regressions, condition-specific tradeoffs,
failed or incomplete runs, and a reproducible decision record. A lower aggregate
score alone does not establish that a candidate meets the requirements.

## Proposed capabilities

| Capability | Engineering purpose | Expected output |
| --- | --- | --- |
| Dataset and sensor workspace | Preserve original data and inspect units, frames, sensor instances, time domains, gaps, and calibration provenance. | Versioned dataset, explicit transformations, eligibility findings. |
| Calibration and timing tools | Estimate supported calibration/time parameters and assess whether the acquisition constrains them. | Calibration artifact, assumptions, uncertainty or conditioning information, independent evaluation. |
| Visual-inertial development | Inspect and modify image processing, feature tracking, initialization, inertial propagation, and visual-inertial fusion. | Executable VIO changes with traceable visual and estimator diagnostics. |
| GNSS-independent localization | Evaluate tracking loss, drift, recovery, and supported map or additional-sensor constraints. | Condition-specific localization results and explicit availability limits. |
| Estimator development | Configure a reference estimator or modify an external implementation and its sensor models. | Source/configuration diff, build artifact, numerical checks. |
| Reproducible execution | Run the actual selected implementation against the same declared input semantics. | Input, code, configuration, environment, and output identities. |
| Campaigns and optimization | Compare candidates across recordings and conditions, with bounded searches and declared tradeoffs. | Candidate settings, comparison results, independent evaluation. |
| Failure investigation | Connect symptoms to competing explanations and discriminating tests. | Evidence-backed investigation and unresolved questions. |
| Engineering delivery | Export work into the team's codebase, automation, and target integration process. | Code/configuration, executable reproduction recipe, report, regression assets. |
| Team workspace | Review results, share precise inspection states, and preserve decisions in private projects. | Versioned reviews and traceable experiment history. |

Import formats should follow actual integrations, starting with a camera/IMU
recording supported by a selected VIO reference. Existing DataFlash support is
a useful separate asset; PX4 ULog and robotics recording formats such as
MCAP/ROS bags are candidates, not a promise to decode every recording. Developing
the visual pipeline requires images and their acquisition metadata, not just
an exported pose trajectory. Required input availability must be checked before
claiming that an estimator can be reproduced.

External engineers must be able to evaluate their own code. The execution
contract should cover input events and timing, initialization, state and
covariance conventions, diagnostic availability, configuration, and outputs.
Missing internal diagnostics should remain explicitly unavailable. An adapter
must not silently change an estimator's scheduling or measurement semantics.

## Numerical direction, VIO, and the EKF

The initial professional reference workflow should use an existing VIO
implementation with a prepared camera/IMU dataset. Its revision, configuration,
calibration, timing conventions, and reproducible outputs must be pinned.
This provides an engineering task before developing every numerical component
inside Meridian. Increment 1.1 selected and qualified OpenVINS on TUM-VI room1;
its [integration guide](vio-reference.md) records the pinned implementation,
compatibility checks, notices and limitations.

Meridian's own numerical development should include 3D inertial propagation and
EKF support for orientation, velocity, position, and biases, with observation
models appropriate to the selected use case. IMU-only propagation is not a
complete position solution. Visual-inertial filtering may require additional
state structure and consistency treatment; a generic inertial EKF is not a VIO
implementation by itself. State conventions, frames, gravity assumptions,
discretization, initialization, timestamps, sensor delays, and measurement models
must be specified before implementation and tested independently.

VIO development covers the actual visual and inertial chain: camera models and
calibration, image handling, feature tracking, initialization, propagation,
fusion, and inspectable intermediate outputs. It must support changing the
implementation and comparing the result, rather than only replaying a finished
trajectory. Monocular/stereo support and the first filtering or optimization
formulation will be selected when the corresponding part is designed.

External estimators should coexist with Meridian's components. Filtering and
optimization-based VIO must not be forced into a single EKF state/API design.
Published systems such as [OpenVINS](https://docs.openvins.com/) and
[VINS-Fusion](https://github.com/HKUST-Aerial-Robotics/VINS-Fusion) illustrate
these different approaches. OpenVINS supplies the first reference integration;
VINS-Fusion remains an example, not an adopted dependency.

Longer-term localization work includes loss/recovery of visual tracking,
relocalization, map constraints, and additional measurements where they address
a defined requirement. Barometric altitude, range, optical flow, lidar, or other
sources are possible extensions, not an obligation to implement every modality.
GNSS absence, degradation, and return should be explicit experiment conditions
when GNSS is supported. Observability and drift limits remain dependent on the
available measurements, motion, environment, and reference information.

The existing roll/bias implementation remains useful as a small numerical
regression case and Python/C++ comparison. Its gravity-dominated single-axis
assumptions do not extend to general 3D navigation. Existing reports retain their
original scope and evidence; this roadmap does not upgrade their validation.

## Automation and AI

Automation should operate on executable experiments and explicit artifacts.
Numerical tools calculate metrics; an agent may plan and coordinate their use.
Every recommendation must expose what was executed and what evidence supports it.

Proposed progression:

1. **Deterministic automation:** imports, data checks, repeatable runs, parameter
   searches, comparison reports, and regression campaigns.
2. **Assisted investigations:** an agent formulates hypotheses, chooses supported
   analyses, executes them within a budget, and records supporting and conflicting
   evidence. Its ability to identify faults is evaluated on known cases.
3. **Bounded engineering tasks:** an agent modifies an allowed model, configuration,
   or implementation, runs a campaign, and delivers a candidate change that an
   independent evaluator accepts, rejects, or marks inconclusive.
4. **Research extensions:** experiment selection, automated counterexample search,
   model-structure exploration, and learned estimation components.

The last group contains substantial research uncertainty. Examples worth
investigating are:

- Select the next simulation or permitted bench acquisition that best separates
  competing failure explanations.
- Search for realistic sensor faults or timing conditions that break a candidate
  while preserving the stated experiment constraints.
- Propose a new observation/state model, generate its implementation, and compare
  it against established baselines on untouched evaluation data.
- Learn a bounded correction or uncertainty model and assess whether it improves
  performance across independent sessions, vehicles, and environments.

An optimizing agent must not alter its acceptance criteria or independent
evaluation harness. Evaluation access must be tracked: once results guide a new
candidate, they are no longer untouched final evidence. Failed searches, rejected
changes, and compute costs belong in the record.

These are candidate differentiators, not claims of novelty or demonstrated
Meridian capabilities. Autonomous execution of a scoped investigation is a
different claim from reliable autonomous engineering across arbitrary vehicles.

## Evidence and acceptance

Each campaign must declare its operating domain, requirements, available
references, exclusions, and decision criteria before candidate selection.

- Quantify accuracy only against a suitable reference and preserve its timing,
  frame alignment, uncertainty, and coverage. Another fused estimate is a
  comparator unless independently justified as a reference.
- Separate estimator inputs from evaluation references. A GNSS-free VIO run can
  be evaluated against an independent motion-capture or suitably characterized
  GNSS reference where available, without supplying that reference to the VIO.
  Declare trajectory alignment and the reported local/global quantities; do not
  hide drift or claim absolute accuracy through an inappropriate alignment.
- Examine consistency, innovations, covariance behavior, availability, recovery,
  and numerical failures as distinct results. Consistency scores without truth
  do not establish physical accuracy or identify a unique cause.
- Separate calibration/training/tuning data from evaluation by appropriate units
  such as recording, session, vehicle, or environment; avoid overlapping-window
  leakage. Define any adaptation allowed during evaluation.
- Compare candidates on identical inputs, retain unfavorable runs, and report
  condition-specific outcomes alongside aggregates.
- Record code/configuration/data identities, dependency versions, seeds, hardware,
  and numerical tolerances. Explicitly state nondeterministic behavior.
- Measure runtime and memory on the named target. Hosted-worker performance does
  not establish embedded timing or resource use.
- Distinguish recorded-input replay from closed-loop simulation. A changed
  estimator can change controller outputs and the vehicle trajectory; replay
  alone cannot establish the resulting flight behavior.

Actual bench acquisition and replay remain part of Meridian's scope. Initial
bench work uses a disarmed drone with propellers removed and keeps the estimator
outside the control loop. Any later hardware integration or flight evaluation
requires its own protocol and evidence; none is claimed by this proposal.

## Hosted product and expert interface

The intended default workflow runs from a hosted browser workspace, including
compute jobs. Users should not need a local Meridian installation for supported
work. Generated implementations must also be usable independently of that site.

The proposed system separates the browser, experiment metadata, original data
and derived artifacts, job scheduling, and isolated execution workers. Executing
external code requires per-project isolation, bounded resources, cancellation,
and explicit access to datasets and credentials. AI tools use the same controlled
execution boundary and cannot broaden a job's permissions.

A publicly accessible service must support private datasets and source code,
explicit sharing, deletion/retention choices, and clear AI data handling. Private
compute or self-hosted deployment may become necessary for some organizations;
these are later deployment options, not a prerequisite for the hosted path.

The main workspace should organize projects, datasets, estimator versions,
experiments, and comparisons. An investigation view should link a selected
interval across images, visual tracks, trajectories, sensor signals, innovations,
state/covariance, events, configuration changes, and code differences where available. Campaign
views should expose failures, budgets, progress, and reproducible run manifests.
Automation should be reviewable through the resulting artifacts and executions.

The initial metadata workspace now uses Vite, Django and PostgreSQL on an isolated
shared-VPS deployment. Durable PostgreSQL jobs coordinate a separate compute
worker and isolated native/evaluation containers. The original
static frontend supplies reusable plotting and interaction components; its fixed
simulation data contract is unsuitable as the general execution API.

## Major development parts

The sequence below defines outcomes and boundaries, not detailed implementation
tasks or delivery dates. Each part's detailed design will use the results of the
preceding parts. VIO is the reference use case from the first part, rather than
a feature deferred until the platform is otherwise complete.

| Part | Scope | Completion outcome |
| --- | --- | --- |
| 1. Hosted workspace and reference workflow | A scoped professional task, private project, minimal job execution, prepared calibrated camera/IMU dataset, and an existing VIO implementation. | Run a reference from the browser, inspect its outputs, and export a reproducible result without installing Meridian locally. |
| 2. Sensor data, calibration, and timing | Extend the initial supported data path to user recordings, image/IMU synchronization, units/frames, sensor metadata, and supported calibration workflows. | Produce a versioned acquisition and calibration usable by the estimator, with explicit data-quality and calibration limits. |
| 3. 3D estimation and estimator integration | Inertial models, states and uncertainty, EKF components, model/code modification, numerical checks, and a contract for external implementations. | Develop and compare a scoped estimation change, with tested conventions and code/configuration reusable outside the site. |
| 4. Visual-inertial development | Visual tracking, initialization, camera/IMU fusion, intermediate diagnostics, and changes to the VIO implementation. | An engineer can modify a VIO component and evaluate the full resulting pipeline on recordings independent of its tuning data. |
| 5. Robust localization without reliable GNSS | Tracking degradation/loss, drift, recovery, and selected relocalization, map, or additional-sensor methods; GNSS transitions when relevant. | Characterize and improve localization availability under declared failure conditions, without concealing unresolved ambiguity or drift. |
| 6. Advanced campaigns and system integration | Larger test campaigns, constrained tuning, fault injection, regression automation, target resource measurements, and scoped closed-loop integration. | Compare robustness and performance across conditions and produce a documented integration candidate with target-specific evidence. |
| 7. AI engineering agents | Hypothesis-driven investigations and bounded model/code/configuration changes using the established execution and independent evaluation tools. | Complete a defined engineering task with reviewable artifacts; measure verified improvements, false diagnoses, regressions, human effort, and cost. |
| 8. Advanced research | Experiment selection, counterexample search, model-structure exploration, and learned estimation components. | Demonstrate additional value on separate evaluation conditions before promoting an experimental capability into routine engineering use. |

Usability, numerical checks, comparisons, reproducibility, basic runtime
measurements, exports, and private-data handling accompany the first applicable
increments. Part 6 expands their depth and scale; it does not introduce testing
or usable deliverables for the first time. Target and flight claims still require
their own evidence. Real bench acquisition remains part of the development and
evaluation work when the required hardware is available.

The minimal execution/data contract in part 1 supports its chosen reference;
parts 2 and 3 extend it from observed requirements. This avoids a universal
plugin framework before multiple concrete integrations exist. The roll study
can support infrastructure checks but does not constitute the VIO milestone.

The [part 1 implementation plan](part-1-hosted-vio-workspace.md) divides the first
hosted workflow into eight sequential increments. Increment 1.1 has
[qualified the local OpenVINS/TUM-VI reference](../results/vio-reference/README.md).
Increment 1.2 adds a [validated hosted private workspace](../results/workspace/README.md).
Increment 1.3 adds [versioned VIO experiments](versioned-experiments.md), with
immutable specifications consumed unchanged by the local runner. Increment 1.4
adds durable isolated VIO jobs using those same specifications. Increment 1.5
adds synchronized investigation, and increment 1.6 adds saved comparisons with
explicit common support. Increment 1.7 adds
[independent exports and verified reproduction](../results/exports/README.md).
Increment 1.8 completes [hosted acceptance](../results/hosted-acceptance/README.md),
including operational faults, quotas, off-server backup restoration and replay
after deleting the source job. The separately evaluated room2 recording fails
the unchanged coverage gates. The
[part 2 plan](part-2-sensor-data-calibration-timing.md) retains that result
and details acquisition, calibration, timing and input/reference validation.
Increment 2.1 implements local acquisition/calibration/clock contracts and
synthetic convention checks; import, hosted uploads and calibration fitting
remain planned. Parts 3–8 are not detailed here.

## Existing tools and research

The following primary sources were inspected on 2026-10-17. They establish a
capability baseline, not comparative performance or a verified market gap.

| Source | Documented capability | Consequence for this proposal |
| --- | --- | --- |
| [MathWorks inertial fusion](https://www.mathworks.com/help/fusion/inertial-sensor-fusion.html) and [automatic tuning](https://www.mathworks.com/help/fusion/ref/insfilterasync.tune.html) | Navigation/orientation filters and optimization of supported filter properties against reference states. | Multiple filters and automatic noise tuning alone are not differentiation. |
| [PX4 replay](https://docs.px4.io/main/en/debug/system_wide_replay) and [navigation-filter analysis](https://docs.px4.io/main/en/advanced_config/tuning_the_ecl_ekf) | Logged-input replay, parameter comparisons, estimator diagnostics, and analysis tools, with replay prerequisites and restrictions. | Use exact implementation/revision semantics; build value beyond plotting and rerunning logs. |
| [Foxglove agents](https://docs.foxglove.dev/docs/agents) | Recording search, data inspection, layouts, transformation scripts, and assisted debugging. | Natural-language interaction and robotics plots already exist together. |
| [Kalibr](https://github.com/ethz-asl/kalibr/wiki/camera-imu-calibration) | Camera/IMU calibration with required models, motion excitation, and timestamp assumptions. | Assess or integrate established methods before creating another calibrator. |
| [OpenVINS](https://docs.openvins.com/) and [VINS-Fusion](https://github.com/HKUST-Aerial-Robotics/VINS-Fusion) | Visual-inertial estimation using filtering and optimization respectively, with calibration and evaluation capabilities. | VIO is a concrete professional reference task; support different formulations and evaluate existing implementations before selecting an integration. |
| [SymForce](https://symforce.org/) | Symbolic robotics models, Jacobians, code generation, and optimization. | Model-to-code automation has concrete foundations; unrestricted generated models still require independent checks. |
| [Chen et al., Kalman Filter Auto-Tuning](https://discovery.ucl.ac.uk/id/eprint/10185809/) | Bayesian tuning with analysis of ambiguity in consistency-based objectives. | A single favorable consistency score is not a sufficient optimization or acceptance target. |
| [AirIMU](https://arxiv.org/abs/2310.04874) | Learned inertial corrections and uncertainty combined with model-based estimation in reported experiments. | Hybrid approaches are a research option; transfer to a new vehicle/domain must be evaluated. |

The proposed advantage is a connected engineering process that produces reusable
changes and independently evaluated evidence. Its usefulness, cost, integration
effort, and advantage over existing tools must be demonstrated with external
engineers. No source review establishes that any feature is unique.

## Decisions still required

Part 1 is complete. Part 2 must qualify its supported transfer formats,
calibration models/tools, clock corrections, private-data budgets and independent
calibration/evaluation recordings. Its proposed increments resolve these choices
from measured evidence. Room2 is already evaluated and cannot be reused as an
untouched reserved sequence. Preserve its recorded negative result.
Numerical formulations, broader formats/sensors and automation tasks remain
decisions for their corresponding parts.

Broader estimator development and hardware integration remain
future work. The existing technical reports remain evidence for their stated scope.
