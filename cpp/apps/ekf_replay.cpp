#include "meridian/angle_bias_ekf.hpp"

#include <array>
#include <cerrno>
#include <cmath>
#include <cstdio>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <limits>
#include <locale>
#include <set>
#include <sstream>
#include <stdexcept>
#include <string>
#include <string_view>

#ifndef MERIDIAN_COMPILER_ID
#define MERIDIAN_COMPILER_ID "unknown"
#endif
#ifndef MERIDIAN_COMPILER_VERSION
#define MERIDIAN_COMPILER_VERSION "unknown"
#endif
#ifndef MERIDIAN_BUILD_TYPE
#define MERIDIAN_BUILD_TYPE "unknown"
#endif

namespace {

constexpr std::string_view input_header = "operation,value0,value1";
constexpr std::string_view output_header =
    "step,operation,angle_rad,bias_rad_s,p00,p01,p10,p11,v0,v1,s00,s01,s10,s11";

struct NumericOption {
    std::string_view name;
    double meridian::EkfConfig::*member;
    bool required = true;
};

constexpr std::array<NumericOption, 8> numeric_options{{
    {"--initial-angle-rad", &meridian::EkfConfig::initial_angle_rad},
    {"--initial-bias-rad-s", &meridian::EkfConfig::initial_bias_rad_s},
    {"--initial-angle-std-rad", &meridian::EkfConfig::initial_angle_std_rad},
    {"--initial-bias-std-rad-s", &meridian::EkfConfig::initial_bias_std_rad_s},
    {"--gyro-noise-std-rad-s", &meridian::EkfConfig::gyro_noise_std_rad_s},
    {"--accel-noise-std-m-s2", &meridian::EkfConfig::accel_noise_std_m_s2},
    {"--gravity-m-s2", &meridian::EkfConfig::gravity_m_s2},
    {"--bias-random-walk-std-rad-s-per-sqrt-s",
     &meridian::EkfConfig::bias_random_walk_std_rad_s_per_sqrt_s, false},
}};

struct Arguments {
    std::filesystem::path input;
    std::filesystem::path output;
    meridian::EkfConfig config;
};

double parse_number(const std::string& text, const std::string& context) {
    std::istringstream stream(text);
    stream.imbue(std::locale::classic());
    double value = 0.0;
    stream >> std::noskipws >> value;
    if (stream.fail() || stream.peek() != std::char_traits<char>::eof() ||
        !std::isfinite(value)) {
        throw std::invalid_argument(context + " must be a finite number without surrounding whitespace");
    }
    return value;
}

Arguments parse_arguments(int argc, char** argv) {
    Arguments result;
    std::set<std::string> seen;
    for (int i = 1; i < argc; ++i) {
        const std::string option(argv[i]);
        const NumericOption* numeric = nullptr;
        for (const auto& candidate : numeric_options) {
            if (option == candidate.name) {
                numeric = &candidate;
                break;
            }
        }
        if (option == "--help" || option == "--version") {
            throw std::invalid_argument(option + " must be used alone");
        }
        if (option != "--input" && option != "--output" && numeric == nullptr) {
            throw std::invalid_argument("unknown option: " + option);
        }
        if (!seen.insert(option).second) {
            throw std::invalid_argument("duplicate option: " + option);
        }
        if (i + 1 >= argc || std::string_view(argv[i + 1]).substr(0, 2) == "--") {
            throw std::invalid_argument("missing value for " + option);
        }
        const std::string value(argv[++i]);
        if (value.empty()) {
            throw std::invalid_argument("empty value for " + option);
        }
        if (option == "--input") {
            result.input = value;
        } else if (option == "--output") {
            result.output = value;
        } else {
            result.config.*(numeric->member) = parse_number(value, option);
        }
    }
    for (const auto* required : {"--input", "--output"}) {
        if (seen.count(required) == 0) {
            throw std::invalid_argument(std::string("missing required option: ") + required);
        }
    }
    for (const auto& required : numeric_options) {
        if (required.required && seen.count(std::string(required.name)) == 0) {
            throw std::invalid_argument("missing required option: " + std::string(required.name));
        }
    }
    return result;
}

void print_help() {
    std::cout
        << "Usage: meridian_ekf_replay --input EVENTS.csv --output ESTIMATES.csv\n"
        << "  --initial-angle-rad VALUE --initial-bias-rad-s VALUE\n"
        << "  --initial-angle-std-rad VALUE --initial-bias-std-rad-s VALUE\n"
        << "  --gyro-noise-std-rad-s VALUE --accel-noise-std-m-s2 VALUE\n"
        << "  --gravity-m-s2 VALUE\n"
        << "  [--bias-random-walk-std-rad-s-per-sqrt-s VALUE]\n\n"
        << "All options except bias random walk are required, with finite SI values.\n"
        << "Gyro and accelerometer noise standard deviations are per observation.\n"
        << "Bias random walk is a continuous density in rad/s/sqrt(s), default 0.\n"
        << "Input header: operation,value0,value1\n"
        << "Rows: predict,rate_rad_s,dt_s or update,force_y_m_s2,force_z_m_s2\n"
        << "The strict CSV protocol does not support quoting or blank rows.\n"
        << "Output includes the initial state and every subsequent event.\n"
        << "Existing output paths are refused. No raw sensor log import is performed.\n\n"
        << "Standalone options: --help, --version\n";
}

std::string json_string(std::string_view value) {
    std::ostringstream stream;
    stream.imbue(std::locale::classic());
    stream << '"';
    for (const unsigned char character : value) {
        if (character == '"' || character == '\\') {
            stream << '\\' << character;
        } else if (character < 0x20) {
            stream << "\\u" << std::hex << std::setw(4) << std::setfill('0')
                   << static_cast<unsigned int>(character) << std::dec;
        } else {
            stream << character;
        }
    }
    stream << '"';
    return stream.str();
}

void print_version() {
    std::ostringstream eigen_version;
    eigen_version.imbue(std::locale::classic());
    eigen_version << EIGEN_WORLD_VERSION << '.' << EIGEN_MAJOR_VERSION << '.' << EIGEN_MINOR_VERSION;
    std::cout << "{\"protocol_version\":1,\"compiler_id\":"
              << json_string(MERIDIAN_COMPILER_ID) << ",\"compiler_version\":"
              << json_string(MERIDIAN_COMPILER_VERSION) << ",\"build_type\":"
              << json_string(MERIDIAN_BUILD_TYPE) << ",\"eigen_version\":"
              << json_string(eigen_version.str()) << ",\"cxx_standard\":17}\n";
}

void write_row(std::ostream& output, std::size_t step, std::string_view operation,
               const meridian::AngleBiasEKF& filter,
               const meridian::Innovation* innovation = nullptr) {
    const auto state = filter.state();
    const auto covariance = filter.covariance();
    output << step << ',' << operation << ',' << state(0) << ',' << state(1)
           << ',' << covariance(0, 0) << ',' << covariance(0, 1)
           << ',' << covariance(1, 0) << ',' << covariance(1, 1);
    if (innovation == nullptr) {
        output << ",,,,,,";
    } else {
        output << ',' << innovation->residual(0) << ',' << innovation->residual(1)
               << ',' << innovation->covariance(0, 0) << ',' << innovation->covariance(0, 1)
               << ',' << innovation->covariance(1, 0) << ',' << innovation->covariance(1, 1);
    }
    output << '\n';
}

void strip_carriage_return(std::string& line) {
    if (!line.empty() && line.back() == '\r') {
        line.pop_back();
    }
}

std::string replay(const Arguments& arguments) {
    meridian::AngleBiasEKF filter(arguments.config);
    std::ifstream input(arguments.input, std::ios::binary);
    if (!input) {
        throw std::runtime_error("cannot open input file");
    }
    std::string line;
    if (!std::getline(input, line)) {
        throw std::invalid_argument("missing input header");
    }
    strip_carriage_return(line);
    if (line != input_header) {
        throw std::invalid_argument("input header must be operation,value0,value1");
    }

    std::ostringstream output;
    output.imbue(std::locale::classic());
    output << std::setprecision(std::numeric_limits<double>::max_digits10);
    output << output_header << '\n';
    write_row(output, 0, "init", filter);
    std::size_t step = 0;
    while (std::getline(input, line)) {
        strip_carriage_return(line);
        if (step == std::numeric_limits<std::size_t>::max()) {
            throw std::runtime_error("too many input events");
        }
        ++step;
        try {
            const auto first = line.find(',');
            const auto second = first == std::string::npos ? std::string::npos : line.find(',', first + 1);
            if (first == std::string::npos || second == std::string::npos ||
                line.find(',', second + 1) != std::string::npos) {
                throw std::invalid_argument("expected exactly three unquoted CSV fields");
            }
            const auto operation = line.substr(0, first);
            if (operation != "predict" && operation != "update") {
                throw std::invalid_argument("unknown operation: " + operation);
            }
            const double value0 = parse_number(line.substr(first + 1, second - first - 1), "value0");
            const double value1 = parse_number(line.substr(second + 1), "value1");
            if (operation == "predict") {
                filter.predict(value0, value1);
                write_row(output, step, operation, filter);
            } else {
                const meridian::Vector2 force(value0, value1);
                const auto innovation = filter.update(force);
                write_row(output, step, operation, filter, &innovation);
            }
        } catch (const std::exception& error) {
            throw std::runtime_error("input event " + std::to_string(step) + ": " + error.what());
        }
    }
    if (input.bad() || (!input.eof() && input.fail())) {
        throw std::runtime_error("failed while reading input file");
    }
    if (!output) {
        throw std::runtime_error("failed while formatting output");
    }
    return output.str();
}

void check_output_path(const Arguments& arguments) {
    if (std::filesystem::absolute(arguments.input).lexically_normal() ==
        std::filesystem::absolute(arguments.output).lexically_normal()) {
        throw std::invalid_argument("input and output paths must differ");
    }
    // symlink_status also catches dangling symlinks, which must not be followed.
    const auto status = std::filesystem::symlink_status(arguments.output);
    if (std::filesystem::exists(status)) {
        throw std::invalid_argument("output path already exists");
    }
}

void write_exclusively(const std::filesystem::path& path, const std::string& contents) {
    // The C11 exclusive mode also refuses paths created after the initial check.
    std::FILE* file = std::fopen(path.string().c_str(), "wbx");
    if (file == nullptr) {
        throw std::runtime_error("cannot create output file exclusively: " + std::string(std::strerror(errno)));
    }
    const bool written = std::fwrite(contents.data(), 1, contents.size(), file) == contents.size();
    const bool closed = std::fclose(file) == 0;
    if (!written || !closed) {
        std::error_code ignored;
        std::filesystem::remove(path, ignored);
        throw std::runtime_error("failed while writing output file");
    }
}

}  // namespace

int main(int argc, char** argv) {
    try {
        if (argc == 2 && std::string_view(argv[1]) == "--help") {
            print_help();
            return 0;
        }
        if (argc == 2 && std::string_view(argv[1]) == "--version") {
            print_version();
            return 0;
        }
        const auto arguments = parse_arguments(argc, argv);
        check_output_path(arguments);
        const auto output = replay(arguments);
        write_exclusively(arguments.output, output);
        return 0;
    } catch (const std::exception& error) {
        std::cerr << "meridian_ekf_replay: " << error.what() << '\n';
        return 1;
    }
}
