cmake_minimum_required(VERSION 3.20)
project(Meridian VERSION 0.1.0 LANGUAGES CXX)

find_package(Eigen3 3.4 REQUIRED NO_MODULE)

add_library(meridian_ekf cpp/src/angle_bias_ekf.cpp)
target_include_directories(meridian_ekf PUBLIC cpp/include)
target_link_libraries(meridian_ekf PUBLIC Eigen3::Eigen)
target_compile_features(meridian_ekf PUBLIC cxx_std_17)
set_target_properties(meridian_ekf PROPERTIES CXX_EXTENSIONS OFF)

add_executable(meridian_ekf_replay cpp/apps/ekf_replay.cpp)
target_link_libraries(meridian_ekf_replay PRIVATE meridian_ekf)
target_compile_definitions(meridian_ekf_replay PRIVATE
    MERIDIAN_COMPILER_ID="${CMAKE_CXX_COMPILER_ID}"
    MERIDIAN_COMPILER_VERSION="${CMAKE_CXX_COMPILER_VERSION}"
    MERIDIAN_BUILD_TYPE="$<CONFIG>")

if(MSVC)
    target_compile_options(meridian_ekf PRIVATE /W4)
    target_compile_options(meridian_ekf_replay PRIVATE /W4)
else()
    target_compile_options(meridian_ekf PRIVATE -Wall -Wextra -Wpedantic)
    target_compile_options(meridian_ekf_replay PRIVATE -Wall -Wextra -Wpedantic)
endif()

include(CTest)
if(BUILD_TESTING)
    add_executable(meridian_ekf_tests cpp/tests/test_angle_bias_ekf.cpp)
    target_link_libraries(meridian_ekf_tests PRIVATE meridian_ekf)
    add_test(NAME ekf_core COMMAND meridian_ekf_tests)

    # Explicitly select the project's Python environment; C++ builds need no Python.
    set(MERIDIAN_TEST_PYTHON "" CACHE FILEPATH "Python interpreter with Meridian and pytest installed")
    if(MERIDIAN_TEST_PYTHON)
        add_test(NAME cpp_python_parity
            COMMAND "${CMAKE_COMMAND}" -E env
                "MERIDIAN_CPP_BINARY=$<TARGET_FILE:meridian_ekf_replay>"
                "${MERIDIAN_TEST_PYTHON}" -m pytest -q
                "${CMAKE_CURRENT_SOURCE_DIR}/tests/test_cpp_parity.py")
        set_tests_properties(cpp_python_parity PROPERTIES
            WORKING_DIRECTORY "${CMAKE_CURRENT_SOURCE_DIR}")
    endif()
endif()
