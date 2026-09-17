// What a certificate, an authority and a private key are, before any of them is trusted.
//
// Nothing here is real cryptographic material and nothing needs to be: what this module
// decides is whether it was handed the kind of thing it was offered, which is the mistake
// that happens at a serial port at two in the morning. Whether the key matches the
// certificate is mbedTLS's question, and it asks it during the handshake.

#include <cstring>
#include <string>

#include "harness.h"
#include "sentry/credentials.h"

using sentry::Credentials;
using sentry::is_pem;

namespace {

std::string pem(const char* label, const char* body = "MIIBkTCCATegAwIBAgIUX0Yk1Q") {
  std::string text = "-----BEGIN ";
  text += label;
  text += "-----\n";
  text += body;
  text += "\n-----END ";
  text += label;
  text += "-----\n";
  return text;
}

bool take_authority(Credentials& held, const std::string& text) {
  return held.take_authority(text.c_str(), text.size());
}

bool take_certificate(Credentials& held, const std::string& text) {
  return held.take_certificate(text.c_str(), text.size());
}

bool take_key(Credentials& held, const std::string& text) {
  return held.take_private_key(text.c_str(), text.size());
}

std::string document(const std::string& ca, const std::string& cert, const std::string& key) {
  auto escaped = [](const std::string& text) {
    std::string out;
    for (char c : text) {
      if (c == '\n') {
        out += "\\n";
      } else {
        out.push_back(c);
      }
    }
    return out;
  };
  std::string out = "{\"ca\":\"";
  out += escaped(ca);
  out += "\",\"cert\":\"";
  out += escaped(cert);
  out += "\",\"key\":\"";
  out += escaped(key);
  out += "\"}";
  return out;
}

}  // namespace

TEST(a_node_with_nothing_has_no_business_connecting_to_anything) {
  Credentials held;
  CHECK(!held.complete());
  CHECK(!held.has_authority());
  CHECK(!held.has_certificate());
  CHECK(!held.has_private_key());
  CHECK(held.authority_size() == 0);
}

TEST(each_of_the_three_is_kept_with_the_terminator_mbedtls_counts) {
  Credentials held;
  const std::string authority = pem("CERTIFICATE");
  CHECK(take_authority(held, authority));
  CHECK(held.authority_size() == authority.size() + 1);
  CHECK(std::strlen(held.authority()) == authority.size());
  CHECK(!held.complete());

  CHECK(take_certificate(held, pem("CERTIFICATE", "MIIBZzCCAQ2gAwIBAgIUdw")));
  CHECK(take_key(held, pem("PRIVATE KEY", "MIGHAgEAMBMGByqGSM49")));
  CHECK(held.complete());
  // The two certificates are two certificates, not one written twice.
  CHECK(std::strcmp(held.authority(), held.certificate()) != 0);
}

TEST(a_key_where_a_certificate_was_asked_for_is_not_a_certificate) {
  Credentials held;
  CHECK(!take_certificate(held, pem("PRIVATE KEY")));
  CHECK(!take_authority(held, pem("EC PRIVATE KEY")));
  CHECK(!take_key(held, pem("CERTIFICATE")));
  CHECK(!held.has_certificate());
  CHECK(!held.has_private_key());
}

TEST(a_key_openssl_wrote_the_old_way_is_still_a_key) {
  Credentials held;
  CHECK(take_key(held, pem("EC PRIVATE KEY", "MHcCAQEEIJ7d")));
  CHECK(held.has_private_key());
}

TEST(what_is_not_pem_is_not_kept) {
  Credentials held;
  const std::string good = pem("CERTIFICATE");
  CHECK(take_certificate(held, good));

  CHECK(!take_certificate(held, "MIIBkTCCATegAwIBAgIUX0Yk1Q"));           // no markers
  CHECK(!take_certificate(held, "-----BEGIN CERTIFICATE-----\nMIIB\n"));  // never ends
  std::string binary = pem("CERTIFICATE");
  binary[30] = '\x01';
  CHECK(!take_certificate(held, binary));
  std::string trailing = pem("CERTIFICATE") + "and a note about it\n";
  CHECK(!take_certificate(held, trailing));
  // The one that was already there is the one that is still there.
  CHECK(std::strcmp(held.certificate(), good.c_str()) == 0);
}

TEST(more_than_there_is_room_for_is_refused_rather_than_cut_short) {
  Credentials held;
  const std::string enormous = pem("CERTIFICATE", std::string(4096, 'A').c_str());
  CHECK(!take_certificate(held, enormous));
  CHECK(!held.has_certificate());
}

TEST(a_record_is_taken_whole_or_not_at_all) {
  Credentials held;
  const std::string authority = pem("CERTIFICATE", "MIIBAAAA");
  const std::string certificate = pem("CERTIFICATE", "MIIBBBBB");
  const std::string key = pem("PRIVATE KEY", "MIGHAAAA");
  const std::string whole = document(authority, certificate, key);
  CHECK(sentry::read_credentials(whole.c_str(), whole.size(), held));
  CHECK(held.complete());

  // A second record with a key that is a certificate changes nothing, including the
  // authority that came before it in the document.
  const std::string wrong = document(pem("CERTIFICATE", "MIIBCCCC"), certificate, certificate);
  CHECK(!sentry::read_credentials(wrong.c_str(), wrong.size(), held));
  CHECK(std::strcmp(held.authority(), authority.c_str()) == 0);
  CHECK(std::strcmp(held.private_key(), key.c_str()) == 0);
}

TEST(an_authority_can_be_replaced_on_its_own) {
  Credentials held;
  const std::string first = document(pem("CERTIFICATE", "MIIBAAAA"), pem("CERTIFICATE", "MIIBBBBB"),
                                     pem("PRIVATE KEY", "MIGHAAAA"));
  CHECK(sentry::read_credentials(first.c_str(), first.size(), held));

  const std::string next = pem("CERTIFICATE", "MIIBZZZZ");
  std::string only = "{\"ca\":\"";
  for (char c : next) {
    if (c == '\n') {
      only += "\\n";
    } else {
      only.push_back(c);
    }
  }
  only += "\"}";
  CHECK(sentry::read_credentials(only.c_str(), only.size(), held));
  CHECK(std::strcmp(held.authority(), next.c_str()) == 0);
  CHECK(held.complete());  // the identity it already had is still there
}

TEST(a_document_with_nothing_in_it_is_not_a_record) {
  Credentials held;
  CHECK(!sentry::read_credentials("{}", 2, held));
  const char* unknown = "{\"passphrase\":\"hunter2\"}";
  CHECK(!sentry::read_credentials(unknown, std::strlen(unknown), held));
  const char* wrong_type = "{\"ca\":41}";
  CHECK(!sentry::read_credentials(wrong_type, std::strlen(wrong_type), held));
}

TEST(forgetting_leaves_nothing_of_the_key_behind) {
  Credentials held;
  const std::string key = pem("PRIVATE KEY", "MIGHsecretsecret");
  CHECK(take_key(held, key));
  CHECK(take_certificate(held, pem("CERTIFICATE")));
  held.forget();
  CHECK(!held.complete());
  CHECK(!held.has_private_key());
  CHECK(held.private_key()[0] == '\0');
  CHECK(held.certificate()[0] == '\0');
  CHECK(std::memcmp(held.private_key(), key.c_str(), 8) != 0);
}

TEST(a_label_is_the_label_it_says_it_is) {
  const std::string certificate = pem("CERTIFICATE");
  CHECK(is_pem(certificate.c_str(), certificate.size(), "CERTIFICATE"));
  CHECK(!is_pem(certificate.c_str(), certificate.size(), "PRIVATE KEY"));
  CHECK(!is_pem(nullptr, 10, "CERTIFICATE"));
  CHECK(!is_pem(certificate.c_str(), 0, "CERTIFICATE"));
}

int main() { return harness::run_all("credentials"); }
