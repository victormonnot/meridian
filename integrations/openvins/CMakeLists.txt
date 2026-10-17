# SPDX-License-Identifier: GPL-3.0-or-later
cmake_minimum_required(VERSION 3.20)
project(meridian_openvins LANGUAGES CXX)

set(OPENVINS_SOURCE "" CACHE PATH "Pinned OpenVINS source checkout")
if(NOT EXISTS "${OPENVINS_SOURCE}/ov_msckf/src/core/VioManager.cpp")
  message(FATAL_ERROR "Set OPENVINS_SOURCE to the pinned OpenVINS checkout")
endif()
execute_process(COMMAND git -C "${OPENVINS_SOURCE}" rev-parse HEAD
  OUTPUT_VARIABLE OPENVINS_REVISION OUTPUT_STRIP_TRAILING_WHITESPACE
  COMMAND_ERROR_IS_FATAL ANY)
if(NOT OPENVINS_REVISION STREQUAL "69488123ed9362dd44b6f28e7f4680abbff1442b")
  message(FATAL_ERROR "Unexpected OpenVINS revision: ${OPENVINS_REVISION}")
endif()
find_package(Eigen3 REQUIRED)
find_package(OpenCV 4 REQUIRED COMPONENTS core imgproc imgcodecs features2d video calib3d)
find_package(Boost REQUIRED COMPONENTS system filesystem thread date_time)
find_package(Ceres REQUIRED)
find_package(Threads REQUIRED)

# The source selection follows upstream ov_msckf/cmake/ROS1.cmake's no-ROS
# library. C++17 is required by the pinned Ceres 2.2 headers. No upstream
# estimator equations are patched; ROS, GUI and simulator executables are absent.
file(GLOB_RECURSE CORE_SOURCES "${OPENVINS_SOURCE}/ov_core/src/*.cpp")
list(FILTER CORE_SOURCES EXCLUDE REGEX "/test_[^/]+\\.cpp$")
file(GLOB_RECURSE INIT_SOURCES "${OPENVINS_SOURCE}/ov_init/src/*.cpp")
list(FILTER INIT_SOURCES EXCLUDE REGEX "/test_[^/]+\\.cpp$")
list(FILTER INIT_SOURCES EXCLUDE REGEX "/Simulator\\.cpp$")
set(MSCKF_SOURCES
  state/State.cpp state/StateHelper.cpp state/Propagator.cpp
  core/VioManager.cpp core/VioManagerHelper.cpp
  update/UpdaterHelper.cpp update/UpdaterMSCKF.cpp
  update/UpdaterSLAM.cpp update/UpdaterZeroVelocity.cpp)
list(TRANSFORM MSCKF_SOURCES PREPEND "${OPENVINS_SOURCE}/ov_msckf/src/")
add_library(meridian_openvins SHARED ${CORE_SOURCES} ${INIT_SOURCES} ${MSCKF_SOURCES})
target_compile_features(meridian_openvins PUBLIC cxx_std_17)
target_compile_definitions(meridian_openvins PUBLIC ROS_AVAILABLE=0 ENABLE_ARUCO_TAGS=0)
target_compile_options(meridian_openvins PRIVATE -O2 -g0)
target_include_directories(meridian_openvins PUBLIC
  "${OPENVINS_SOURCE}/ov_core/src" "${OPENVINS_SOURCE}/ov_init/src"
  "${OPENVINS_SOURCE}/ov_msckf/src" ${OpenCV_INCLUDE_DIRS})
target_link_libraries(meridian_openvins PUBLIC Eigen3::Eigen ${OpenCV_LIBS}
  Boost::system Boost::filesystem Boost::thread Boost::date_time Ceres::ceres Threads::Threads)

file(SHA256 "${CMAKE_CURRENT_SOURCE_DIR}/runner.cpp" ADAPTER_SOURCE_SHA256)
add_executable(meridian-openvins runner.cpp)
target_link_libraries(meridian-openvins PRIVATE meridian_openvins)
target_compile_options(meridian-openvins PRIVATE -Wall -Wextra -Wpedantic)
target_compile_definitions(meridian-openvins PRIVATE
  OPENVINS_REVISION="${OPENVINS_REVISION}" ADAPTER_SOURCE_SHA256="${ADAPTER_SOURCE_SHA256}")
set_target_properties(meridian-openvins PROPERTIES INSTALL_RPATH "/usr/local/lib")
install(TARGETS meridian_openvins meridian-openvins LIBRARY DESTINATION lib RUNTIME DESTINATION bin)
