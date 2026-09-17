// The three things a connection to the broker is made of, and the one that must not leave.
//
// A satellite proves who it is with a certificate and verifies the hub with the authority
// that issued it. That is the whole of the trust here: nothing is accepted because it is
// valid, and there is no setting anywhere that turns verification off. A node that has not
// been given all three does not connect — it says so and waits, because a connection made
// without them would be one to anybody.
//
// PEM, and PEM as mbedTLS wants it: printable text, the right label, and a terminating
// zero counted in the length. What is checked here is that it is the kind of thing it was
// offered as. A private key handed over as a certificate is a mistake worth catching at
// the serial port rather than inside a handshake.
//
// The key is written once and read only by the TLS layer. There is no accessor that
// returns it as text to print, no counter of what is in it, and `forget()` overwrites it:
// the one thing this firmware holds that nobody may see again is the private key.

#ifndef SENTRY_CREDENTIALS_H
#define SENTRY_CREDENTIALS_H

#include <cstddef>
#include <cstdint>

namespace sentry {

// P-256 in PEM: a certificate is about 700 bytes and a key about 250. Room for a chain of
// two and an RSA key somebody insisted on, and not the 8 kB that would cost a tenth of a
// Pico W's memory for no reason.
inline constexpr size_t kMaxAuthorityText = 2048;
inline constexpr size_t kMaxCertificateText = 2048;
inline constexpr size_t kMaxPrivateKeyText = 1792;

class Credentials {
 public:
  // Each takes text, checks it is PEM of the kind it was offered as, and keeps a copy.
  // False leaves what was there before in place: half a certificate is worse than an old
  // one, and a node that lost its identity to a bad paste could not be reached to fix it.
  bool take_authority(const char* pem, size_t size);
  bool take_certificate(const char* pem, size_t size);
  bool take_private_key(const char* pem, size_t size);

  bool has_authority() const { return authority_size_ > 0; }
  bool has_certificate() const { return certificate_size_ > 0; }
  bool has_private_key() const { return private_key_size_ > 0; }
  // All three, which is the only state in which a connection may be attempted.
  bool complete() const { return has_authority() && has_certificate() && has_private_key(); }

  const char* authority() const { return authority_; }
  const char* certificate() const { return certificate_; }
  // Only the TLS layer has any business with this, and only when handing it to mbedTLS.
  const char* private_key() const { return private_key_; }

  // The length mbedTLS wants for PEM: the text and the zero that ends it.
  size_t authority_size() const { return authority_size_; }
  size_t certificate_size() const { return certificate_size_; }
  size_t private_key_size() const { return private_key_size_; }

  // Overwrite everything, key included. Used when a record is replaced and before a
  // reset, so that what is left in memory is not somebody's identity.
  void forget();

 private:
  char authority_[kMaxAuthorityText] = {};
  char certificate_[kMaxCertificateText] = {};
  char private_key_[kMaxPrivateKeyText] = {};
  size_t authority_size_ = 0;
  size_t certificate_size_ = 0;
  size_t private_key_size_ = 0;
};

// `{"ca": "...", "cert": "...", "key": "..."}`, as the provisioning tool sends it over the
// serial line with the newlines escaped. Every field is optional — an authority can be
// replaced without resending an identity — and a field that is present and wrong refuses
// the whole document rather than leaving a half-changed node.
bool read_credentials(const char* document, size_t size, Credentials& out);

// Whether `text` is a PEM block with this label, from `-----BEGIN <label>-----` to the end
// marker, with nothing but printable text and newlines in between.
bool is_pem(const char* text, size_t size, const char* label);

}  // namespace sentry

#endif  // SENTRY_CREDENTIALS_H
