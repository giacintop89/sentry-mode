## The Pico satellite firmware, and the part of it that can be tested without a board.
##
## Everything under src/protocol is pure: it reads and writes the satellite contract in
## buffers the caller owns, with no SDK, no allocation, no clock and no network. That is
## deliberate, and it is what this file builds by default — a host build with the tests,
## which runs anywhere and is the only thing in this directory that has ever been run.
##
##   cmake -S firmware/pico -B build/pico-host -G Ninja
##   cmake --build build/pico-host
##   ctest --test-dir build/pico-host --output-on-failure
##
## The device build is not here yet. It needs a pinned SDK, a pinned lwIP and Mbed TLS, and
## a first measurement on a real RP2040 — the hardware half of PICO-00, which nobody has
## done. A CMakeLists that offered `-DPICO_BOARD=pico_w` today would be offering a build
## that has never produced a binary, which is worse than not offering it.

cmake_minimum_required(VERSION 3.20)
project(sentry_pico LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

set(SENTRY_PICO_TARGET "host" CACHE STRING "host (the pure modules and their tests) or device")

if(NOT SENTRY_PICO_TARGET STREQUAL "host")
  message(FATAL_ERROR
    "Only the host build exists. The device build waits on a pinned SDK and a first "
    "build on a real board; see docs/adr/pico-control-contracts.md.")
endif()

# Where the contracts live, so the tests read the same fixtures as the hub and the agent.
get_filename_component(SENTRY_CONTRACTS_DIR "${CMAKE_CURRENT_SOURCE_DIR}/../../contracts/satellite/v1" ABSOLUTE)

add_library(sentry_protocol
  src/protocol/json.cpp
  src/protocol/command.cpp
  src/protocol/event.cpp
  src/protocol/lease.cpp
)
target_include_directories(sentry_protocol PUBLIC include)
target_compile_options(sentry_protocol PRIVATE
  -Wall -Wextra -Wpedantic -Wconversion -Wshadow -Werror
  # Nothing here throws and nothing here asks what type anything is at runtime. Compiled
  # this way, a change that needs either of those fails here rather than on the board.
  -fno-exceptions -fno-rtti
)

enable_testing()

foreach(suite json command event lease)
  add_executable(test_${suite} tests/test_${suite}.cpp)
  target_link_libraries(test_${suite} PRIVATE sentry_protocol)
  target_include_directories(test_${suite} PRIVATE tests)
  target_compile_definitions(test_${suite} PRIVATE SENTRY_CONTRACTS_DIR="${SENTRY_CONTRACTS_DIR}")
  target_compile_options(test_${suite} PRIVATE -Wall -Wextra -Werror)
  add_test(NAME ${suite} COMMAND test_${suite})
endforeach()

# Writes events with the firmware's own serializer, for the hub's models to judge. See
# tools/check_against_contracts.py.
add_executable(sentry_emit tools/emit.cpp)
target_link_libraries(sentry_emit PRIVATE sentry_protocol)
target_compile_options(sentry_emit PRIVATE -Wall -Wextra -Werror)
