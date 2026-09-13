#pragma once

#include <Eigen/Core>

namespace meridian {

using Vector2 = Eigen::Vector2d;
using Matrix2 = Eigen::Matrix2d;

/// All standard deviations are per sample, not continuous-time noise densities.
/// Set accel_noise_std_m_s2 to a strictly positive value before construction.
struct EkfConfig {
    double initial_angle_rad = 0.0;
    double initial_bias_rad_s = 0.0;
    double initial_angle_std_rad = 0.0;
    double initial_bias_std_rad_s = 0.0;
    double gyro_noise_std_rad_s = 0.0;
    double accel_noise_std_m_s2 = 0.0;
    double gravity_m_s2 = 9.80665;
};

/// Predicted body [f_y, f_z] in m/s^2 for pure roll, forward/right/down axes.
/// A level sensor measures [0, -g]; positive roll makes f_y negative.
Vector2 gravity_observation(double angle_rad, double gravity_m_s2 = 9.80665);

/// Observation derivative with respect to [angle_rad, bias_rad_s].
Matrix2 gravity_jacobian(double angle_rad, double gravity_m_s2 = 9.80665);

struct Innovation {
    Vector2 residual;    ///< Prior [f_y, f_z] innovation in m/s^2.
    Matrix2 covariance;  ///< Prior innovation covariance in (m/s^2)^2.
};

/// Unwrapped roll and constant gyro-bias EKF with an unnormalized gravity vector.
///
/// State order is [angle_rad, bias_rad_s]. Prediction consumes one interval's
/// mean gyro rate and uses Q = diag((gyro_noise_std_rad_s * dt_s)^2, 0).
/// There is no bias random walk. The caller owns time ordering and scheduling.
/// Correction linearizes once at the prior, solves a joint 2 x 2 system with
/// R = accel_noise_std_m_s2^2 * I, and uses the Joseph covariance formula.
///
/// This is a local pure-roll model, with no translation compensation, force
/// normalization, rejection gate, or angle wrapping. Finite zero and opposite
/// force vectors are accepted; arbitrary-angle convergence is not guaranteed.
/// Accessors return values and failed predict/update operations are atomic.
/// Invalid inputs throw std::invalid_argument; numerical range or solve failures
/// throw std::runtime_error. No embedded execution or timing claim is implied.
class AngleBiasEKF {
public:
    explicit AngleBiasEKF(const EkfConfig& config);

    Vector2 state() const;
    Matrix2 covariance() const;

    void predict(double rate_rad_s, double dt_s);
    Innovation update(const Vector2& force_yz_m_s2);

private:
    Vector2 state_;
    Matrix2 covariance_;
    double gyro_noise_std_rad_s_;
    Matrix2 accel_covariance_;
    double gravity_m_s2_;
};

}  // namespace meridian
