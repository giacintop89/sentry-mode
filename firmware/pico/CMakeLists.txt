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
## The device build compiles the same modules for the board and adds one program that runs
## them there, reporting over USB serial. It needs the SDK, which is not vendored:
##
##   cmake -S firmware/pico -B build/pico2w -G Ninja \
##         -DSENTRY_PICO_TARGET=device -DPICO_BOARD=pico2_w -DPICO_SDK_PATH=~/.local/share/pico-sdk
##   cmake --build build/pico2w          # build/pico2w/sentry_firmware.uf2
##
## There is no network in that program yet. It exists because the modules had only ever run
## on a computer, and a serializer that agrees with itself on two architectures is worth
## more than one that has only been seen on the machine it was written on.

cmake_minimum_required(VERSION 3.20)

set(SENTRY_PICO_TARGET "host" CACHE STRING "host (the pure modules and their tests) or device")

if(NOT SENTRY_PICO_TARGET STREQUAL "host" AND NOT SENTRY_PICO_TARGET STREQUAL "device")
  message(FATAL_ERROR "SENTRY_PICO_TARGET is host or device, not ${SENTRY_PICO_TARGET}")
endif()

if(SENTRY_PICO_TARGET STREQUAL "device")
  # The SDK decides the compiler, so it has to be imported before the project exists.
  if(NOT DEFINED PICO_BOARD)
    set(PICO_BOARD pico2_w)
  endif()
  # What of mbedTLS is compiled in, which is decided by this firmware and not by whoever
  # builds it. It has to be set before the SDK is imported: that is when the libraries
  # that carry the definition are declared.
  set(PICO_MBEDTLS_CONFIG_FILE sentry_mbedtls_config.h)
  include(pico_sdk_import.cmake)
endif()

project(sentry_pico LANGUAGES C CXX ASM)

set(CMAKE_C_STANDARD 11)
set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

if(SENTRY_PICO_TARGET STREQUAL "device")
  pico_sdk_init()
endif()

# Where the contracts live, so the tests read the same fixtures as the hub and the agent.
get_filename_component(SENTRY_CONTRACTS_DIR "${CMAKE_CURRENT_SOURCE_DIR}/../../contracts/satellite/v1" ABSOLUTE)

add_library(sentry_protocol
  src/protocol/json.cpp
  src/protocol/names.cpp
  src/protocol/command.cpp
  src/protocol/control.cpp
  src/protocol/topics.cpp
  src/protocol/mqtt.cpp
  src/protocol/client.cpp
  src/protocol/event.cpp
  src/protocol/lease.cpp
  src/core/store.cpp
  src/core/identity.cpp
  src/core/credentials.cpp
  src/core/timebase.cpp
  src/core/spool.cpp
  src/core/session.cpp
  src/core/input.cpp
  src/core/sensors.cpp
  src/core/pins.cpp
  src/core/plan.cpp
)
target_include_directories(sentry_protocol PUBLIC include)
target_compile_options(sentry_protocol PRIVATE
  -Wall -Wextra -Wpedantic -Wconversion -Wshadow -Werror
  # Nothing here throws and nothing here asks what type anything is at runtime. Compiled
  # this way, a change that needs either of those fails here rather than on the board.
  -fno-exceptions -fno-rtti
)

if(SENTRY_PICO_TARGET STREQUAL "device")
  # One program, and the same library the host tests are run against. What it proves is
  # that these modules behave on the chip as they do on a computer; it is not a satellite.
  add_executable(sentry_firmware src/device/main.cpp src/device/net.cpp src/device/onewire.cpp)
  # lwipopts.h lives beside them, because what of lwIP this firmware compiles in is part of
  # the firmware and not of whoever builds it.
  target_include_directories(sentry_firmware PRIVATE src/device)
  target_link_libraries(sentry_firmware PRIVATE
    sentry_protocol
    pico_stdlib
    pico_rand
    pico_unique_id
    hardware_adc
  )
  if(PICO_CYW43_SUPPORTED)
    # The background context: lwIP runs from an alarm and the main loop never blocks on it.
    target_link_libraries(sentry_firmware PRIVATE
      pico_cyw43_arch_lwip_threadsafe_background
      pico_lwip_sntp
      # TLS, and the only way this firmware has of reaching the broker. There is no plain
      # TCP client here to fall back to.
      pico_lwip_mbedtls
      pico_mbedtls
    )
  endif()
  target_compile_options(sentry_firmware PRIVATE -Wall -Wextra -Wshadow -Werror)
  # Over USB, so one cable carries the power and everything this program says. The UART
  # stays off: the pins it would take are pins a source may be configured on.
  pico_enable_stdio_usb(sentry_firmware 1)
  pico_enable_stdio_uart(sentry_firmware 0)
  pico_add_extra_outputs(sentry_firmware)
  return()
endif()

enable_testing()

foreach(suite json command control event lease topics mqtt client session store identity credentials timebase spool input sensors pins plan)
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

# Writes the packets this node would put on the wire, for tools/mqtt_check.py to decode
# with an implementation that has never seen mqtt.cpp.
add_executable(sentry_mqtt_emit tools/mqtt_emit.cpp)
target_link_libraries(sentry_mqtt_emit PRIVATE sentry_protocol)
target_compile_options(sentry_mqtt_emit PRIVATE -Wall -Wextra -Werror)

# Drives the client through one whole connection, with a broker made of bytes from the
# specification, and prints the packets in the order it wrote them.
add_executable(sentry_client_run tools/client_run.cpp)
target_link_libraries(sentry_client_run PRIVATE sentry_protocol)
target_compile_options(sentry_client_run PRIVATE -Wall -Wextra -Werror)

# Reads two configuration slots the way the firmware would at boot. See
# tools/pack_provisioning.py, which writes them with its own framing and checks the answer.
add_executable(sentry_unpack tools/unpack.cpp)
target_link_libraries(sentry_unpack PRIVATE sentry_protocol)
target_compile_options(sentry_unpack PRIVATE -Wall -Wextra -Werror)
