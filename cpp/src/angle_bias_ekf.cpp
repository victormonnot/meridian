#include "meridian/angle_bias_ekf.hpp"

#include <Eigen/Cholesky>

#include <cmath>
#include <stdexcept>
#include <string>

namespace meridian {
namespace {

void validate_gravity(double gravity_m_s2) {
    if (!std::isfinite(gravity_m_s2) || gravity_m_s2 <= 0.0) {
        throw std::invalid_argument("gravity_m_s2 must be finite and positive");
    }
}

void validate_angle(double angle_rad) {
    if (!std::isfinite(angle_rad)) {
        throw std::invalid_argument("angle_rad must be finite");
    }
}

double checked_variance(double standard_deviation, const char* name) {
    if (!std::isfinite(standard_deviation) || standard_deviation < 0.0) {
        throw std::invalid_argument(std::string(name) + " must be finite and nonnegative");
    }
    const double variance = standard_deviation * standard_deviation;
    if (!std::isfinite(variance)) {
        throw std::invalid_argument(std::string(name) + " must have a finite squared value");
    }
    return variance;
}

Matrix2 symmetrized(const Matrix2& matrix) {
    // Scale before addition to avoid overflowing a finite diagonal. Return an
    // evaluated value so transposition never aliases an assignment target.
    return (0.5 * matrix + 0.5 * matrix.transpose()).eval();
}

}  // namespace

Vector2 gravity_observation(double angle_rad, double gravity_m_s2) {
    validate_angle(angle_rad);
    validate_gravity(gravity_m_s2);
    return Vector2(-gravity_m_s2 * std::sin(angle_rad),
                   -gravity_m_s2 * std::cos(angle_rad));
}

Matrix2 gravity_jacobian(double angle_rad, double gravity_m_s2) {
    validate_angle(angle_rad);
    validate_gravity(gravity_m_s2);
    Matrix2 jacobian = Matrix2::Zero();
    jacobian(0, 0) = -gravity_m_s2 * std::cos(angle_rad);
    jacobian(1, 0) = gravity_m_s2 * std::sin(angle_rad);
    return jacobian;
}

AngleBiasEKF::AngleBiasEKF(const EkfConfig& config) {
    validate_angle(config.initial_angle_rad);
    if (!std::isfinite(config.initial_bias_rad_s)) {
        throw std::invalid_argument("initial_bias_rad_s must be finite");
    }
    validate_gravity(config.gravity_m_s2);
    const double angle_variance = checked_variance(
        config.initial_angle_std_rad, "initial_angle_std_rad");
    const double bias_variance = checked_variance(
        config.initial_bias_std_rad_s, "initial_bias_std_rad_s");
    checked_variance(config.gyro_noise_std_rad_s, "gyro_noise_std_rad_s");
    const double accel_variance = checked_variance(
        config.accel_noise_std_m_s2, "accel_noise_std_m_s2");
    if (accel_variance <= 0.0) {
        throw std::invalid_argument(
            "accel_noise_std_m_s2 must have a strictly positive variance");
    }

    state_ = Vector2(config.initial_angle_rad, config.initial_bias_rad_s);
    covariance_ = Matrix2::Zero();
    covariance_(0, 0) = angle_variance;
    covariance_(1, 1) = bias_variance;
    gyro_noise_std_rad_s_ = config.gyro_noise_std_rad_s;
    accel_covariance_ = accel_variance * Matrix2::Identity();
    gravity_m_s2_ = config.gravity_m_s2;
}

Vector2 AngleBiasEKF::state() const {
    return state_;
}

Matrix2 AngleBiasEKF::covariance() const {
    return covariance_;
}

void AngleBiasEKF::predict(double rate_rad_s, double dt_s) {
    if (!std::isfinite(rate_rad_s)) {
        throw std::invalid_argument("rate_rad_s must be finite");
    }
    if (!std::isfinite(dt_s) || dt_s <= 0.0) {
        throw std::invalid_argument("dt_s must be finite and positive");
    }

    Matrix2 transition = Matrix2::Identity();
    transition(0, 1) = -dt_s;
    Vector2 state = state_;
    state(0) += dt_s * (rate_rad_s - state(1));
    Matrix2 covariance = transition * covariance_ * transition.transpose();
    const double angle_noise_std = gyro_noise_std_rad_s_ * dt_s;
    covariance(0, 0) += angle_noise_std * angle_noise_std;
    if (!state.allFinite() || !covariance.allFinite()) {
        throw std::runtime_error("prediction exceeds the finite numerical range");
    }
    covariance = symmetrized(covariance);
    state_ = state;
    covariance_ = covariance;
}

Innovation AngleBiasEKF::update(const Vector2& force_yz_m_s2) {
    if (!force_yz_m_s2.allFinite()) {
        throw std::invalid_argument("force_yz_m_s2 must be finite");
    }
    const Vector2 expected = gravity_observation(state_(0), gravity_m_s2_);
    const Matrix2 jacobian = gravity_jacobian(state_(0), gravity_m_s2_);
    const Vector2 residual = force_yz_m_s2 - expected;
    const Matrix2 cross_covariance = covariance_ * jacobian.transpose();
    Matrix2 innovation_covariance = jacobian * cross_covariance + accel_covariance_;
    if (!residual.allFinite() || !cross_covariance.allFinite()
        || !innovation_covariance.allFinite()) {
        throw std::runtime_error("innovation exceeds the finite numerical range");
    }
    innovation_covariance = symmetrized(innovation_covariance);

    const Eigen::LDLT<Matrix2> decomposition(innovation_covariance);
    if (decomposition.info() != Eigen::Success
        || !decomposition.vectorD().allFinite()
        || !(decomposition.vectorD().array() > 0.0).all()) {
        throw std::runtime_error("innovation covariance is not numerically positive definite");
    }
    const Matrix2 gain = decomposition.solve(cross_covariance.transpose()).transpose();
    if (!gain.allFinite()) {
        throw std::runtime_error("innovation solve exceeds the finite numerical range");
    }

    const Vector2 state = state_ + gain * residual;
    const Matrix2 correction = Matrix2::Identity() - gain * jacobian;
    Matrix2 covariance = correction * covariance_ * correction.transpose()
        + gain * accel_covariance_ * gain.transpose();
    if (!state.allFinite() || !covariance.allFinite()) {
        throw std::runtime_error("correction exceeds the finite numerical range");
    }
    covariance = symmetrized(covariance);
    state_ = state;
    covariance_ = covariance;
    return Innovation{residual, innovation_covariance};
}

}  // namespace meridian
