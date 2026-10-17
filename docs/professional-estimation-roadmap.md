# Professional state-estimation workspace

**Status: product and engineering proposal, 2026-10-17.** This document describes
a proposed redesign. The [README](../README.md) and [design notes](design.md)
describe the implementation available today. No hosted computation service,
3D navigation estimator, external-estimator execution, or AI engineering agent
has been implemented as part of this proposal.

## Product objective

Meridian should help estimation engineers build, investigate, improve, and
evaluate the state-estimation systems used in autonomous drones. The intended
product is a hosted engineering workspace, optimized for professional work.
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

An initial user should be an engineer responsible for an inertial navigation
pipeline, working with IMU and GNSS measurements and an existing codebase.
Supporting broader teams requires interoperability across sensor setups and
estimator implementations. Universal compatibility is an objective to work
toward, not a release claim.

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
| Estimator development | Configure a reference estimator or modify an external implementation and its sensor models. | Source/configuration diff, build artifact, numerical checks. |
| Reproducible execution | Run the actual selected implementation against the same declared input semantics. | Input, code, configuration, environment, and output identities. |
| Campaigns and optimization | Compare candidates across recordings and conditions, with bounded searches and declared tradeoffs. | Candidate settings, comparison results, independent evaluation. |
| Failure investigation | Connect symptoms to competing explanations and discriminating tests. | Evidence-backed investigation and unresolved questions. |
| Engineering delivery | Export work into the team's codebase, automation, and target integration process. | Code/configuration, executable reproduction recipe, report, regression assets. |
| Team workspace | Review results, share precise inspection states, and preserve decisions in private projects. | Versioned reviews and traceable experiment history. |

Import formats should follow actual integrations. Existing DataFlash support is
a starting asset; PX4 ULog and robotics recording formats such as MCAP/ROS bags
are candidates, not an initial promise to decode every recording. A log that
contains published attitude alone may be insufficient to rerun its estimator.

External engineers must be able to evaluate their own code. The execution
contract should cover input events and timing, initialization, state and
covariance conventions, diagnostic availability, configuration, and outputs.
Missing internal diagnostics should remain explicitly unavailable. An adapter
must not silently change an estimator's scheduling or measurement semantics.

## Numerical direction and the EKF

The proposed reference direction is a 3D error-state EKF for inertial navigation,
initially combining IMU and GNSS. Its state would include orientation, velocity,
position, and inertial biases. State conventions, frames, gravity assumptions,
discretization, initialization, timestamps, sensor delays, and measurement models
must be specified before implementation and tested independently.

The reference provides a working estimation engine and a comparable baseline.
It should coexist with external estimators. Further sensor integrations could
include barometric altitude, magnetometers, range, and visual odometry. Direct
camera processing and visual-inertial estimation introduce additional calibration,
timing, compute, and validation requirements and should form distinct increments.

Alternative formulations, such as smoothing or factor-graph methods, should be
added when a concrete engineering comparison requires them. EKF support does not
require forcing every future estimation problem into the same model.

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
interval across trajectories, sensor signals, innovations, state/covariance,
events, configuration changes, and code differences where available. Campaign
views should expose failures, budgets, progress, and reproducible run manifests.
Automation should be reviewable through the resulting artifacts and executions.

Provider, frontend framework, database, and queue choices remain open. The
current static frontend can supply reusable plotting and interaction components;
its fixed simulation data contract is unsuitable as the general execution API.

## Delivery sequence and gates

These are proposed capability gates, not delivery dates or a commitment to
implement every item at once.

| Stage | Deliverable | Gate before expanding |
| --- | --- | --- |
| 1. Define an expert task | A real external estimator/data pair, baseline investigation, reference quality, and agreed evaluation protocol. | Another engineer can describe the decision to make and reproduce the starting result; measure the existing workflow's effort. |
| 2. Deliver the first hosted engineering workflow | Run and compare a baseline and candidate on a scoped 3D IMU/GNSS task, with at least one external implementation, private data, and exportable results. | Reproduce an independently checked result; detect known defects and preserve inconclusive cases; demonstrate useful work without a local Meridian installation. |
| 3. Develop and evaluate changes | Reference EKF, model/implementation editing, numerical checks, supported calibration/tuning, campaign execution, and code/configuration export. | An external engineer completes a real modification, evaluates it on separate data, and reproduces the exported result outside Meridian. |
| 4. Automate bounded engineering tasks | Investigation and change-generation agents, independent evaluation, budgets, and complete experiment records. | Compare with manual work and deterministic automation on a fixed benchmark; measure verified fixes, false diagnoses, accepted regressions, review effort, time, and cost. |
| 5. Expand the research and integration scope | More sensors/estimators, active experiment selection, learned components, and target/closed-loop integrations. | Each extension passes domain-specific evaluation and demonstrates additional engineering value. |

The small roll study can support early infrastructure checks but does not
constitute the professional product milestone. Detailed stage specifications
should follow a selected estimator and dataset rather than a universal plugin
framework built in anticipation of every possible integration.

## Existing tools and research

The following primary sources were inspected on 2026-10-17. They establish a
capability baseline, not comparative performance or a verified market gap.

| Source | Documented capability | Consequence for this proposal |
| --- | --- | --- |
| [MathWorks inertial fusion](https://www.mathworks.com/help/fusion/inertial-sensor-fusion.html) and [automatic tuning](https://www.mathworks.com/help/fusion/ref/insfilterasync.tune.html) | Navigation/orientation filters and optimization of supported filter properties against reference states. | Multiple filters and automatic noise tuning alone are not differentiation. |
| [PX4 replay](https://docs.px4.io/main/en/debug/system_wide_replay) and [navigation-filter analysis](https://docs.px4.io/main/en/advanced_config/tuning_the_ecl_ekf) | Logged-input replay, parameter comparisons, estimator diagnostics, and analysis tools, with replay prerequisites and restrictions. | Use exact implementation/revision semantics; build value beyond plotting and rerunning logs. |
| [Foxglove agents](https://docs.foxglove.dev/docs/agents) | Recording search, data inspection, layouts, transformation scripts, and assisted debugging. | Natural-language interaction and robotics plots already exist together. |
| [Kalibr](https://github.com/ethz-asl/kalibr/wiki/camera-imu-calibration) | Camera/IMU calibration with required models, motion excitation, and timestamp assumptions. | Assess or integrate established methods before creating another calibrator. |
| [SymForce](https://symforce.org/) | Symbolic robotics models, Jacobians, code generation, and optimization. | Model-to-code automation has concrete foundations; unrestricted generated models still require independent checks. |
| [Chen et al., Kalman Filter Auto-Tuning](https://discovery.ucl.ac.uk/id/eprint/10185809/) | Bayesian tuning with analysis of ambiguity in consistency-based objectives. | A single favorable consistency score is not a sufficient optimization or acceptance target. |
| [AirIMU](https://arxiv.org/abs/2310.04874) | Learned inertial corrections and uncertainty combined with model-based estimation in reported experiments. | Hybrid approaches are a research option; transfer to a new vehicle/domain must be evaluated. |

The proposed advantage is a connected engineering process that produces reusable
changes and independently evaluated evidence. Its usefulness, cost, integration
effort, and advantage over existing tools must be demonstrated with external
engineers. No source review establishes that any feature is unique.

## Decisions still required

Select the first external estimator, dataset and reference, task owner, and
acceptance protocol. Specify the first 3D model and adapter boundaries before
implementation. Evaluate dependency licenses and integration costs before
adoption. Establish hosted data handling and realistic compute budgets. Choose
the first bounded automation task after deterministic execution is reliable.

Implementation changes, dependency selection, hosted deployment, and hardware
integration remain future work. The existing technical reports remain evidence
for their stated scope.
