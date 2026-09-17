/* What of mbedTLS this firmware compiles in.
 *
 * One kind of connection is made here: a TLS 1.2 client, to a broker whose certificate was
 * signed by an authority we run, proving who it is with a certificate of its own. So this
 * is the smallest configuration that does that — one curve, one key exchange, one cipher
 * suite family — and everything else is left out rather than carried. What is not compiled
 * in cannot be negotiated down to.
 *
 * There is no fallback here and no way to ask for one: a handshake that fails is a node
 * that does not connect, which is the whole point of the certificate.
 */

#ifndef SENTRY_MBEDTLS_CONFIG_H
#define SENTRY_MBEDTLS_CONFIG_H

/* No operating system underneath: the entropy comes from the chip, through the SDK's
 * `mbedtls_hardware_poll`, and there is no /dev/urandom to fall back to. */
#define MBEDTLS_NO_PLATFORM_ENTROPY
#define MBEDTLS_ENTROPY_HARDWARE_ALT

/* P-256, which is what `satellite_admin.py` issues, because a Zero W and a Pico both do
 * that handshake in a fraction of the time an RSA key of comparable strength would take. */
#define MBEDTLS_ECP_C
#define MBEDTLS_ECP_DP_SECP256R1_ENABLED
#define MBEDTLS_ECDH_C
#define MBEDTLS_ECDSA_C
#define MBEDTLS_ECDSA_DETERMINISTIC
#define MBEDTLS_HMAC_DRBG_C
#define MBEDTLS_KEY_EXCHANGE_ECDHE_ECDSA_ENABLED

#define MBEDTLS_AES_C
#define MBEDTLS_GCM_C
#define MBEDTLS_CIPHER_C
#define MBEDTLS_MD_C
#define MBEDTLS_SHA224_C
#define MBEDTLS_SHA256_C
#define MBEDTLS_BIGNUM_C
#define MBEDTLS_OID_C
#define MBEDTLS_ASN1_PARSE_C
#define MBEDTLS_ASN1_WRITE_C
#define MBEDTLS_BASE64_C
#define MBEDTLS_PEM_PARSE_C
#define MBEDTLS_PK_C
#define MBEDTLS_PK_PARSE_C
#define MBEDTLS_ENTROPY_C
#define MBEDTLS_CTR_DRBG_C

#define MBEDTLS_X509_USE_C
#define MBEDTLS_X509_CRT_PARSE_C
#define MBEDTLS_X509_CHECK_KEY_USAGE
#define MBEDTLS_X509_CHECK_EXTENDED_KEY_USAGE

#define MBEDTLS_SSL_TLS_C
#define MBEDTLS_SSL_CLI_C
#define MBEDTLS_SSL_PROTO_TLS1_2
#define MBEDTLS_SSL_SERVER_NAME_INDICATION
#define MBEDTLS_SSL_KEEP_PEER_CERTIFICATE

/* The records this node exchanges are a handshake and then MQTT packets of at most a few
 * hundred bytes. Sixteen kilobytes each way is what the standard allows and not what this
 * conversation needs, and on a board it is memory that could have been a queue. */
#define MBEDTLS_SSL_IN_CONTENT_LEN 4096
#define MBEDTLS_SSL_OUT_CONTENT_LEN 2048

/* Error strings, because a handshake that failed with a number nobody can look up is a
 * night spent guessing. */
#define MBEDTLS_ERROR_C

/* lwIP's TLS layer reads fields mbedTLS 3 calls private. That is lwIP's business with
 * mbedTLS and not an invitation for this firmware to do the same. */
#define MBEDTLS_ALLOW_PRIVATE_ACCESS

/* A certificate that expired is a certificate that expired, and a board with no clock
 * would have no way to say so. There is no system clock here — `timebase.h` is what this
 * firmware trusts about time — so mbedTLS is given the time the network last said it was,
 * and nothing at all before an answer has arrived. A node that has not been told the time
 * does not connect, which is the same rule its readings follow.
 */
#define MBEDTLS_PLATFORM_C
#define MBEDTLS_HAVE_TIME
#define MBEDTLS_HAVE_TIME_DATE
#define MBEDTLS_PLATFORM_TIME_TYPE_MACRO long long
#define MBEDTLS_PLATFORM_TIME_MACRO sentry_unix_seconds

/* And, separately, the monotonic milliseconds mbedTLS measures its own timeouts with.
 * That one is the counter on the chip and never the network's answer: a retransmission
 * timer moved by an SNTP correction would be a timer that jumps. */
#define MBEDTLS_PLATFORM_MS_TIME_ALT

#ifdef __cplusplus
extern "C" {
#endif
long long sentry_unix_seconds(long long* out);
#ifdef __cplusplus
}
#endif

#endif /* SENTRY_MBEDTLS_CONFIG_H */
