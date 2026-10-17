// SPDX-License-Identifier: GPL-3.0-or-later
// Meridian's sensor-only, synchronous OpenVINS adapter. See LICENSE and NOTICE.md.
#include "core/VioManager.h"
#include "core/VioManagerOptions.h"
#include "state/State.h"
#include "utils/opencv_yaml_parse.h"
#include "utils/print.h"

#include <opencv2/imgcodecs.hpp>
#include <Eigen/Geometry>
#include <sys/resource.h>

#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <filesystem>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <limits>
#include <optional>
#include <sstream>
#include <stdexcept>
#include <string>
#include <vector>

namespace fs = std::filesystem;
using Clock = std::chrono::steady_clock;
using Ns = std::int64_t;

namespace {
struct ImageRow { Ns timestamp; std::string filename; };
struct ImuRow { Ns timestamp; Eigen::Vector3d gyro; Eigen::Vector3d accel; };
struct Status {
  std::string state = "failed", error;
  std::size_t imu_input = 0, cam0_input = 0, cam1_input = 0, imu_processed = 0;
  std::size_t camera_pairs_processed = 0, camera_pairs_without_imu_bracket = 0, trajectory_states = 0;
  std::optional<Ns> first_camera, last_camera, first_estimate, last_estimate, initialized;
  Clock::time_point started = Clock::now();
};

std::string trim(std::string value) {
  const auto start = value.find_first_not_of(" \t\r\n");
  if (start == std::string::npos) return "";
  return value.substr(start, value.find_last_not_of(" \t\r\n") - start + 1);
}

std::vector<std::string> fields(const std::string &line) {
  std::vector<std::string> result;
  std::stringstream stream(line);
  std::string field;
  while (std::getline(stream, field, ',')) result.push_back(trim(field));
  if (!line.empty() && line.back() == ',') result.emplace_back();
  return result;
}

Ns timestamp(const std::string &value) {
  if (value.empty() || value.find_first_not_of("0123456789") != std::string::npos)
    throw std::runtime_error("Timestamp must be a nonnegative integer in nanoseconds");
  std::size_t consumed = 0;
  const auto result = std::stoll(value, &consumed);
  if (consumed != value.size()) throw std::runtime_error("Invalid timestamp");
  return result;
}

double scalar(const std::string &value) {
  std::size_t consumed = 0;
  const double result = std::stod(value, &consumed);
  if (consumed != value.size() || !std::isfinite(result))
    throw std::runtime_error("IMU values must be finite numbers");
  return result;
}

template <typename Row, typename Parse>
std::vector<Row> read_csv(const fs::path &path, std::size_t columns, Parse parse) {
  if (!fs::is_regular_file(path) || fs::is_symlink(path))
    throw std::runtime_error("Missing regular CSV file: " + path.string());
  std::ifstream input(path);
  if (!input) throw std::runtime_error("Cannot read CSV: " + path.string());
  std::vector<Row> rows;
  std::string line;
  std::size_t number = 0;
  while (std::getline(input, line)) {
    ++number;
    line = trim(line);
    if (line.empty() || line[0] == '#') continue;
    try {
      const auto parts = fields(line);
      if (parts.size() != columns) throw std::runtime_error("Unexpected CSV column count");
      auto row = parse(parts);
      if (!rows.empty() && row.timestamp <= rows.back().timestamp)
        throw std::runtime_error("Timestamps must be strictly increasing; rows are never sorted");
      rows.push_back(std::move(row));
    } catch (const std::exception &error) {
      throw std::runtime_error(path.string() + ":" + std::to_string(number) + ": " + error.what());
    }
  }
  if (input.bad() || rows.empty()) throw std::runtime_error("Empty or unreadable CSV: " + path.string());
  return rows;
}

std::vector<ImageRow> read_images(const fs::path &path) {
  return read_csv<ImageRow>(path, 2, [](const auto &parts) {
    const fs::path name(parts[1]);
    if (name.empty() || name.has_parent_path() || name.filename() != name || name.extension() != ".png")
      throw std::runtime_error("Image filename must be a simple relative .png filename");
    return ImageRow{timestamp(parts[0]), parts[1]};
  });
}

std::vector<ImuRow> read_imu(const fs::path &path) {
  return read_csv<ImuRow>(path, 7, [](const auto &parts) {
    return ImuRow{timestamp(parts[0]), {scalar(parts[1]), scalar(parts[2]), scalar(parts[3])},
                  {scalar(parts[4]), scalar(parts[5]), scalar(parts[6])}};
  });
}

cv::Mat load_image(const fs::path &path, int width, int height) {
  if (!fs::is_regular_file(path) || fs::is_symlink(path))
    throw std::runtime_error("Missing regular image: " + path.string());
  cv::Mat image = cv::imread(path.string(), cv::IMREAD_UNCHANGED);
  if (image.empty() || image.cols != width || image.rows != height)
    throw std::runtime_error("Image decode or calibrated dimensions mismatch: " + path.string());
  if (image.type() == CV_16UC1) {
    // Same full-range scaling as cv_bridge's MONO16 -> MONO8 conversion.
    // No per-image normalization or photometric calibration is introduced.
    cv::Mat mono8;
    image.convertTo(mono8, CV_8UC1, 255.0 / 65535.0);
    return mono8;
  }
  if (image.type() != CV_8UC1)
    throw std::runtime_error("Only monochrome uint8/uint16 PNGs are supported: " + path.string());
  return image;
}

std::string json_string(const std::string &value) {
  std::ostringstream out;
  out << '"';
  for (unsigned char c : value) {
    if (c == '"' || c == '\\') out << '\\' << c;
    else if (c < 32) out << "\\u" << std::hex << std::setw(4) << std::setfill('0') << int(c) << std::dec;
    else out << c;
  }
  out << '"';
  return out.str();
}

std::string optional_ns(const std::optional<Ns> &value) {
  return value ? std::to_string(*value) : "null";
}

void write_status(const fs::path &output, const Status &s) {
  rusage usage{};
  if (getrusage(RUSAGE_SELF, &usage) != 0) throw std::runtime_error("getrusage failed");
  const double cpu = usage.ru_utime.tv_sec + usage.ru_stime.tv_sec +
                     (usage.ru_utime.tv_usec + usage.ru_stime.tv_usec) * 1e-6;
  const double wall = std::chrono::duration<double>(Clock::now() - s.started).count();
  std::ofstream out(output / "status.json");
  out.exceptions(std::ios::failbit | std::ios::badbit);
  out << std::setprecision(17)
      << "{\n  \"schema_version\": 1,\n  \"state\": " << json_string(s.state)
      << ",\n  \"error\": " << (s.error.empty() ? "null" : json_string(s.error))
      << ",\n  \"upstream_revision\": \"" << OPENVINS_REVISION << "\",\n"
      << "  \"counts\": {\"imu_input\": " << s.imu_input << ", \"cam0_input\": " << s.cam0_input
      << ", \"cam1_input\": " << s.cam1_input << ", \"imu_processed\": " << s.imu_processed
      << ", \"camera_pairs_processed\": " << s.camera_pairs_processed
      << ", \"camera_pairs_without_imu_bracket\": " << s.camera_pairs_without_imu_bracket
      << ", \"trajectory_states\": " << s.trajectory_states << "},\n"
      << "  \"first_camera_timestamp_ns\": " << optional_ns(s.first_camera) << ",\n"
      << "  \"last_camera_timestamp_ns\": " << optional_ns(s.last_camera) << ",\n"
      << "  \"first_estimate_timestamp_ns\": " << optional_ns(s.first_estimate) << ",\n"
      << "  \"last_estimate_timestamp_ns\": " << optional_ns(s.last_estimate) << ",\n"
      << "  \"initialized_timestamp_ns\": " << optional_ns(s.initialized) << ",\n"
      << "  \"initialization_delay_s\": ";
  if (s.first_estimate && s.first_camera) out << (*s.first_estimate - *s.first_camera) * 1e-9;
  else out << "null";
  out << ",\n  \"timing\": {\"wall_seconds\": " << wall << ", \"cpu_seconds\": " << cpu
      << ", \"peak_rss_kib\": " << usage.ru_maxrss << "},\n"
      << "  \"orientation\": \"Hamilton xyzw, IMU-body to local-world\",\n"
      << "  \"image_conversion\": \"uint16 to uint8: OpenCV convertTo scale 255/65535; uint8 unchanged\",\n"
      << "  \"ground_truth_access\": \"no reference input or initialization\"\n}\n";
  out.close();
}

void run(const fs::path &dataset, const fs::path &config, const fs::path &output, Status &status) {
  const fs::path mav = dataset / "mav0";
  const auto left = read_images(mav / "cam0/data.csv");
  status.cam0_input = left.size();
  const auto right = read_images(mav / "cam1/data.csv");
  status.cam1_input = right.size();
  const auto imu = read_imu(mav / "imu0/data.csv");
  status.imu_input = imu.size();
  status.first_camera = left.front().timestamp;
  status.last_camera = left.back().timestamp;
  if (left.size() != right.size()) throw std::runtime_error("Stereo camera row counts differ");
  for (std::size_t i = 0; i < left.size(); ++i)
    if (left[i].timestamp != right[i].timestamp)
      throw std::runtime_error("Prepared stereo inputs must have identical paired timestamps");
  const Ns origin = std::min(imu.front().timestamp, left.front().timestamp);
  const auto seconds = [origin](Ns value) { return static_cast<double>(value - origin) * 1e-9; };

  ov_core::Printer::setPrintLevel("WARNING");
  auto parser = std::make_shared<ov_core::YamlParser>(config.string());
  ov_msckf::VioManagerOptions options;
  options.print_and_load(parser);
  if (!parser->successful()) throw std::runtime_error("Incomplete OpenVINS configuration");
  if (options.state_options.num_cameras != 2 || !options.use_stereo || options.downsample_cameras)
    throw std::runtime_error("Adapter requires two synchronized stereo cameras without downsampling");
  if (options.record_timing_information)
    throw std::runtime_error("Use adapter timing; upstream output paths are disabled");
  options.num_opencv_threads = 1;
  options.use_multi_threading_pubs = false;
  options.use_multi_threading_subs = false;
  options.init_options.init_dyn_mle_max_threads = 1;
  cv::setNumThreads(1);
  cv::setRNGSeed(0);
  ov_msckf::VioManager estimator(options);
  std::ofstream trajectory(output / "trajectory.csv");
  trajectory.exceptions(std::ios::failbit | std::ios::badbit);
  trajectory << "timestamp_ns,px_m,py_m,pz_m,qx,qy,qz,qw\n" << std::setprecision(17);
  for (std::size_t i = 0; i < left.size(); ++i) {
    const double camera_time = seconds(left[i].timestamp);
    const double imu_time = camera_time + estimator.get_state()->_calib_dt_CAMtoIMU->value()(0);
    // Supply the first IMU strictly after the camera (including current offset),
    // as required for interpolation by the upstream serial ROS callback.
    while (status.imu_processed < imu.size() &&
           (status.imu_processed == 0 || seconds(imu[status.imu_processed - 1].timestamp) <= imu_time)) {
      const auto &row = imu[status.imu_processed];
      ov_core::ImuData measurement;
      measurement.timestamp = seconds(row.timestamp);
      measurement.wm = row.gyro;
      measurement.am = row.accel;
      estimator.feed_measurement_imu(measurement);
      ++status.imu_processed;
    }
    if (seconds(imu.front().timestamp) > imu_time ||
        seconds(imu[status.imu_processed - 1].timestamp) <= imu_time) {
      ++status.camera_pairs_without_imu_bracket;
      continue;
    }
    ov_core::CameraData measurement;
    measurement.timestamp = camera_time;
    measurement.sensor_ids = {0, 1};
    for (int camera = 0; camera < 2; ++camera) {
      const auto &row = camera == 0 ? left[i] : right[i];
      const auto model = options.camera_intrinsics.at(camera);
      auto image = load_image(mav / ("cam" + std::to_string(camera)) / "data" / row.filename,
                              model->w(), model->h());
      measurement.images.push_back(image);
      measurement.masks.push_back(options.use_mask ? options.masks.at(camera)
                                                   : cv::Mat::zeros(image.size(), CV_8UC1));
    }
    estimator.feed_measurement_camera(measurement);
    ++status.camera_pairs_processed;
    if (!estimator.initialized()) continue;
    const auto state = estimator.get_state();
    if (!status.initialized)
      status.initialized = origin + static_cast<Ns>(std::llround(estimator.initialized_time() * 1e9));
    // Never assign a new camera timestamp to a delayed or unchanged estimate.
    if (std::abs(state->_timestamp - camera_time) > 1e-9) continue;
    const auto position = state->_imu->pos();
    // Upstream stores R_GtoI (JPL convention). Transpose that rotation matrix
    // and construct a Hamilton quaternion, avoiding ambiguous component copying.
    Eigen::Quaterniond rotation(state->_imu->Rot().transpose());
    if (!position.allFinite() || !rotation.coeffs().allFinite() || rotation.norm() < 1e-12)
      throw std::runtime_error("Estimator produced a nonfinite or degenerate pose");
    rotation.normalize();
    if (rotation.w() < 0) rotation.coeffs() *= -1;
    trajectory << left[i].timestamp << ',' << position.x() << ',' << position.y() << ',' << position.z()
               << ',' << rotation.x() << ',' << rotation.y() << ',' << rotation.z() << ',' << rotation.w() << '\n';
    if (!status.first_estimate) status.first_estimate = left[i].timestamp;
    status.last_estimate = left[i].timestamp;
    ++status.trajectory_states;
  }
  trajectory.close();
  status.state = status.trajectory_states > 0 ? "complete" : "uninitialized";
}
} // namespace

int main(int argc, char **argv) {
  fs::path dataset, config, output;
  Status status;
  bool output_ready = false;
  try {
    if (argc == 2 && std::string(argv[1]) == "--version") {
      std::cout << "{\"schema_version\":1,\"protocol_version\":1,\"adapter\":\"meridian-openvins\",\"upstream_revision\":\""
                << OPENVINS_REVISION << "\",\"adapter_sha256\":\"" << ADAPTER_SOURCE_SHA256
                << "\",\"opencv_version\":\"" << CV_VERSION << "\"}\n";
      return 0;
    }
    if (argc == 2 && std::string(argv[1]) == "--help") {
      std::cout << "meridian-openvins --dataset SENSOR_ROOT --config CONFIG_YAML --output OUTPUT_DIRECTORY\n";
      return 0;
    }
    if (argc != 7) throw std::runtime_error("Expected --dataset, --config and --output; see --help");
    for (int i = 1; i < argc; i += 2) {
      const std::string key(argv[i]);
      fs::path *destination = key == "--dataset" ? &dataset : key == "--config" ? &config : key == "--output" ? &output : nullptr;
      if (!destination || !destination->empty()) throw std::runtime_error("Unknown or repeated argument: " + key);
      *destination = argv[i + 1];
    }
    if (dataset.empty() || config.empty() || output.empty()) throw std::runtime_error("Empty required path");
    if (!fs::is_directory(output)) throw std::runtime_error("Output directory must already exist");
    if (fs::exists(output / "trajectory.csv") || fs::exists(output / "status.json"))
      throw std::runtime_error("Refusing to overwrite existing trajectory or status");
    output_ready = true;
    run(dataset, config, output, status);
    write_status(output, status);
    return status.state == "complete" ? 0 : 3;
  } catch (const std::exception &error) {
    status.state = "failed";
    status.error = error.what();
    std::cerr << "meridian-openvins: " << error.what() << '\n';
    if (output_ready) {
      try { write_status(output, status); }
      catch (const std::exception &failure) { std::cerr << "Cannot write failure status: " << failure.what() << '\n'; }
    }
    return 2;
  }
}
