#include "meridian/angle_bias_ekf.hpp"

#include <array>
#include <cmath>
#include <iostream>
#include <limits>
#include <stdexcept>
#include <string>

namespace {

using meridian::AngleBiasEKF;
using meridian::EkfConfig;
using meridian::Matrix2;
using meridian::Vector2;

constexpr double pi = 3.141592653589793238462643383279502884;
constexpr double gravity = 9.80665;
int checks = 0;

void check(bool condition, const char* expression, int line) {
    ++checks;
    if (!condition) {
        throw std::runtime_error("line " + std::to_string(line) + ": " + expression);
    }
}

// These checks remain active in Release builds, unlike the assert macro.
#define CHECK(expression) check(static_cast<bool>(expression), #expression, __LINE__)

void check_near(double actual, double expected, double tolerance = 1e-12) {
    CHECK(std::isfinite(actual));
    CHECK(std::abs(actual - expected) <= tolerance);
}

template <typename Actual, typename Expected>
void check_matrix(const Eigen::MatrixBase<Actual>& actual,
                  const Eigen::MatrixBase<Expected>& expected,
                  double tolerance = 1e-12) {
    CHECK(actual.rows() == expected.rows());
    CHECK(actual.cols() == expected.cols());
    CHECK(actual.allFinite());
    CHECK((actual - expected).cwiseAbs().maxCoeff() <= tolerance);
}

template <typename Exception, typename Function>
void expect_exception(Function function) {
    bool caught = false;
    try {
        function();
    } catch (const Exception&) {
        caught = true;
    }
    CHECK(caught);
}

EkfConfig configuration() {
    return EkfConfig{0.3, 0.02, 0.4, 0.2, 0.05, 0.2, gravity};
}

void check_covariance(const Matrix2& covariance) {
    CHECK(covariance.allFinite());
    check_matrix(covariance, covariance.transpose(), 1e-15);
    const double half_trace = 0.5 * covariance(0, 0) + 0.5 * covariance(1, 1);
    const double half_difference = 0.5 * covariance(0, 0) - 0.5 * covariance(1, 1);
    const double minimum_eigenvalue = half_trace - std::hypot(half_difference, covariance(0, 1));
    CHECK(minimum_eigenvalue >= -1e-14);
}

void test_gravity_geometry_and_jacobian() {
    check_matrix(meridian::gravity_observation(0.0), Vector2(0.0, -gravity));
    check_matrix(meridian::gravity_observation(pi / 2.0), Vector2(-gravity, 0.0));
    check_matrix(meridian::gravity_observation(-pi / 2.0), Vector2(gravity, 0.0));
    check_matrix(meridian::gravity_observation(pi), Vector2(0.0, gravity));
    check_matrix(meridian::gravity_observation(0.0, 3.0), Vector2(0.0, -3.0));

    const std::array<double, 7> angles{{-4.7, -pi, -0.8, 0.0, 0.9, pi / 2.0, 7.1}};
    for (double angle : angles) {
        const Matrix2 jacobian = meridian::gravity_jacobian(angle);
        Matrix2 finite_difference;
        const Vector2 state(angle, 0.27);
        for (int column = 0; column < 2; ++column) {
            Vector2 lower = state;
            Vector2 upper = state;
            constexpr double step = 1e-6;
            lower(column) -= step;
            upper(column) += step;
            finite_difference.col(column) = (
                meridian::gravity_observation(upper(0))
                - meridian::gravity_observation(lower(0))) / (2.0 * step);
        }
        check_matrix(jacobian, finite_difference, 3e-9);
        check_matrix(jacobian.col(1), Vector2::Zero(), 0.0);
        check_near(jacobian.col(0).norm(), gravity);
        check_near(jacobian.col(0).dot(meridian::gravity_observation(angle)), 0.0, 2e-14);
    }
}

void test_analytical_prediction_and_bias_correction() {
    const EkfConfig config = configuration();
    AngleBiasEKF estimator(config);
    constexpr double dt = 0.15;
    estimator.predict(0.4, dt);
    check_matrix(estimator.state(), Vector2(0.3 + dt * (0.4 - 0.02), 0.02));
    Matrix2 expected;
    expected << 0.16 + dt * dt * 0.04 + std::pow(0.05 * dt, 2), -dt * 0.04,
                -dt * 0.04, 0.04;
    check_matrix(estimator.covariance(), expected);

    const Vector2 prior = estimator.state();
    estimator.update(meridian::gravity_observation(prior(0) + 0.1));
    CHECK(estimator.state()(0) > prior(0));
    CHECK(estimator.state()(1) < prior(1));
    check_covariance(estimator.covariance());

    EkfConfig no_bias = config;
    no_bias.initial_bias_std_rad_s = 0.0;
    AngleBiasEKF fixed_bias(no_bias);
    fixed_bias.predict(0.4, dt);
    fixed_bias.update(meridian::gravity_observation(1.0));
    check_near(fixed_bias.state()(1), config.initial_bias_rad_s, 0.0);
    check_near(fixed_bias.covariance()(1, 1), 0.0, 0.0);
}

void test_bias_random_walk_analytical_covariance() {
    EkfConfig config = configuration();
    config.bias_random_walk_std_rad_s_per_sqrt_s = 0.2;
    AngleBiasEKF estimator(config);
    estimator.predict(0.4, 1.5);

    // Integrating q_b=0.04 over 1.5 s contributes 0.045 rad^2 to angle,
    // -0.045 rad^2/s cross covariance and 0.06 (rad/s)^2 to bias.
    // Add these to the initial covariance propagation and sample gyro noise.
    Matrix2 expected;
    expected << 0.16 + 2.25 * 0.04 + 0.075 * 0.075 + 0.045, -1.5 * 0.04 - 0.045,
                -1.5 * 0.04 - 0.045, 0.04 + 0.06;
    check_matrix(estimator.state(), Vector2(0.87, 0.02));
    check_matrix(estimator.covariance(), expected);
    check_covariance(estimator.covariance());

    // With no initial uncertainty or gyro noise, isolate the continuous Q.
    config.initial_angle_std_rad = 0.0;
    config.initial_bias_std_rad_s = 0.0;
    config.gyro_noise_std_rad_s = 0.0;
    AngleBiasEKF isolated(config);
    isolated.predict(0.4, 1.5);
    expected << 0.045, -0.045, -0.045, 0.06;
    check_matrix(isolated.covariance(), expected);
    check_covariance(isolated.covariance());
}

void test_bias_random_walk_interval_composition() {
    EkfConfig config = configuration();
    config.gyro_noise_std_rad_s = 0.0;
    config.bias_random_walk_std_rad_s_per_sqrt_s = 0.13;
    AngleBiasEKF whole(config);
    AngleBiasEKF partitioned(config);
    whole.predict(0.4, 1.25);
    for (double dt : {0.125, 0.375, 0.5, 0.25}) {
        partitioned.predict(0.4, dt);
    }
    // Continuous process noise composes across intervals via F Q F^T.
    // Fixed per-sample gyro variance deliberately contributes no noise here.
    check_matrix(partitioned.state(), whole.state());
    check_matrix(partitioned.covariance(), whole.covariance());
}

void test_bias_random_walk_reopens_bias_uncertainty() {
    EkfConfig config{0.0, 0.0, 0.0, 0.0, 0.0, 0.2, gravity};
    config.bias_random_walk_std_rad_s_per_sqrt_s = 0.2;
    AngleBiasEKF estimator(config);
    estimator.predict(0.0, 1.5);
    check_matrix(estimator.state(), Vector2::Zero(), 0.0);
    const Matrix2 prior = estimator.covariance();
    CHECK(prior(1, 1) > 0.0);
    CHECK(prior(0, 1) < 0.0);

    // The nonzero cross covariance permits a bias correction even though
    // P0=0. This scalar tangent oracle is independent of the 2 x 2 solve.
    const Vector2 expected_state = prior.col(0)
        * (std::sin(0.1) / (prior(0, 0) + std::pow(0.2 / gravity, 2)));
    estimator.update(meridian::gravity_observation(0.1));
    check_matrix(estimator.state(), expected_state);
    CHECK(estimator.state()(1) < 0.0);
    CHECK(estimator.covariance()(1, 1) < prior(1, 1));
    check_covariance(estimator.covariance());
}

void test_zero_bias_random_walk_preserves_reference() {
    // The original seven-field aggregate still defaults to the old model.
    const EkfConfig original = configuration();
    check_near(original.bias_random_walk_std_rad_s_per_sqrt_s, 0.0, 0.0);
    EkfConfig explicit_zero = original;
    explicit_zero.bias_random_walk_std_rad_s_per_sqrt_s = 0.0;
    AngleBiasEKF default_estimator(original);
    AngleBiasEKF zero_estimator(explicit_zero);
    for (int index = 1; index <= 25; ++index) {
        const double dt = 0.01 * (index % 3 + 1);
        const double rate = 0.2 * std::cos(0.1 * index);
        default_estimator.predict(rate, dt);
        zero_estimator.predict(rate, dt);
        check_matrix(zero_estimator.state(), default_estimator.state(), 0.0);
        check_matrix(zero_estimator.covariance(), default_estimator.covariance(), 0.0);
        const Vector2 observation = meridian::gravity_observation(0.1 * std::sin(index));
        const auto default_innovation = default_estimator.update(observation);
        const auto zero_innovation = zero_estimator.update(observation);
        check_matrix(zero_estimator.state(), default_estimator.state(), 0.0);
        check_matrix(zero_estimator.covariance(), default_estimator.covariance(), 0.0);
        check_matrix(zero_innovation.residual, default_innovation.residual, 0.0);
        check_matrix(zero_innovation.covariance, default_innovation.covariance, 0.0);
    }

    // Zero density must skip the unused dt^3 calculation: this prediction has
    // exactly zero uncertainty, even at an interval whose cube would overflow.
    AngleBiasEKF certain(EkfConfig{0.1, 0.02, 0.0, 0.0, 0.0, 0.2, gravity});
    certain.predict(0.02, 1e200);
    check_matrix(certain.state(), Vector2(0.1, 0.02), 0.0);
    check_matrix(certain.covariance(), Matrix2::Zero(), 0.0);
}

void test_bias_random_walk_overflow_is_atomic() {
    for (const auto& density_interval : std::array<std::array<double, 2>, 2>{{
             {{1.0, 1e110}}, {{1e150, 1e10}}}}) {
        EkfConfig config{0.1, 0.02, 0.0, 0.0, 0.0, 0.2, gravity};
        config.bias_random_walk_std_rad_s_per_sqrt_s = density_interval[0];
        AngleBiasEKF estimator(config);
        const Vector2 initial_state = estimator.state();
        const Matrix2 initial_covariance = estimator.covariance();
        // Respectively overflow the integrated angle variance and q_b * dt.
        // The deterministic state and all other covariance terms are finite.
        expect_exception<std::runtime_error>([&] {
            estimator.predict(0.02, density_interval[1]);
        });
        check_matrix(estimator.state(), initial_state, 0.0);
        check_matrix(estimator.covariance(), initial_covariance, 0.0);
        estimator.predict(0.02, 0.01);
        CHECK(estimator.covariance().allFinite());
        CHECK(estimator.covariance()(1, 1) > 0.0);
    }
}

void test_independent_scalar_correction_oracle() {
    // Isotropic component noise reduces to a scalar angular correction with
    // innovation (measured magnitude / g) * sin(measured angle - prior angle).
    // This oracle never solves the 2 x 2 measurement system used by the EKF.
    for (double angle : {-2.8, -0.7, 0.0, 0.8, 6.2}) {
        for (double magnitude_ratio : {0.4, 1.0, 1.7}) {
            EkfConfig config = configuration();
            config.initial_angle_rad = angle;
            AngleBiasEKF estimator(config);
            estimator.predict(0.12, 0.19);
            const Vector2 prior_state = estimator.state();
            const Matrix2 prior_covariance = estimator.covariance();
            const double measured_angle = prior_state(0) + 0.37;
            const Vector2 observation = magnitude_ratio * meridian::gravity_observation(measured_angle);

            const double scalar_variance = std::pow(config.accel_noise_std_m_s2 / gravity, 2);
            const Vector2 scalar_gain = prior_covariance.col(0)
                / (prior_covariance(0, 0) + scalar_variance);
            const double effective_innovation = magnitude_ratio * std::sin(0.37);
            const Vector2 expected_state = prior_state + scalar_gain * effective_innovation;
            Matrix2 scalar_correction = Matrix2::Identity();
            scalar_correction.col(0) -= scalar_gain;
            const Matrix2 expected_covariance = scalar_correction * prior_covariance
                * scalar_correction.transpose()
                + scalar_variance * scalar_gain * scalar_gain.transpose();

            const meridian::Innovation innovation = estimator.update(observation);
            check_matrix(estimator.state(), expected_state, 2e-13);
            check_matrix(estimator.covariance(), expected_covariance, 2e-14);
            check_matrix(innovation.residual,
                         observation - meridian::gravity_observation(prior_state(0)));
            const Vector2 tangent(-gravity * std::cos(prior_state(0)),
                                   gravity * std::sin(prior_state(0)));
            const Matrix2 expected_innovation_covariance = prior_covariance(0, 0)
                * tangent * tangent.transpose()
                + std::pow(config.accel_noise_std_m_s2, 2) * Matrix2::Identity();
            check_matrix(innovation.covariance, expected_innovation_covariance, 5e-14);
        }
    }
}

void test_radial_zero_and_opposite_observations() {
    for (double magnitude_ratio : {0.0, -1.0, 0.4, 2.0}) {
        EkfConfig config = configuration();
        config.initial_angle_rad = 0.0;
        config.initial_bias_rad_s = 0.0;
        AngleBiasEKF estimator(config);
        estimator.predict(0.0, 0.1);
        const Vector2 prior_state = estimator.state();
        const Matrix2 prior_covariance = estimator.covariance();
        const auto innovation = estimator.update(Vector2(0.0, -magnitude_ratio * gravity));
        check_matrix(estimator.state(), prior_state, 0.0);
        CHECK(estimator.covariance()(0, 0) < prior_covariance(0, 0));
        CHECK(innovation.residual.norm() > 0.0);
        check_covariance(estimator.covariance());
    }

    // A complete turn remains in the state; the force observation is periodic.
    EkfConfig config = configuration();
    config.initial_angle_rad = 2.0 * pi + 0.2;
    AngleBiasEKF estimator(config);
    estimator.update(meridian::gravity_observation(0.2));
    check_near(estimator.state()(0), 2.0 * pi + 0.2);
    estimator.predict(1.0, 0.1);
    CHECK(estimator.state()(0) > 2.0 * pi);
}

void test_value_ownership_and_zero_uncertainty() {
    AngleBiasEKF estimator(configuration());
    const Vector2 initial_state = estimator.state();
    const Matrix2 initial_covariance = estimator.covariance();
    Vector2 state_copy = estimator.state();
    Matrix2 covariance_copy = estimator.covariance();
    state_copy.setConstant(200.0);
    covariance_copy.setZero();
    check_matrix(estimator.state(), initial_state, 0.0);
    check_matrix(estimator.covariance(), initial_covariance, 0.0);

    Vector2 observation = meridian::gravity_observation(0.31);
    auto innovation = estimator.update(observation);
    const Vector2 corrected_state = estimator.state();
    const Matrix2 corrected_covariance = estimator.covariance();
    observation.setZero();
    innovation.residual.setZero();
    innovation.covariance.setZero();
    check_matrix(estimator.state(), corrected_state, 0.0);
    check_matrix(estimator.covariance(), corrected_covariance, 0.0);

    const EkfConfig certain{0.1, 0.02, 0.0, 0.0, 0.0, 0.2, gravity};
    AngleBiasEKF fixed(certain);
    fixed.predict(0.12, 0.5);
    fixed.update(meridian::gravity_observation(2.0));
    check_matrix(fixed.state(), Vector2(0.15, 0.02));
    check_matrix(fixed.covariance(), Matrix2::Zero(), 0.0);
}

void test_nominal_convergence_and_covariance() {
    const EkfConfig config{0.0, 0.0, 0.0, 0.1, 0.01, 0.2, gravity};
    AngleBiasEKF estimator(config);
    constexpr double bias = 0.01;
    double time = 0.0;
    double previous_angle = 0.0;
    for (int index = 1; index <= 3000; ++index) {
        const double dt = (index % 3 == 0) ? 0.012 : 0.009;
        time += dt;
        const double true_angle = 0.3 * std::sin(0.7 * time);
        estimator.predict((true_angle - previous_angle) / dt + bias, dt);
        if (index % 10 == 0) {
            estimator.update(meridian::gravity_observation(true_angle));
        }
        CHECK(estimator.state().allFinite());
        check_covariance(estimator.covariance());
        previous_angle = true_angle;
    }
    check_near(estimator.state()(0), previous_angle, 2e-6);
    check_near(estimator.state()(1), bias, 2e-6);
}

void test_invalid_configuration_and_helpers() {
    const double infinity = std::numeric_limits<double>::infinity();
    const double nan = std::numeric_limits<double>::quiet_NaN();
    const std::array<double EkfConfig::*, 5> deviations{{
        &EkfConfig::initial_angle_std_rad, &EkfConfig::initial_bias_std_rad_s,
        &EkfConfig::gyro_noise_std_rad_s, &EkfConfig::accel_noise_std_m_s2,
        &EkfConfig::bias_random_walk_std_rad_s_per_sqrt_s}};
    for (const auto member : deviations) {
        for (double invalid : {-0.1, infinity, -infinity, nan, 1e200}) {
            EkfConfig config = configuration();
            config.*member = invalid;
            expect_exception<std::invalid_argument>([&] { AngleBiasEKF invalid_estimator(config); });
        }
    }
    for (double invalid : {0.0, 1e-200}) {
        EkfConfig config = configuration();
        config.accel_noise_std_m_s2 = invalid;
        expect_exception<std::invalid_argument>([&] { AngleBiasEKF invalid_estimator(config); });
    }
    for (double invalid : {infinity, -infinity, nan}) {
        EkfConfig config = configuration();
        config.initial_angle_rad = invalid;
        expect_exception<std::invalid_argument>([&] { AngleBiasEKF invalid_estimator(config); });
        config = configuration();
        config.initial_bias_rad_s = invalid;
        expect_exception<std::invalid_argument>([&] { AngleBiasEKF invalid_estimator(config); });
        expect_exception<std::invalid_argument>([&] { meridian::gravity_observation(invalid); });
        expect_exception<std::invalid_argument>([&] { meridian::gravity_jacobian(invalid); });
    }
    for (double invalid : {0.0, -1.0, infinity, -infinity, nan}) {
        EkfConfig config = configuration();
        config.gravity_m_s2 = invalid;
        expect_exception<std::invalid_argument>([&] { AngleBiasEKF invalid_estimator(config); });
        expect_exception<std::invalid_argument>([&] { meridian::gravity_observation(0.0, invalid); });
        expect_exception<std::invalid_argument>([&] { meridian::gravity_jacobian(0.0, invalid); });
    }
    expect_exception<std::invalid_argument>([] { AngleBiasEKF invalid_estimator(EkfConfig{}); });
}

void test_invalid_operations_are_atomic() {
    AngleBiasEKF estimator(configuration());
    estimator.predict(0.2, 0.1);
    estimator.update(meridian::gravity_observation(0.4));
    const Vector2 prior_state = estimator.state();
    const Matrix2 prior_covariance = estimator.covariance();
    const double infinity = std::numeric_limits<double>::infinity();
    const double nan = std::numeric_limits<double>::quiet_NaN();
    for (double invalid : {0.0, -0.1, infinity, nan}) {
        expect_exception<std::invalid_argument>([&] { estimator.predict(0.0, invalid); });
        check_matrix(estimator.state(), prior_state, 0.0);
        check_matrix(estimator.covariance(), prior_covariance, 0.0);
    }
    for (double invalid : {infinity, -infinity, nan}) {
        expect_exception<std::invalid_argument>([&] { estimator.predict(invalid, 0.1); });
        for (int component = 0; component < 2; ++component) {
            Vector2 invalid_observation(0.0, -gravity);
            invalid_observation(component) = invalid;
            expect_exception<std::invalid_argument>([&] { estimator.update(invalid_observation); });
        }
        check_matrix(estimator.state(), prior_state, 0.0);
        check_matrix(estimator.covariance(), prior_covariance, 0.0);
    }

    expect_exception<std::runtime_error>([&] { estimator.predict(1e308, 1e308); });
    check_matrix(estimator.state(), prior_state, 0.0);
    check_matrix(estimator.covariance(), prior_covariance, 0.0);
    // Covariance overflow must reject the operation even if the angle is finite.
    expect_exception<std::runtime_error>([&] { estimator.predict(prior_state(1), 1e308); });
    check_matrix(estimator.state(), prior_state, 0.0);
    check_matrix(estimator.covariance(), prior_covariance, 0.0);

    EkfConfig huge_config = configuration();
    huge_config.gravity_m_s2 = 1e308;
    huge_config.initial_angle_rad = 0.0;
    AngleBiasEKF huge_gravity(huge_config);
    const Vector2 huge_state = huge_gravity.state();
    const Matrix2 huge_covariance = huge_gravity.covariance();
    expect_exception<std::runtime_error>([&] { huge_gravity.update(Vector2(0.0, 1e308)); });
    check_matrix(huge_gravity.state(), huge_state, 0.0);
    check_matrix(huge_gravity.covariance(), huge_covariance, 0.0);

    // Here residual and S remain finite; the correction itself overflows.
    EkfConfig correction_config = configuration();
    correction_config.initial_angle_rad = 0.0;
    correction_config.gravity_m_s2 = 1e-3;
    correction_config.accel_noise_std_m_s2 = 1e-6;
    AngleBiasEKF correction_overflow(correction_config);
    const Vector2 correction_state = correction_overflow.state();
    const Matrix2 correction_covariance = correction_overflow.covariance();
    expect_exception<std::runtime_error>([&] {
        correction_overflow.update(Vector2(1e308, 0.0));
    });
    check_matrix(correction_overflow.state(), correction_state, 0.0);
    check_matrix(correction_overflow.covariance(), correction_covariance, 0.0);

    // A positive mathematical R can disappear at the working precision. Reject
    // a nonpositive LDLT pivot rather than accepting a singular correction.
    EkfConfig singular_config = configuration();
    singular_config.initial_angle_rad = pi / 4.0;
    singular_config.accel_noise_std_m_s2 = 1e-100;
    AngleBiasEKF singular(singular_config);
    const Vector2 singular_state = singular.state();
    const Matrix2 singular_covariance = singular.covariance();
    expect_exception<std::runtime_error>([&] {
        singular.update(meridian::gravity_observation(pi / 4.0));
    });
    check_matrix(singular.state(), singular_state, 0.0);
    check_matrix(singular.covariance(), singular_covariance, 0.0);
}

}  // namespace

int main() {
    try {
        test_gravity_geometry_and_jacobian();
        test_analytical_prediction_and_bias_correction();
        test_bias_random_walk_analytical_covariance();
        test_bias_random_walk_interval_composition();
        test_bias_random_walk_reopens_bias_uncertainty();
        test_zero_bias_random_walk_preserves_reference();
        test_bias_random_walk_overflow_is_atomic();
        test_independent_scalar_correction_oracle();
        test_radial_zero_and_opposite_observations();
        test_value_ownership_and_zero_uncertainty();
        test_nominal_convergence_and_covariance();
        test_invalid_configuration_and_helpers();
        test_invalid_operations_are_atomic();
        std::cout << "AngleBiasEKF: " << checks << " checks passed\n";
        return 0;
    } catch (const std::exception& error) {
        std::cerr << "AngleBiasEKF test failure: " << error.what() << '\n';
        return 1;
    }
}
