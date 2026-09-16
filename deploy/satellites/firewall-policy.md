# What the satellites are allowed to reach

A satellite is a small computer in a hallway running an agent that was written quickly and
will be updated rarely. It should be able to reach exactly one thing.

## The shape

| From | To | Port | Why |
|---|---|---|---|
| Satellite | Hub | 8883/tcp | MQTT over TLS. The only link the base profile needs. |
| Hub | Satellite | — | Nothing. Commands travel back through the broker, not to the node. |
| Satellite | Anywhere else | — | Nothing. Not the internet, not each other, not the rest of the house. |

Nodes do not talk to nodes. Two satellites that cannot reach each other cannot be used to
reach each other, and the broker's access control already makes sure neither can read the
other's topics.

The media ports belong to a later increment and are not opened here. When they are, they
are opened per node, per lease, and never as a range.

## On the hub

The broker listens on the address the satellites use, not on every interface it happens to
have. If the hub is also the machine with the cameras and the web interface, that
interface and this one are separate concerns: exposing 8883 is not a reason to expose 8443.

## What this does not protect against

A satellite on the same physical network as everything else is still on that network. If
the sensors are worth protecting, the satellites belong on their own subnet or VLAN, with
this policy applied at the boundary rather than on each board. Host firewalls on the nodes
are a second line, not the line.

## Certificates and addresses

The hub's certificate is issued for the address the satellites actually connect to,
including when that is a bare IP — `satellite_admin.py hub-cert --address 192.168.11.10`.
Nothing here ever turns verification off to make an address work; the certificate is what
gets reissued.
