// Permission with an end to it, measured on this node's own clock.

#include <cstring>

#include "harness.h"
#include "sentry/lease.h"

using sentry::Action;
using sentry::Answer;
using sentry::Answered;
using sentry::Capability;
using sentry::Command;
using sentry::Decision;
using sentry::Lease;

namespace {

Command a_grant(const char* grant_id = "025a1b1f-2969-48c2-b14e-079c8b955f7c",
                double seconds = 300.0, int64_t sequence = 1, Action action = Action::kGrant) {
  Command command;
  std::strcpy(command.command_id, "c-1");
  command.action = action;
  std::strcpy(command.node_id, "pico-ingresso");
  command.hub_epoch = 7;
  command.capability = Capability::kEvents;
  std::strcpy(command.grant_id, grant_id);
  command.duration_seconds = seconds;
  command.sequence = sequence;
  return command;
}

}  // namespace

TEST(a_node_may_not_publish_until_it_is_granted) {
  Lease lease;
  CHECK(!lease.live(0));
  Decision decision = lease.apply(a_grant(), 1000);
  CHECK(decision.answer == Answer::kApplied);
  CHECK(decision.newly_granted);  // the hub is owed a baseline of every source
  CHECK(lease.live(1000));
  CHECK(lease.live(300999));
}

TEST(a_lease_ends_on_its_own_clock_and_not_when_the_hub_says_so) {
  Lease lease;
  lease.apply(a_grant(), 1000);
  CHECK(lease.live(300999));
  CHECK(!lease.live(301000));
  CHECK(!lease.live(301001));
}

TEST(a_renewal_extends_the_grant_it_names_and_no_other) {
  Lease lease;
  lease.apply(a_grant(), 1000);
  Decision renewed = lease.apply(a_grant("025a1b1f-2969-48c2-b14e-079c8b955f7c", 300, 2,
                                         Action::kRenew),
                                 200000);
  CHECK(renewed.answer == Answer::kApplied);
  CHECK(!renewed.newly_granted);  // the same grant continuing: no second baseline
  CHECK(lease.live(499999));

  Decision stranger =
      lease.apply(a_grant("11111111-2222-3333-4444-555555555555", 300, 3, Action::kRenew),
                  210000);
  CHECK(stranger.answer == Answer::kFailed);
  CHECK(lease.live(499999));
}

TEST(a_renewal_that_took_the_long_way_round_does_not_move_the_expiry) {
  Lease lease;
  lease.apply(a_grant(), 1000);
  lease.apply(a_grant("025a1b1f-2969-48c2-b14e-079c8b955f7c", 300, 5, Action::kRenew), 100000);
  uint64_t expiry = lease.expires_at();
  Decision late =
      lease.apply(a_grant("025a1b1f-2969-48c2-b14e-079c8b955f7c", 300, 4, Action::kRenew), 150000);
  CHECK(late.answer == Answer::kFailed);
  CHECK(lease.expires_at() == expiry);
}

TEST(a_grant_that_has_already_expired_is_not_a_grant) {
  Lease lease;
  CHECK(lease.apply(a_grant("025a1b1f-2969-48c2-b14e-079c8b955f7c", 0), 1000).answer ==
        Answer::kFailed);
  CHECK(!lease.live(1000));
}

TEST(a_revocation_takes_the_permission_away_at_once) {
  Lease lease;
  lease.apply(a_grant(), 1000);
  Command revoke = a_grant();
  revoke.action = Action::kRevoke;
  CHECK(lease.apply(revoke, 2000).answer == Answer::kApplied);
  CHECK(!lease.live(2000));
  CHECK(lease.grant_id() == nullptr);
}

TEST(a_dropped_link_ends_the_permission_without_being_told) {
  Lease lease;
  lease.apply(a_grant(), 1000);
  lease.surrender();
  CHECK(!lease.live(1000));
}

TEST(stopping_is_answered_and_asked_for_rather_than_done_here) {
  Lease lease;
  Command stop = a_grant();
  stop.action = Action::kStop;
  Decision decision = lease.apply(stop, 1000);
  CHECK(decision.answer == Answer::kApplied);
  CHECK(decision.stop_requested);
}

TEST(a_command_answered_before_is_answered_again_but_not_acted_on_twice) {
  Answered seen;
  CHECK(seen.recall("c-1") == nullptr);
  seen.remember("c-1", Answer::kApplied);
  const Answer* again = seen.recall("c-1");
  CHECK(again != nullptr && *again == Answer::kApplied);
  seen.remember("c-1", Answer::kFailed);  // a retry does not rewrite what happened
  again = seen.recall("c-1");
  CHECK(again != nullptr && *again == Answer::kApplied);
}

TEST(the_memory_of_commands_has_a_bottom_to_it) {
  Answered seen;
  char id[16];
  for (int index = 0; index < 200; ++index) {
    std::snprintf(id, sizeof(id), "c-%d", index);
    seen.remember(id, Answer::kApplied);
  }
  CHECK(seen.size() == Answered::kCapacity);
  std::snprintf(id, sizeof(id), "c-%d", 199);
  CHECK(seen.recall(id) != nullptr);
  CHECK(seen.recall("c-0") == nullptr);  // long gone, and the lease still protects the rest
}

TEST(a_command_that_arrives_twice_does_not_buy_a_second_lease) {
  // The broker delivers at least once, so a renewal will arrive twice sooner or later.
  // Answering it again is correct; extending the session again is not.
  Answered seen;
  Lease lease;
  Command renewal = a_grant("025a1b1f-2969-48c2-b14e-079c8b955f7c", 300, 2, Action::kRenew);
  std::strcpy(renewal.command_id, "c-renew");
  lease.apply(a_grant(), 1000);

  const Answer* already = seen.recall(renewal.command_id);
  CHECK(already == nullptr);
  Decision first = lease.apply(renewal, 100000);
  seen.remember(renewal.command_id, first.answer);
  uint64_t expiry = lease.expires_at();

  already = seen.recall(renewal.command_id);
  CHECK(already != nullptr && *already == Answer::kApplied);  // answered again, from memory
  CHECK(lease.expires_at() == expiry);                        // and the clock did not move

  // Even if the memory of it were gone, the sequence number says it is not new.
  Decision again = lease.apply(renewal, 200000);
  CHECK(again.answer == Answer::kFailed);
  CHECK(lease.expires_at() == expiry);
}

int main() { return harness::run_all("lease"); }
