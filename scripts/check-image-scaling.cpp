// Check every uint16 value against the qualified runner's OpenCV conversion.
#include <opencv2/core.hpp>
#include <cstdint>
#include <iostream>

int main() {
    cv::Mat input(256, 256, CV_16UC1);
    for (int value = 0; value < 65536; ++value) {
        input.at<std::uint16_t>(value / 256, value % 256) = value;
    }
    cv::Mat output;
    input.convertTo(output, CV_8UC1, 255.0 / 65535.0);
    int mismatches = 0;
    for (int value = 0; value < 65536; ++value) {
        mismatches += output.at<std::uint8_t>(value / 256, value % 256)
                      != (value + 128) / 257;
    }
    std::cout << "{\"opencv_version\":\"" << CV_VERSION
              << "\",\"checked_values\":65536,\"mismatches\":"
              << mismatches << "}\n";
    return mismatches == 0 ? 0 : 1;
}
