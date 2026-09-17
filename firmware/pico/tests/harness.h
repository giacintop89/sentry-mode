// A test runner small enough to read in one sitting.
//
// The firmware has no test framework and does not want one: a dependency that runs on the
// host but not on the board is a dependency that decides what can be tested. A test is a
// function with a name, a check is a line and a reason, and a failure prints the file and
// line it happened on. That is all this needs to do.

#ifndef SENTRY_TEST_HARNESS_H
#define SENTRY_TEST_HARNESS_H

#include <cstdio>
#include <cstring>
#include <string>
#include <vector>

namespace harness {

struct Test {
  const char* name;
  void (*run)();
};

inline std::vector<Test>& all() {
  static std::vector<Test> tests;
  return tests;
}

inline int& failures() {
  static int count = 0;
  return count;
}

inline const char*& current() {
  static const char* name = "";
  return name;
}

struct Register {
  Register(const char* name, void (*run)()) { all().push_back(Test{name, run}); }
};

inline void failed(const char* file, int line, const char* what) {
  ++failures();
  std::printf("  FAIL %s\n    %s:%d: %s\n", current(), file, line, what);
}

inline int run_all(const char* suite) {
  std::printf("%s: %zu tests\n", suite, all().size());
  for (const Test& test : all()) {
    current() = test.name;
    int before = failures();
    test.run();
    if (failures() == before) std::printf("  ok   %s\n", test.name);
  }
  if (failures() > 0) std::printf("%s: %d failed\n", suite, failures());
  return failures() == 0 ? 0 : 1;
}

// Read a whole file, or return an empty string. Tests say which fixture they wanted.
inline std::string slurp(const std::string& path) {
  std::string content;
  std::FILE* file = std::fopen(path.c_str(), "rb");
  if (file == nullptr) return content;
  char chunk[4096];
  size_t read = 0;
  while ((read = std::fread(chunk, 1, sizeof(chunk), file)) > 0) content.append(chunk, read);
  std::fclose(file);
  return content;
}

}  // namespace harness

#define TEST(name)                                        \
  static void name();                                     \
  static harness::Register register_##name(#name, &name); \
  static void name()

#define CHECK(condition)                              \
  do {                                                \
    if (!(condition)) {                               \
      harness::failed(__FILE__, __LINE__, #condition); \
      return;                                         \
    }                                                 \
  } while (false)

#define CHECK_TEXT(actual, expected)                                            \
  do {                                                                          \
    if (std::strcmp((actual), (expected)) != 0) {                               \
      std::printf("    expected %s, got %s\n", (expected), (actual));           \
      harness::failed(__FILE__, __LINE__, #actual " != " #expected);            \
      return;                                                                   \
    }                                                                           \
  } while (false)

#endif  // SENTRY_TEST_HARNESS_H
