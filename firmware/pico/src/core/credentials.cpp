#include "sentry/credentials.h"

#include <cstdio>
#include <cstring>

#include "sentry/json.h"

namespace sentry {
namespace {

// The label a private key may carry. `EC PRIVATE KEY` is what OpenSSL writes for a P-256
// key made the old way and `PRIVATE KEY` is PKCS#8; both are keys, and neither is a
// certificate, which is the distinction that matters here.
bool is_private_key(const char* text, size_t size) {
  return is_pem(text, size, "PRIVATE KEY") || is_pem(text, size, "EC PRIVATE KEY");
}

bool keep(const char* pem, size_t size, char* out, size_t capacity, size_t& kept) {
  if (pem == nullptr || size == 0) return false;
  if (size + 1 > capacity) return false;
  std::memcpy(out, pem, size);
  out[size] = '\0';
  kept = size + 1;  // mbedTLS counts the terminator for PEM
  return true;
}

void wipe(char* text, size_t capacity) {
  volatile char* at = text;
  for (size_t index = 0; index < capacity; ++index) at[index] = '\0';
}

}  // namespace

bool is_pem(const char* text, size_t size, const char* label) {
  if (text == nullptr || label == nullptr || size == 0) return false;
  char opening[64];
  char closing[64];
  const int opened = std::snprintf(opening, sizeof(opening), "-----BEGIN %s-----", label);
  const int closed = std::snprintf(closing, sizeof(closing), "-----END %s-----", label);
  if (opened <= 0 || closed <= 0) return false;
  const size_t opening_length = static_cast<size_t>(opened);
  const size_t closing_length = static_cast<size_t>(closed);
  if (size < opening_length + closing_length) return false;
  if (std::memcmp(text, opening, opening_length) != 0) return false;

  // The end marker, and nothing of consequence after it. A file with a second block in it
  // is a chain this firmware was not asked for, and is refused rather than half read.
  const char* end = nullptr;
  for (size_t at = opening_length; at + closing_length <= size; ++at) {
    if (std::memcmp(text + at, closing, closing_length) == 0) {
      end = text + at + closing_length;
      break;
    }
  }
  if (end == nullptr) return false;
  for (const char* after = end; after < text + size; ++after) {
    if (*after != '\n' && *after != '\r') return false;
  }
  for (size_t index = 0; index < size; ++index) {
    const unsigned char c = static_cast<unsigned char>(text[index]);
    const bool printable = c >= 0x20 && c < 0x7f;
    if (!printable && c != '\n' && c != '\r') return false;
  }
  return true;
}

bool Credentials::take_authority(const char* pem, size_t size) {
  if (!is_pem(pem, size, "CERTIFICATE")) return false;
  return keep(pem, size, authority_, sizeof(authority_), authority_size_);
}

bool Credentials::take_certificate(const char* pem, size_t size) {
  if (!is_pem(pem, size, "CERTIFICATE")) return false;
  return keep(pem, size, certificate_, sizeof(certificate_), certificate_size_);
}

bool Credentials::take_private_key(const char* pem, size_t size) {
  if (!is_private_key(pem, size)) return false;
  return keep(pem, size, private_key_, sizeof(private_key_), private_key_size_);
}

void Credentials::forget() {
  wipe(authority_, sizeof(authority_));
  wipe(certificate_, sizeof(certificate_));
  wipe(private_key_, sizeof(private_key_));
  authority_size_ = 0;
  certificate_size_ = 0;
  private_key_size_ = 0;
}

bool read_credentials(const char* document, size_t size, Credentials& out) {
  // Read into a copy and keep it only if the whole document was good: a node left with a
  // new certificate and an old key would fail a handshake for a reason nobody could see.
  static Credentials taken;
  taken = out;
  Reader reader(document, size);
  if (!reader.begin_object()) return false;
  Span key;
  Kind kind = Kind::kEnd;
  bool any = false;
  while (reader.member(key, kind)) {
    if (kind == Kind::kEnd) break;
    if (key.is("ca") || key.is("cert") || key.is("key")) {
      if (kind != Kind::kString) return false;
      // The buffer has to hold the largest of the three, because which one this is is
      // known only after it has been read.
      static char pem[kMaxCertificateText];
      size_t length = 0;
      if (!reader.string(pem, sizeof(pem) - 1, length)) return false;
      pem[length] = '\0';
      const bool kept = key.is("ca")     ? taken.take_authority(pem, length)
                        : key.is("cert") ? taken.take_certificate(pem, length)
                                         : taken.take_private_key(pem, length);
      if (!kept) return false;
      any = true;
    } else if (!reader.skip()) {
      return false;
    }
  }
  if (!any) return false;
  out = taken;
  taken.forget();
  return true;
}

}  // namespace sentry
